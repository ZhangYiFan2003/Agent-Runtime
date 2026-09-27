from __future__ import annotations

import asyncio
import json
import os
import select
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from axiom.config import ProviderGatewayConfig, ProviderTargetConfig
from axiom.llm.base import LlmClient
from axiom.llm.openai_compatible import OpenAICompatibleClient
from axiom.runtime.dependency import DependencyFailureCategory, RetryClassifier
from axiom.types import Message


class CircuitState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class Admission:
    target: ProviderTargetConfig
    state: _TargetState
    wait_ms: float
    circuit_state: CircuitState

    def release(self) -> None:
        self.state.release()


class ProviderGatewayRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status_code: int,
        failure_category: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.failure_category = failure_category
        self.retry_after_seconds = retry_after_seconds

    def payload(self) -> dict[str, Any]:
        return {
            "type": "error",
            "code": self.code,
            "message": str(self),
            "status_code": self.status_code,
            "failure_category": self.failure_category,
            "retry_after_seconds": self.retry_after_seconds,
        }


class _TargetState:
    def __init__(self, config: ProviderTargetConfig) -> None:
        self.config = config
        self._condition = threading.Condition()
        self.active = 0
        self.pending = 0
        self.circuit_state = CircuitState.CLOSED
        self.failure_count = 0
        self.open_until = 0.0
        self.half_open_probes = 0
        self.cooldown_until = 0.0
        self.tokens = float(config.requests_per_minute or 0)
        self.last_refill = time.monotonic()
        self.attempts = 0
        self.failures = 0
        self.rate_limited_count = 0
        self.circuit_rejections = 0
        self.busy_rejections = 0
        self.total_wait_ms = 0.0

    def admit(self) -> tuple[Admission | None, str, float | None]:
        started = time.monotonic()
        deadline = started + self.config.admission_timeout_seconds
        with self._condition:
            reason, retry_after = self._availability(started)
            if reason:
                self._record_rejection(reason)
                return None, reason, retry_after
            if self.active >= self.config.max_concurrency:
                if self.pending >= self.config.max_pending:
                    self.busy_rejections += 1
                    return None, "busy", self.config.admission_timeout_seconds
                self.pending += 1
                try:
                    while self.active >= self.config.max_concurrency:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self.busy_rejections += 1
                            return None, "busy", self.config.admission_timeout_seconds
                        self._condition.wait(remaining)
                        reason, retry_after = self._availability(time.monotonic())
                        if reason:
                            self._record_rejection(reason)
                            return None, reason, retry_after
                finally:
                    self.pending -= 1
            now = time.monotonic()
            reason, retry_after = self._availability(now)
            if reason:
                self._record_rejection(reason)
                return None, reason, retry_after
            rpm_retry = self._consume_rpm(now)
            if rpm_retry is not None:
                self.rate_limited_count += 1
                return None, "rate_limited", rpm_retry
            self.active += 1
            self.attempts += 1
            if self.circuit_state == CircuitState.HALF_OPEN:
                self.half_open_probes += 1
            wait_ms = round((time.monotonic() - started) * 1000, 3)
            self.total_wait_ms += wait_ms
            return (
                Admission(self.config, self, wait_ms, self.circuit_state),
                "",
                None,
            )

    def release(self) -> None:
        with self._condition:
            self.active = max(0, self.active - 1)
            self._condition.notify()

    def record_success(self) -> None:
        with self._condition:
            self.circuit_state = CircuitState.CLOSED
            self.failure_count = 0
            self.open_until = 0.0
            self.half_open_probes = 0

    def record_failure(
        self,
        category: DependencyFailureCategory,
        *,
        retry_after_seconds: float | None,
    ) -> None:
        with self._condition:
            self.failures += 1
            now = time.monotonic()
            if category == DependencyFailureCategory.RATE_LIMITED:
                self.rate_limited_count += 1
                delay = retry_after_seconds or self.config.rate_limit_cooldown_seconds
                self.cooldown_until = max(self.cooldown_until, now + delay)
                return
            if category not in {
                DependencyFailureCategory.CONNECTION_ERROR,
                DependencyFailureCategory.TIMEOUT,
                DependencyFailureCategory.TRANSIENT_SERVER_ERROR,
            }:
                return
            self.failure_count += 1
            if (
                self.circuit_state == CircuitState.HALF_OPEN
                or self.failure_count >= self.config.failure_threshold
            ):
                self.circuit_state = CircuitState.OPEN
                self.open_until = now + self.config.open_seconds
                self.half_open_probes = 0

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            now = time.monotonic()
            self._refresh_circuit(now)
            return {
                "id": self.config.id,
                "provider": self.config.provider,
                "model": self.config.model,
                "circuit_state": self.circuit_state.value,
                "active": self.active,
                "pending": self.pending,
                "max_concurrency": self.config.max_concurrency,
                "max_pending": self.config.max_pending,
                "cooldown_seconds": round(max(0.0, self.cooldown_until - now), 3),
                "attempts": self.attempts,
                "failures": self.failures,
                "rate_limited": self.rate_limited_count,
                "circuit_rejections": self.circuit_rejections,
                "busy_rejections": self.busy_rejections,
                "admission_wait_ms": round(self.total_wait_ms, 3),
            }

    def _availability(self, now: float) -> tuple[str, float | None]:
        self._refresh_circuit(now)
        if now < self.cooldown_until:
            return "cooldown", self.cooldown_until - now
        if self.circuit_state == CircuitState.OPEN:
            return "circuit_open", max(0.0, self.open_until - now)
        if (
            self.circuit_state == CircuitState.HALF_OPEN
            and self.half_open_probes >= self.config.half_open_max_probes
        ):
            return "circuit_open", self.config.open_seconds
        return "", None

    def _refresh_circuit(self, now: float) -> None:
        if self.circuit_state == CircuitState.OPEN and now >= self.open_until:
            self.circuit_state = CircuitState.HALF_OPEN
            self.half_open_probes = 0

    def _consume_rpm(self, now: float) -> float | None:
        rpm = self.config.requests_per_minute
        if rpm is None:
            return None
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(float(rpm), self.tokens + elapsed * (float(rpm) / 60.0))
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return None
        return (1.0 - self.tokens) / (float(rpm) / 60.0)

    def _record_rejection(self, reason: str) -> None:
        if reason == "circuit_open":
            self.circuit_rejections += 1
        elif reason in {"cooldown", "rate_limited"}:
            self.rate_limited_count += 1
        else:
            self.busy_rejections += 1


