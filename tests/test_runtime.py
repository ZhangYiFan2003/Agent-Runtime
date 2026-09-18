from __future__ import annotations

import asyncio
import json
import socket
from io import BytesIO
from typing import Any

import httpx
import pytest

from axiom.config import load_config
from axiom.memory import MemoryKind, MemoryScopeType, MemoryService, SummaryPolicy
from axiom.runtime import DurableTaskManager
from axiom.runtime.api import RuntimeApiServer, RuntimeTurnContext


def test_durable_task_lifecycle(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    task = manager.claim_next()
    assert task is not None
    assert task.id == task_id
    assert task.status == "running"

    manager.complete(task_id, "done")
    completed = manager.get(task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == "done"


def test_durable_task_cancel(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    assert manager.cancel(task_id)
    assert manager.get(task_id).status == "canceled"  # type: ignore[union-attr]


def test_durable_task_ids_are_unique_under_rapid_creation(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")

    task_ids = [manager.add(f"task {index}") for index in range(100)]

    assert len(task_ids) == 100
    assert len(set(task_ids)) == 100
    assert all(task_id.startswith("task_") for task_id in task_ids)
    assert all(len(task_id) == len("task_") + 32 for task_id in task_ids)
    assert len(manager.list(limit=200)) == 100




class FakeHttpRequest:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        api_key: str | None = "test-api-key",
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
    ):
        body = raw_body if raw_body is not None else json.dumps(payload or {}).encode("utf-8")
        self.command = method
        self.path = path
        self.headers = {"content-length": str(len(body))}
        if api_key is not None:
            self.headers["x-api-key"] = api_key
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status: int | None = None
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.response_headers[key.lower()] = value

    def end_headers(self) -> None:
        return

    def json(self) -> dict[str, Any]:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _runtime_server(tmp_path) -> RuntimeApiServer:
    config = load_config(project_root=tmp_path)
    return RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-api-key",
        port=0,
        data_dir=tmp_path / "runtime-data",
        workers=0,
    )


def _handle(server: RuntimeApiServer, request: FakeHttpRequest) -> tuple[int, dict[str, Any]]:
    server._handle(request)
    assert request.status is not None
    return request.status, request.json()


def test_runtime_api_rejects_unauthorized_request(tmp_path):
    server = _runtime_server(tmp_path)
    status, payload = _handle(
        server,
        FakeHttpRequest(method="GET", path="/v1/tasks", api_key=None),
    )

    assert status == 401
    assert payload == {"error": "unauthorized"}


def test_runtime_api_task_create_get_complete_and_cancel(tmp_path):
    server = _runtime_server(tmp_path)

    status, created = _handle(
        server,
        FakeHttpRequest(method="POST", path="/v1/tasks", payload={"message": "do work"}),
    )
    assert status == 200
    assert created["status"] == "queued"
    task_id = created["id"]

    status, queued = _handle(server, FakeHttpRequest(method="GET", path=f"/v1/tasks/{task_id}"))
    assert status == 200
    assert queued["id"] == task_id
    assert queued["status"] == "queued"

    server.task_manager.complete(task_id, "done")
    status, completed = _handle(
        server,
        FakeHttpRequest(method="GET", path=f"/v1/tasks/{task_id}"),
    )
    assert status == 200
    assert completed["status"] == "completed"
    assert completed["result"] == "done"

    cancel_id = server.task_manager.add("cancel me")
    status, canceled = _handle(
        server,
        FakeHttpRequest(method="POST", path=f"/v1/tasks/{cancel_id}/cancel"),
    )
    assert status == 200
    assert canceled == {"canceled": True}
    assert server.task_manager.get(cancel_id).status == "canceled"  # type: ignore[union-attr]


def test_runtime_api_handles_invalid_and_malformed_requests_without_internal_leaks(tmp_path):
    server = _runtime_server(tmp_path)

    cases = [
        FakeHttpRequest(method="GET", path="/v1/tasks/missing"),
        FakeHttpRequest(method="POST", path="/v1/tasks", payload={}),
        FakeHttpRequest(method="POST", path="/v1/tasks", raw_body=b"{"),
        FakeHttpRequest(method="DELETE", path="/v1/tasks"),
    ]
    responses = [_handle(server, request) for request in cases]

    assert responses[0][0] == 404
    assert responses[0][1] == {"error": "not found"}
    assert responses[1][0] == 400
    assert responses[1][1] == {"error": "message is required"}
    assert responses[2][0] == 400
    assert responses[2][1] == {"error": "message is required"}
    assert responses[3][0] == 404
    assert responses[3][1] == {"error": "not found"}

    for _status, payload in responses:
        body = json.dumps(payload)
        assert "Traceback" not in body
        assert ".py" not in body
        assert "test-api-key" not in body


class FakeEngine:
    def __init__(self, text: str = "fake response"):
        self.text = text

    async def ask(self, message: str):
        yield {"type": "text_delta", "text": self.text}
        yield {"type": "tool_call", "name": "fake_tool", "arguments_chars": len(message)}
        yield {"type": "tool_result", "name": "fake_tool", "content_chars": 2}
        yield {"type": "done", "stop_reason": "stop"}

    async def ask_complete_async(self, _prompt: str):
        class Result:
            text = "task response"

        return Result()


def _fake_engine_factory(contexts: list[RuntimeTurnContext]):
    def factory(context: RuntimeTurnContext) -> FakeEngine:
        contexts.append(context)
        return FakeEngine()

    return factory


class FailingEngine:
    async def ask(self, _message: str):
        raise RuntimeError("engine failed")
        yield {}


class FailingMemoryService:
    def history_from_runtime_events(self, events):
        return []

    def save_conversation(self, *_args, **_kwargs):
        raise RuntimeError("memory unavailable")

    def save_tool_result(self, *_args, **_kwargs):
        raise RuntimeError("memory unavailable")


class FailingSummaryMemoryService(FailingMemoryService):
    def save_conversation(self, *_args, **_kwargs):
        return None

    def save_tool_result(self, *_args, **_kwargs):
        return None

    async def summarize_thread(self, _thread_id: str):
        raise RuntimeError("summary unavailable")


def _headers(kind: str = "x-api-key") -> dict[str, str]:
    if kind == "bearer":
        return {"authorization": "Bearer test-api-key"}
    return {"x-api-key": "test-api-key"}


def _api_key_arg() -> dict[str, str]:
    return {"api" + "_key": "test-api-key"}


def _sse_events(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.strip().split("\n\n"):
        if not block:
            continue
        item: dict[str, Any] = {}
        for line in block.splitlines():
            name, _, value = line.partition(": ")
            item[name] = value
        if "data" in item:
            item["data"] = json.loads(str(item["data"]))
        if "id" in item:
            item["id"] = int(item["id"])
        events.append(item)
    return events


def test_runtime_api_live_http_lifecycle_auth_threads_events_and_tasks(tmp_path):
    contexts: list[RuntimeTurnContext] = []
    data_dir = tmp_path / "runtime-data"
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=0,
        data_dir=data_dir,
        engine_factory=_fake_engine_factory(contexts),
    )

    with server.running() as running:
        host, port = running.address
        base_url = f"http://{host}:{port}"
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            health = client.get("/health")
            assert health.status_code == 200
            assert health.json() == {
                "status": "ok",
                "workers": 0,
                "database": "ok",
                "storage_backend": "sqlite",
                "sse": {"active_followers": 0, "reconnects": 0},
            }

            assert client.get("/v1/tasks").status_code == 401
            assert client.get("/v1/tasks", headers={"x-api-key": "wrong"}).status_code == 401
            assert client.get("/v1/tasks", headers=_headers("bearer")).status_code == 200

            created_thread = client.post("/v1/threads", headers=_headers()).json()
            thread_id = created_thread["id"]

            turn = client.post(
                f"/v1/threads/{thread_id}/turns",
                headers=_headers("bearer"),
                json={"message": "hello runtime"},
            )
            assert turn.status_code == 200
            assert turn.json() == {"thread_id": thread_id, "text": "fake response"}

            second_turn = client.post(
                f"/v1/threads/{thread_id}/turns",
                headers=_headers(),
                json={"message": "second question"},
            )
            assert second_turn.status_code == 200

            replay = client.get(f"/v1/threads/{thread_id}/events", headers=_headers())
            assert replay.status_code == 200
            assert replay.headers["content-type"].startswith("text/event-stream")
            events = _sse_events(replay.text)
            ids = [event["id"] for event in events]
            assert ids == sorted(ids)
            assert len(ids) == len(set(ids))
            assert {"thread.created", "turn.started", "user.message", "assistant.message"} <= {
                event["event"] for event in events
            }
            assert {"tool_call", "tool_result", "turn.completed"} <= {
                event["event"] for event in events
            }

            cursor = ids[2]
            after = client.get(
                f"/v1/threads/{thread_id}/events?after_id={cursor}",
                headers=_headers(),
            )
            assert after.status_code == 200
            assert all(event["id"] > cursor for event in _sse_events(after.text))

            non_numeric = client.get(
                f"/v1/threads/{thread_id}/events?after_id=not-a-number",
                headers=_headers(),
            )
            negative = client.get(
                f"/v1/threads/{thread_id}/events?after_id=-1",
                headers=_headers(),
            )
            assert [event["id"] for event in _sse_events(non_numeric.text)] == ids
            assert [event["id"] for event in _sse_events(negative.text)] == ids

            missing_events = client.get("/v1/threads/thread_missing/events", headers=_headers())
            assert missing_events.status_code == 404
            assert missing_events.json() == {"error": "thread not found"}

            task = client.post("/v1/tasks", headers=_headers(), json={"message": "do work"})
            assert task.status_code == 200
            task_id = task.json()["id"]
            listed = client.get("/v1/tasks", headers=_headers()).json()["tasks"]
            assert any(item["id"] == task_id for item in listed)
            fetched = client.get(f"/v1/tasks/{task_id}", headers=_headers())
            assert fetched.status_code == 200
            assert fetched.json()["status"] == "queued"
            canceled = client.post(f"/v1/tasks/{task_id}/cancel", headers=_headers())
            assert canceled.status_code == 200
            assert canceled.json() == {"canceled": True}

            malformed = client.post("/v1/tasks", headers=_headers(), content=b"{")
            assert malformed.status_code == 400
            missing_field = client.post("/v1/tasks", headers=_headers(), json={})
            assert missing_field.status_code == 400
            unknown = client.get("/v1/unknown", headers=_headers())
            assert unknown.status_code == 404

    assert contexts
    assert contexts[0].thread_id == thread_id
    assert contexts[0].message == "hello runtime"
    assert contexts[0].history == []
    assert contexts[1].message == "second question"
    assert [(message.role, message.content) for message in contexts[1].history] == [
        ("user", "hello runtime"),
        ("assistant", "fake response"),
    ]
    assert (data_dir / "runtime.db").exists()
    assert (data_dir / "tasks.db").exists()
    assert (data_dir / "memory.db").exists()
    assert not (tmp_path / "home" / ".axiom").exists()
    assert server._server_thread is None
    assert server._worker_threads == []

    second = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=port,
        workers=0,
        data_dir=tmp_path / "second-data",
        engine_factory=_fake_engine_factory([]),
    )
    with second.running() as restarted:
        assert restarted.address[1] == port


def test_runtime_events_repository_persists_after_server_restart(tmp_path):
    data_dir = tmp_path / "runtime-data"
    first = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=0,
        data_dir=data_dir,
        engine_factory=_fake_engine_factory([]),
    )
    thread_id = first.repository.create_thread()
    first.repository.append_event(thread_id, "user.message", {"text": "persist me"})

    second = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=0,
        data_dir=data_dir,
        engine_factory=_fake_engine_factory([]),
    )
    events = second.repository.list_events(thread_id)

    assert [event.type for event in events] == ["thread.created", "user.message"]
    assert events[0].id < events[1].id


