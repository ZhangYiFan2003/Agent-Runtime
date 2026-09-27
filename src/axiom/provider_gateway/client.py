from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from axiom.types import Message


class ProviderGatewayError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_GATEWAY_ERROR",
        status_code: int | None = None,
        failure_category: str = "unknown",
        retry_after_seconds: float | None = None,
        provider: str | None = None,
        model: str | None = None,
        target_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.failure_category = failure_category
        self.retry_after_seconds = retry_after_seconds
        self.provider = provider
        self.model = model
        self.target_id = target_id
        headers = (
            {"retry-after": str(retry_after_seconds)}
            if retry_after_seconds is not None
            else None
        )
        self.response = (
            httpx.Response(status_code, headers=headers)
            if status_code is not None
            else None
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProviderGatewayError:
        return cls(
            str(payload.get("message") or "provider gateway request failed"),
            code=str(payload.get("code") or "PROVIDER_GATEWAY_ERROR"),
            status_code=_optional_int(payload.get("status_code")),
            failure_category=str(payload.get("failure_category") or "unknown"),
            retry_after_seconds=_optional_float(payload.get("retry_after_seconds")),
            provider=_optional_text(payload.get("provider")),
            model=_optional_text(payload.get("model")),
            target_id=_optional_text(payload.get("target_id")),
        )


@dataclass(slots=True)
class GatewayLlmClient:
    route_name: str
    gateway_url: str
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: float = 120.0
    max_context_window: int = 128_000

    @property
    def provider_name(self) -> str:
        return "gateway"

    @property
    def model_name(self) -> str:
        return self.route_name

    async def healthcheck(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.gateway_url.rstrip('/')}/health")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderGatewayError(
                "provider gateway is unavailable",
                code="PROVIDER_GATEWAY_UNAVAILABLE",
                failure_category="connection_error",
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise ProviderGatewayError(
                "provider gateway health check failed",
                code="PROVIDER_GATEWAY_UNAVAILABLE",
                failure_category="connection_error",
            )

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> AsyncIterator[dict[str, Any]]:
        body = {
            "route": self.route_name,
            "messages": [_message_payload(message) for message in messages],
            "tools": tools,
            "system_prompt": system_prompt,
            "settings": {
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "timeout_seconds": self.timeout,
            },
        }
        try:
            async with (
                httpx.AsyncClient(timeout=httpx.Timeout(self.timeout + 10.0)) as client,
                client.stream(
                    "POST",
                    f"{self.gateway_url.rstrip('/')}/v1/chat",
                    json=body,
                ) as response,
            ):
                if response.is_error:
                    raw = await response.aread()
                    raise ProviderGatewayError.from_payload(
                        _error_payload(raw, response.status_code)
                    )
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ProviderGatewayError(
                            "provider gateway returned invalid NDJSON",
                            code="PROVIDER_GATEWAY_PROTOCOL_ERROR",
                        ) from exc
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise ProviderGatewayError(
                            "provider gateway returned an invalid event",
                            code="PROVIDER_GATEWAY_PROTOCOL_ERROR",
                        )
                    if event["type"] == "error":
                        yield {"type": "error", "error": ProviderGatewayError.from_payload(event)}
                        return
                    yield event
        except ProviderGatewayError:
            raise
        except httpx.TimeoutException as exc:
            raise ProviderGatewayError(
                "provider gateway request timed out",
                code="PROVIDER_GATEWAY_TIMEOUT",
                failure_category="timeout",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderGatewayError(
                "provider gateway is unavailable",
                code="PROVIDER_GATEWAY_UNAVAILABLE",
                failure_category="connection_error",
            ) from exc


def _message_payload(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
    }


def _error_payload(raw: bytes, status_code: int) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("status_code", status_code)
    payload.setdefault("message", "provider gateway request failed")
    return payload


def _optional_text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
