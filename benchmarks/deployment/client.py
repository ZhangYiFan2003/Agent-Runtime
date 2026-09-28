from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from benchmarks.deployment.common import SAFE_STATUSES, TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class RuntimeHttpError(RuntimeError):
    status_code: int
    code: str | None
    message: str
    details: dict[str, Any]

    def __str__(self) -> str:
        suffix = f" ({self.code})" if self.code else ""
        return f"HTTP {self.status_code}{suffix}: {self.message}"


class RuntimeClient:
    """Small real-HTTP client used only by the deployment evidence harness."""

    def __init__(self, base_url: str, *, api_key: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout),
        )

    async def __aenter__(self) -> RuntimeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        response = await self._client.get("/health")
        return _json(response)

    async def create_thread(self) -> str:
        payload = _json(await self._client.post("/v1/threads", json={}))
        thread_id = payload.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("Runtime returned a malformed Thread response")
        return thread_id

    async def submit_turn(
        self,
        thread_id: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key or f"stage15-{uuid4().hex}"}
        response = await self._client.post(
            f"/v1/threads/{quote(thread_id, safe='')}/turns",
            json={"message": message},
            headers=headers,
        )
        return _json(response)

    async def list_runs(self) -> list[dict[str, Any]]:
        payload = _json(await self._client.get("/v1/runs"))
        runs = payload.get("runs")
        return [item for item in runs if isinstance(item, dict)] if isinstance(runs, list) else []

    async def run(self, run_id: str) -> dict[str, Any]:
        return _json(await self._client.get(f"/v1/runs/{quote(run_id, safe='')}"))

    async def wait_for_run(
        self,
        run_id: str,
        *,
        timeout: float,
        statuses: set[str] | None = None,
        interval: float = 0.25,
    ) -> dict[str, Any]:
        wanted = statuses or TERMINAL_STATUSES
        deadline = asyncio.get_running_loop().time() + timeout
        last: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                last = await self.run(run_id)
                if str(last.get("status")) in wanted:
                    return last
            except (httpx.TransportError, RuntimeHttpError):
                pass
            await asyncio.sleep(interval)
        status = last.get("status") if last else "unavailable"
        raise TimeoutError(f"Run {run_id} did not reach {sorted(wanted)}; last status={status}")

    async def wait_for_safe_state(self, run_id: str, *, timeout: float) -> dict[str, Any]:
        return await self.wait_for_run(run_id, timeout=timeout, statuses=SAFE_STATUSES)

    async def events(
        self,
        thread_id: str,
        *,
        run_id: str | None = None,
        after_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, str | int] = {}
        if run_id:
            params["run_id"] = run_id
        if after_id is not None:
            params["after_id"] = after_id
        response = await self._client.get(
            f"/v1/threads/{quote(thread_id, safe='')}/events",
            params=params,
            headers={"Accept": "text/event-stream"},
        )
        if response.is_error:
            _raise_http_error(response)
        return parse_sse_events(response.text)

    async def wait_for_event(
        self,
        thread_id: str,
        run_id: str,
        event_types: set[str],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        cursor: int | None = None
        while asyncio.get_running_loop().time() < deadline:
            for event in await self.events(thread_id, run_id=run_id, after_id=cursor):
                event_id = event.get("event_id")
                if isinstance(event_id, int):
                    cursor = max(cursor or event_id, event_id)
                if event.get("event_type") in event_types:
                    return event
            await asyncio.sleep(0.2)
        raise TimeoutError(f"Run {run_id} emitted none of {sorted(event_types)}")

    async def resume(
        self,
        run_id: str,
        *,
        decision: str,
        invocation_id: str,
    ) -> dict[str, Any]:
        response = await self._client.post(
            f"/v1/runs/{quote(run_id, safe='')}/resume",
            json={"decision": decision, "invocation_id": invocation_id},
            headers={"Idempotency-Key": f"stage15-control-{uuid4().hex}"},
        )
        return _json(response)

    async def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        payload = _json(await self._client.get(f"/v1/runs/{quote(run_id, safe='')}/artifacts"))
        items = payload.get("artifacts")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    async def artifact_content(self, artifact_id: str) -> bytes:
        response = await self._client.get(f"/v1/artifacts/{quote(artifact_id, safe='')}/content")
        if response.is_error:
            _raise_http_error(response)
        return response.content

    async def claims(self, run_id: str) -> list[dict[str, Any]]:
        payload = _json(await self._client.get(f"/v1/runs/{quote(run_id, safe='')}/claims"))
        items = payload.get("claims")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def parse_sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        data_lines = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return sorted(events, key=lambda item: int(item.get("event_id") or 0))


def event_timings(events: list[dict[str, Any]]) -> dict[str, float | None]:
    accepted = _first_timestamp(events, {"turn.started", "run.admitted"})
    claimed = _first_timestamp(events, {"run.claimed", "run.taken_over"})
    terminal = _first_timestamp(events, {"run.completed", "run.failed", "run.cancelled"})
    return {
        "queue_wait_ms": _delta_ms(accepted, claimed),
        "run_ms": _delta_ms(claimed, terminal),
        "server_end_to_end_ms": _delta_ms(accepted, terminal),
    }


def _first_timestamp(events: list[dict[str, Any]], types: set[str]) -> datetime | None:
    for event in events:
        if event.get("event_type") not in types:
            continue
        value = event.get("timestamp")
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def _delta_ms(started: datetime | None, ended: datetime | None) -> float | None:
    if started is None or ended is None:
        return None
    return round(max(0.0, (ended - started).total_seconds() * 1000), 3)


def _json(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        _raise_http_error(response)
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Runtime returned a non-object JSON response")
    return payload


def _raise_http_error(response: httpx.Response) -> None:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    code: str | None = None
    details: dict[str, Any] = {}
    message = response.reason_phrase or "request failed"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code")) if error.get("code") is not None else None
            message = str(error.get("message") or message)
            raw_details = error.get("details")
            details = raw_details if isinstance(raw_details, dict) else {}
        elif error is not None:
            message = str(error)
    elif isinstance(payload, str) and payload:
        message = payload[:200]
    raise RuntimeHttpError(response.status_code, code, message, details)