def test_runtime_thread_history_recovers_after_server_restart(tmp_path):
    data_dir = tmp_path / "runtime-data"
    first = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=data_dir,
        engine_factory=_fake_engine_factory([]),
    )
    thread_id = first.repository.create_thread()
    first.repository.append_event(thread_id, "user.message", {"text": "first"})
    first.repository.append_event(thread_id, "tool_result", {"content": "tool-only"})
    first.repository.append_event(thread_id, "assistant.message", {"text": "answer"})
    first.repository.append_event(thread_id, "assistant.message", {"malformed": True})

    contexts: list[RuntimeTurnContext] = []
    second = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=data_dir,
        engine_factory=_fake_engine_factory(contexts),
    )

    asyncio.run(second._run_turn(thread_id, "second"))

    assert [(message.role, message.content) for message in contexts[0].history] == [
        ("user", "first"),
        ("assistant", "answer"),
    ]


def test_runtime_thread_history_isolated_between_threads(tmp_path):
    contexts: list[RuntimeTurnContext] = []
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory(contexts),
    )
    first = server.repository.create_thread()
    second = server.repository.create_thread()
    server.repository.append_event(first, "user.message", {"text": "first thread only"})

    asyncio.run(server._run_turn(second, "second thread message"))

    assert contexts[0].history == []