ClientFactory = Callable[[ProviderTargetConfig, str], LlmClient]
CredentialResolver = Callable[[str], str]


class ProviderGateway:
    def __init__(
        self,
        config: ProviderGatewayConfig,
        *,
        client_factory: ClientFactory | None = None,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        self.config = config
        self._states = {
            target.id: _TargetState(target)
            for targets in config.routes.values()
            for target in targets
        }
        self._client_factory = client_factory or _default_client
        self._credential_resolver = credential_resolver or (lambda name: os.environ.get(name, ""))
        self._classifier = RetryClassifier()

    async def stream(self, request: Mapping[str, Any]) -> AsyncIterator[dict[str, Any]]:
        route = str(request.get("route") or "")
        messages, tools, system_prompt, settings = self._validate_request(request)
        admission, skipped = await asyncio.to_thread(self._select, route)
        if admission is None:
            retry_after = min(
                (
                    float(item["retry_after_seconds"])
                    for item in skipped
                    if item["retry_after_seconds"] is not None
                ),
                default=None,
            )
            raise ProviderGatewayRequestError(
                "no provider target is currently available",
                code="PROVIDER_BUSY",
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                failure_category=DependencyFailureCategory.RATE_LIMITED.value,
                retry_after_seconds=retry_after,
            )
        target = admission.target
        selected = {
            "type": "provider_selected",
            "route": route,
            "provider": target.provider,
            "model": target.model,
            "target_id": target.id,
            "gateway_wait_ms": admission.wait_ms,
            "circuit_state": admission.circuit_state.value,
            "rate_limited": False,
            "skipped_targets": skipped,
        }
        try:
            yield selected
            effective_target = _effective_target(target, settings)
            client = self._client_factory(
                effective_target,
                self._credential_resolver(target.api_key_env),
            )
            async for event in client.chat(messages, tools, system_prompt=system_prompt):
                if event.get("type") == "error":
                    error = event.get("error")
                    if isinstance(error, BaseException):
                        raise error
                    raise RuntimeError(str(error or "upstream provider failed"))
                yield _augment_usage(event, route=route, target=target)
            admission.state.record_success()
        except asyncio.CancelledError:
            raise
        except ProviderGatewayRequestError as exc:
            yield {
                **exc.payload(),
                "route": route,
                "provider": target.provider,
                "model": target.model,
                "target_id": target.id,
            }
        except Exception as exc:  # noqa: BLE001 - upstream failures become normalized events
            category = self._classifier.classify(exc)
            retry_after = self._classifier.retry_after_seconds(exc)
            admission.state.record_failure(category, retry_after_seconds=retry_after)
            yield {
                "type": "error",
                "code": _error_code(category),
                "message": "upstream provider request failed",
                "status_code": _status_code(exc) or _category_status(category),
                "failure_category": category.value,
                "retry_after_seconds": retry_after,
                "route": route,
                "provider": target.provider,
                "model": target.model,
                "target_id": target.id,
            }
        finally:
            admission.release()

    def health(self) -> dict[str, Any]:
        targets = [state.snapshot() for state in self._states.values()]
        return {
            "status": "ok",
            "routes": len(self.config.routes),
            "targets": targets,
        }

    def _select(self, route: str) -> tuple[Admission | None, list[dict[str, Any]]]:
        targets = self.config.routes.get(route)
        if targets is None:
            raise ProviderGatewayRequestError(
                "unknown provider route",
                code="PROVIDER_ROUTE_NOT_FOUND",
                status_code=HTTPStatus.NOT_FOUND,
                failure_category=DependencyFailureCategory.VALIDATION_ERROR.value,
            )
        skipped: list[dict[str, Any]] = []
        for target in targets:
            admission, reason, retry_after = self._states[target.id].admit()
            if admission is not None:
                return admission, skipped
            skipped.append(
                {
                    "target_id": target.id,
                    "reason": reason,
                    "retry_after_seconds": retry_after,
                }
            )
        return None, skipped

    def _validate_request(
        self, request: Mapping[str, Any]
    ) -> tuple[list[Message], list[dict[str, Any]], str, dict[str, Any]]:
        route = request.get("route")
        raw_messages = request.get("messages")
        raw_tools = request.get("tools") or []
        system_prompt = request.get("system_prompt")
        settings = request.get("settings") or {}
        if not isinstance(route, str) or not route.strip():
            raise _invalid_request("provider route is required")
        if not isinstance(raw_messages, list) or len(raw_messages) > self.config.max_messages:
            raise _invalid_request("provider message count is invalid")
        if not isinstance(raw_tools, list):
            raise _invalid_request("provider tools must be an array")
        tool_bytes = len(json.dumps(raw_tools, separators=(",", ":")).encode("utf-8"))
        if tool_bytes > self.config.max_tool_schema_bytes:
            raise _invalid_request("provider tool schemas are too large")
        if not isinstance(system_prompt, str):
            raise _invalid_request("provider system prompt is required")
        if not isinstance(settings, dict):
            raise _invalid_request("provider generation settings must be an object")
        messages = [_parse_message(item) for item in raw_messages]
        if not all(isinstance(item, dict) for item in raw_tools):
            raise _invalid_request("provider tool schemas must be objects")
        return messages, list(raw_tools), system_prompt, settings


class ProviderGatewayHttpServer:
    def __init__(
        self,
        gateway: ProviderGateway,
        *,
        host: str = "127.0.0.1",
        port: int = 8070,
    ) -> None:
        self.gateway = gateway
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None

    def serve_forever(self) -> None:
        gateway = self.gateway

        class Handler(BaseHTTPRequestHandler):
            server_version = "AxiomProviderGateway/1"

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path not in {"/health", "/v1/providers"}:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                self._json(HTTPStatus.OK, gateway.health())

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
                if self.path != "/v1/chat":
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                try:
                    request = self._request_json()
                except ProviderGatewayRequestError as exc:
                    self._json(exc.status_code, exc.payload())
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("content-type", "application/x-ndjson")
                self.send_header("cache-control", "no-store")
                self.end_headers()

                async def write_stream() -> None:
                    stream = gateway.stream(request)
                    try:
                        while True:
                            pending = asyncio.create_task(anext(stream))
                            while not pending.done():
                                disconnected = await asyncio.to_thread(
                                    _client_disconnected,
                                    self.connection,
                                    0.05,
                                )
                                if disconnected:
                                    pending.cancel()
                                    await asyncio.gather(pending, return_exceptions=True)
                                    return
                            try:
                                event = pending.result()
                            except StopAsyncIteration:
                                return
                            line = json.dumps(event, separators=(",", ":"), default=str)
                            self.wfile.write(line.encode("utf-8") + b"\n")
                            self.wfile.flush()
                    except ProviderGatewayRequestError as exc:
                        line = json.dumps(exc.payload(), separators=(",", ":"))
                        self.wfile.write(line.encode("utf-8") + b"\n")
                        self.wfile.flush()
                    finally:
                        await stream.aclose()

                try:
                    asyncio.run(write_stream())
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _request_json(self) -> dict[str, Any]:
                raw_length = self.headers.get("content-length")
                try:
                    length = int(raw_length or "0")
                except ValueError as exc:
                    raise _invalid_request("invalid content length") from exc
                if length <= 0 or length > gateway.config.request_max_bytes:
                    raise _invalid_request("provider request body size is invalid")
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise _invalid_request("provider request body is invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise _invalid_request("provider request body must be an object")
                return payload

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        if self._httpd is not None:
            with suppress(Exception):
                self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _default_client(target: ProviderTargetConfig, api_key: str) -> LlmClient:
    return OpenAICompatibleClient(
        provider_name=target.provider,
        model=target.model,
        api_key=api_key,
        base_url=target.base_url,
        max_tokens=target.max_tokens,
        temperature=target.temperature,
        timeout=target.timeout,
        max_context_window=target.context_window,
        prompt_cache=target.prompt_cache,
    )


def _parse_message(value: Any) -> Message:
    if not isinstance(value, dict):
        raise _invalid_request("provider messages must be objects")
    role = value.get("role")
    content = value.get("content")
    if role not in {"system", "user", "assistant", "tool"}:
        raise _invalid_request("provider message role is invalid")
    if not isinstance(content, (str, list)):
        raise _invalid_request("provider message content is invalid")
    tool_calls = value.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        raise _invalid_request("provider message tool_calls are invalid")
    return Message(
        role=role,
        content=content,
        name=_optional_text(value.get("name")),
        tool_call_id=_optional_text(value.get("tool_call_id")),
        tool_calls=tool_calls,
    )


def _augment_usage(
    event: dict[str, Any], *, route: str, target: ProviderTargetConfig
) -> dict[str, Any]:
    if event.get("type") != "usage":
        return event
    result = dict(event)
    result.update(
        {
            "route": route,
            "provider": target.provider,
            "model": target.model,
            "target_id": target.id,
        }
    )
    return result


def _effective_target(
    target: ProviderTargetConfig,
    settings: Mapping[str, Any],
) -> ProviderTargetConfig:
    try:
        max_tokens = int(settings.get("max_tokens", target.max_tokens))
        temperature = float(settings.get("temperature", target.temperature))
        timeout = float(settings.get("timeout_seconds", target.timeout))
    except (TypeError, ValueError) as exc:
        raise _invalid_request("provider generation settings are invalid") from exc
    if max_tokens <= 0 or temperature < 0 or timeout <= 0:
        raise _invalid_request("provider generation settings are out of range")
    return replace(
        target,
        max_tokens=min(max_tokens, target.max_tokens),
        temperature=temperature,
        timeout=min(timeout, target.timeout),
    )


def _invalid_request(message: str) -> ProviderGatewayRequestError:
    return ProviderGatewayRequestError(
        message,
        code="PROVIDER_INVALID_REQUEST",
        status_code=HTTPStatus.BAD_REQUEST,
        failure_category=DependencyFailureCategory.VALIDATION_ERROR.value,
    )


def _error_code(category: DependencyFailureCategory) -> str:
    return {
        DependencyFailureCategory.RATE_LIMITED: "PROVIDER_RATE_LIMITED",
        DependencyFailureCategory.AUTH_ERROR: "PROVIDER_AUTH_ERROR",
        DependencyFailureCategory.VALIDATION_ERROR: "PROVIDER_VALIDATION_ERROR",
        DependencyFailureCategory.TIMEOUT: "PROVIDER_TIMEOUT",
    }.get(category, "PROVIDER_UPSTREAM_ERROR")


def _status_code(exc: BaseException) -> int | None:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        response = getattr(current, "response", None)
        status = getattr(current, "status_code", None) or getattr(response, "status_code", None)
        if status is not None:
            try:
                return int(status)
            except (TypeError, ValueError):
                return None
        current = current.__cause__ or current.__context__
    return None


def _category_status(category: DependencyFailureCategory) -> int:
    return {
        DependencyFailureCategory.RATE_LIMITED: HTTPStatus.TOO_MANY_REQUESTS,
        DependencyFailureCategory.AUTH_ERROR: HTTPStatus.UNAUTHORIZED,
        DependencyFailureCategory.VALIDATION_ERROR: HTTPStatus.BAD_REQUEST,
        DependencyFailureCategory.TIMEOUT: HTTPStatus.GATEWAY_TIMEOUT,
        DependencyFailureCategory.CONNECTION_ERROR: HTTPStatus.SERVICE_UNAVAILABLE,
        DependencyFailureCategory.TRANSIENT_SERVER_ERROR: HTTPStatus.BAD_GATEWAY,
    }.get(category, HTTPStatus.INTERNAL_SERVER_ERROR)


def _optional_text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _client_disconnected(connection: socket.socket, timeout: float) -> bool:
    try:
        readable, _, _ = select.select([connection], [], [], timeout)
        return bool(readable) and connection.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True
