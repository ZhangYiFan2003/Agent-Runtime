from __future__ import annotations

import argparse
import json
import os
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
    payload = {"choices": [{"delta": delta, "finish_reason": finish_reason}]}
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


def _tool_call(name: str, arguments: dict[str, Any], call_id: str) -> list[bytes]:
    return [
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(arguments, separators=(",", ":")),
                        },
                    }
                ]
            },
            "tool_calls",
        ),
        b"data: [DONE]\n\n",
    ]


def _last_user(messages: list[dict[str, Any]]) -> str:
    for item in reversed(messages):
        if item.get("role") == "user":
            content = item.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def _tool_result(messages: list[dict[str, Any]]) -> str | None:
    for item in reversed(messages):
        if item.get("role") == "tool":
            return str(item.get("content") or "")
    return None


def _response_for(payload: dict[str, Any]) -> list[bytes]:
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    prompt = _last_user(messages)
    result = _tool_result(messages)
    if result is not None:
        marker = re.search(r"\[claim:clm_[A-Za-z0-9_-]+\]", result)
        suffix = f" {marker.group(0)}" if marker else ""
        return [
            _chunk({"content": f"stage15 tool result recorded{suffix}"}, "stop"),
            b"data: [DONE]\n\n",
        ]
    if "STAGE15:SHELL" in prompt:
        return _tool_call(
            "bash",
            {"command": "sleep 4; printf stage15", "timeout": 10},
            "call_stage15_shell",
        )
    if "STAGE15:ARTIFACT" in prompt:
        return _tool_call(
            "publish_artifact",
            {"path": "stage15.txt", "name": "stage15.txt", "media_type": "text/plain"},
            "call_stage15_artifact",
        )
    if "STAGE15:PROVENANCE" in prompt:
        return _tool_call(
            "record_claim",
            {
                "text": "The Stage15 fixture contains deterministic evidence.",
                "evidence": [
                    {"type": "code_location", "path": "stage15.txt", "start_line": 1, "end_line": 1}
                ],
            },
            "call_stage15_claim",
        )
    return [_chunk({"content": "stage15 deterministic response"}, "stop"), b"data: [DONE]\n\n"]


class Handler(BaseHTTPRequestHandler):
    server_version = "AxiomStage15FakeProvider/1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._json(
            HTTPStatus.OK, {"status": "ok", "mode": os.getenv("STAGE15_PROVIDER_MODE", "healthy")}
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        mode = os.getenv("STAGE15_PROVIDER_MODE", "healthy")
        latency_ms = max(0, min(10_000, int(os.getenv("STAGE15_PROVIDER_LATENCY_MS", "25"))))
        prompt = _last_user(payload.get("messages") or [])
        if "STAGE15:SLOW" in prompt:
            latency_ms = max(latency_ms, 4_000)
        time.sleep(latency_ms / 1000)
        if mode == "429":
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.send_header("retry-after", "1")
            self.end_headers()
            return
        if mode == "500":
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        chunks = _response_for(payload)
        if mode == "partial":
            chunks = [_chunk({"content": "partial"})]
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8060)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