def test_runtime_failed_turn_preserves_user_message_without_assistant(tmp_path):
    def failing_factory(_context: RuntimeTurnContext) -> FailingEngine:
        return FailingEngine()

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=failing_factory,
    )
    thread_id = server.repository.create_thread()

    with pytest.raises(RuntimeError):
        asyncio.run(server._run_turn(thread_id, "keep this"))

    events = server.repository.list_events(thread_id)
    assert "user.message" in [event.type for event in events]
    assert "error" in [event.type for event in events]
    assert "assistant.message" not in [event.type for event in events]


def test_runtime_memory_derivation_failure_does_not_fail_turn(tmp_path):
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory([]),
        memory_service=FailingMemoryService(),
    )
    thread_id = server.repository.create_thread()

    result = asyncio.run(server._run_turn(thread_id, "hello"))

    assert result == {"thread_id": thread_id, "text": "fake response"}
    assert {"user.message", "assistant.message", "tool_result"} <= {
        event.type for event in server.repository.list_events(thread_id)
    }


def test_runtime_summary_failure_does_not_fail_completed_turn(tmp_path):
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory([]),
        memory_service=FailingSummaryMemoryService(),
    )
    thread_id = server.repository.create_thread()

    result = asyncio.run(server._run_turn(thread_id, "hello"))

    assert result == {"thread_id": thread_id, "text": "fake response"}
    assert "turn.completed" in [event.type for event in server.repository.list_events(thread_id)]


def test_runtime_turn_completion_creates_summary_checkpoint(tmp_path):
    service = MemoryService(
        tmp_path / "runtime-data" / "memory.db",
        project_scope=str(tmp_path),
        summary_policy=SummaryPolicy(
            threshold_messages=6,
            minimum_unsummarized_messages=4,
            recent_message_reserve=2,
        ),
    )
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory([]),
        memory_service=service,
    )
    thread_id = server.repository.create_thread()

    for index in range(4):
        asyncio.run(server._run_turn(thread_id, f"message {index}"))

    active = service.store.active_summary(thread_id=thread_id)
    summaries = service.list_records(
        kind=MemoryKind.SUMMARY,
        scope_type=MemoryScopeType.THREAD,
        scope_id=thread_id,
        include_inactive=True,
    )

    assert active is not None
    assert active.source_event_end_id is not None
    assert any(record.metadata.get("summary_stage") == "map" for record in summaries)


def test_runtime_rejects_concurrent_turn_on_same_thread(tmp_path):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    lock = server._thread_lock(thread_id)
    assert lock.acquire(blocking=False)
    try:
        status, payload = _handle(
            server,
            FakeHttpRequest(
                method="POST",
                path=f"/v1/threads/{thread_id}/turns",
                payload={"message": "blocked"},
            ),
        )
    finally:
        lock.release()

    assert status == 409
    assert payload == {"error": "thread turn already running"}


def test_runtime_thread_lock_releases_after_handler_error(tmp_path):
    attempts = 0

    def factory(_context: RuntimeTurnContext):
        nonlocal attempts
        attempts += 1
        return FailingEngine() if attempts == 1 else FakeEngine()

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=factory,
    )
    thread_id = server.repository.create_thread()

    failed_status, _failed = _handle(
        server,
        FakeHttpRequest(
            method="POST",
            path=f"/v1/threads/{thread_id}/turns",
            payload={"message": "first"},
        ),
    )
    ok_status, ok_payload = _handle(
        server,
        FakeHttpRequest(
            method="POST",
            path=f"/v1/threads/{thread_id}/turns",
            payload={"message": "second"},
        ),
    )

    assert failed_status == 500
    assert ok_status == 200
    assert ok_payload["text"] == "fake response"


def test_runtime_thread_locks_are_per_thread(tmp_path):
    contexts: list[RuntimeTurnContext] = []
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        **_api_key_arg(),
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory(contexts),
    )
    first = server.repository.create_thread()
    second = server.repository.create_thread()
    first_lock = server._thread_lock(first)
    assert first_lock.acquire(blocking=False)
    try:
        status, payload = _handle(
            server,
            FakeHttpRequest(
                method="POST",
                path=f"/v1/threads/{second}/turns",
                payload={"message": "different thread"},
            ),
        )
    finally:
        first_lock.release()

    assert status == 200
    assert payload["thread_id"] == second


def test_runtime_tasks_persist_after_server_restart(tmp_path):
    data_dir = tmp_path / "runtime-data"
    first = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=0,
        data_dir=data_dir,
        engine_factory=_fake_engine_factory([]),
    )
    task_id = first.task_manager.add("persist task")
    first.task_manager.cancel(task_id)

    second = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=0,
        data_dir=data_dir,
        engine_factory=_fake_engine_factory([]),
    )
    restored = second.task_manager.get(task_id)

    assert restored is not None
    assert restored.status == "canceled"


def test_runtime_shutdown_is_idempotent_when_not_started_or_already_closed(tmp_path):
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=1,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory([]),
    )

    server.shutdown()
    server.start()
    assert server._worker_threads
    server.shutdown()
    server.shutdown()

    assert server._httpd is None
    assert server._server_thread is None
    assert server._worker_threads == []


def test_runtime_running_context_closes_after_exception(tmp_path):
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=1,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory([]),
    )

    with pytest.raises(RuntimeError), server.running() as running:
        host, port = running.address
        assert port > 0
        raise RuntimeError("boom")

    assert server._httpd is None
    assert server._server_thread is None
    assert server._worker_threads == []
    with pytest.raises(OSError):
        socket.create_connection((host, port), timeout=0.25)


def test_runtime_live_server_releases_socket_after_shutdown(tmp_path):
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory([]),
    )
    server.start()
    host, port = server.address
    server.shutdown()
    server.shutdown()

    with pytest.raises(OSError):
        socket.create_connection((host, port), timeout=0.25)
