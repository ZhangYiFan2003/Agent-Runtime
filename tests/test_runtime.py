from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from io import BytesIO
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from axiom.config import load_config
from axiom.memory import MemoryKind, MemoryScopeType, MemoryService, SummaryPolicy
from axiom.runtime import DurableTaskManager
from axiom.runtime.api import RuntimeApiServer, RuntimeTurnContext
from axiom.runtime.models import Checkpoint, RunStatus


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


def _runtime_server(tmp_path, *, workers: int = 0) -> RuntimeApiServer:
    config = load_config(project_root=tmp_path)
    return RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-api-key",
        port=0,
        data_dir=tmp_path / "runtime-data",
        workers=workers,
    )


def _handle(server: RuntimeApiServer, request: FakeHttpRequest) -> tuple[int, dict[str, Any]]:
    server._handle(request)
    assert request.status is not None
    return request.status, request.json()


def _submit_request(thread_id: str, message: str, key: str) -> FakeHttpRequest:
    request = FakeHttpRequest(
        method="POST", path=f"/v1/threads/{thread_id}/turns", payload={"message": message}
    )
    request.headers["Idempotency-Key"] = key
    return request


def _page_turn(server, thread_id, index):
    state = Checkpoint.create(thread_id=thread_id, input=f"question {index}")
    state.created_at = "2026-10-19T00:00:00+00:00"
    state, _ = asyncio.run(server.checkpoint_store.admit_submission(
        state, idempotency_key=f"page-{index}", request_fingerprint=f"question {index}",
    ))
    state.status = RunStatus.COMPLETED
    state.output_text = f"answer {index}"
    asyncio.run(server.checkpoint_store.save(state))
    return state


def _get_page(server, path, **params):
    return _handle(server, FakeHttpRequest(method="GET", path=f"{path}?{urlencode(params)}"))


def test_runtime_message_pages_survive_logs_and_snapshot_increment_boundary(tmp_path):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    for index in range(32):
        _page_turn(server, thread_id, index)
    with server.repository._connect() as conn:
        conn.execute("update events set created_at = '2026-10-19T00:00:00+00:00'")
        conn.executemany(
            "insert into events(thread_id,type,payload,created_at) values (?, ?, ?, ?)",
            [(thread_id, "tool.delta", "{}", "2026-10-19T00:00:00+00:00")] * 1601,
        )
    path = f"/v1/threads/{thread_id}/messages"
    status, initial = _get_page(server, path)
    assert status == 200 and len(initial["messages"]) == 50
    assert initial["messages"][0]["text"] == "question 7"
    assert initial["messages"][-1]["text"] == "answer 31"
    assert initial["has_more"] and initial["older_cursor"]
    # A message committed between the snapshot and its first incremental read is visible.
    added = _page_turn(server, thread_id, 32)
    added.created_at = "2026-10-20T00:00:00+00:00"
    asyncio.run(server.checkpoint_store.save(added))
    _, older = _get_page(server, path, before=initial["older_cursor"], limit=7)
    _, oldest = _get_page(server, path, before=older["older_cursor"], limit=7)
    assert len(older["messages"]) == len(oldest["messages"]) == 7
    assert not oldest["has_more"] and oldest["older_cursor"] is None
    combined = oldest["messages"] + older["messages"] + initial["messages"]
    assert [row["text"] for row in combined] == [
        value for index in range(32) for value in (f"question {index}", f"answer {index}")
    ]
    assert len({row["id"] for row in combined}) == 64
    _, incremental = _get_page(server, path, after=initial["after_cursor"])
    assert [row["text"] for row in incremental["messages"]] == ["question 32", "answer 32"]
    assert incremental["current_run"] == {"run_id": added.run_id, "status": "COMPLETED"}
    _, empty = _get_page(server, path, after=incremental["after_cursor"])
    assert empty["messages"] == [] and not empty["has_more"]
    assert empty["after_cursor"] == incremental["after_cursor"]
    pending, _ = asyncio.run(server.checkpoint_store.admit_submission(
        Checkpoint.create(thread_id=thread_id, input="pending question"),
        idempotency_key="pending", request_fingerprint="pending question",
    ))
    _, new = _get_page(server, path, after=empty["after_cursor"])
    assert [row["text"] for row in new["messages"]] == ["pending question"]
    assert new["current_run"] == {"run_id": pending.run_id, "status": "RUNNING"}


def test_runtime_event_pages_and_follow_drain_every_page(tmp_path, monkeypatch):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    state = _page_turn(server, thread_id, 0)
    for index in range(19):
        server.repository.append_event(thread_id, "tool.delta", {
            "run_id": state.run_id, "turn_id": state.turn_id, "text": str(index),
        })
    server.repository.append_event(thread_id, "artifact.created", {
        "run_id": state.run_id, "turn_id": state.turn_id, "artifact_id": "artifact-final",
    })
    path = f"/v1/threads/{thread_id}/events"
    expected = [row.id for row in server.repository.list_events(thread_id)]
    seen = []
    params = {"limit": 3}
    while True:
        request = FakeHttpRequest(method="GET", path=f"{path}?{urlencode(params)}")
        server._handle(request)
        assert request.status == 200
        rows = _sse_events(request.wfile.getvalue().decode())
        assert 0 < len(rows) <= 3
        seen.extend(row["id"] for row in rows)
        headers = request.response_headers
        assert int(headers["x-axiom-events-watermark"]) >= seen[-1]
        if headers["x-axiom-events-has-more"] == "false":
            assert headers["x-axiom-events-next-cursor"] == ""
            break
        params = {"limit": 3, "cursor": headers["x-axiom-events-next-cursor"]}
    assert seen == expected
    tail = FakeHttpRequest(method="GET", path=f"{path}?latest=1&limit=3")
    server._handle(tail)
    assert [row["id"] for row in _sse_events(tail.wfile.getvalue().decode())] == expected[-3:]
    assert tail.response_headers["x-axiom-events-has-older"] == "true"
    assert tail.response_headers["x-axiom-events-has-more"] == "false"
    empty = FakeHttpRequest(method="GET", path=f"{path}?after_id={expected[-1]}&limit=3")
    server._handle(empty)
    assert empty.wfile.getvalue() == b""
    assert empty.response_headers["x-axiom-events-after-id"] == str(expected[-1])
    assert empty.response_headers["x-axiom-events-watermark"] == str(expected[-1])
    calls = []
    original = server.repository.page_events

    def bounded(*args, **kwargs):
        calls.append(kwargs["limit"])
        return original(*args, **kwargs)

    monkeypatch.setattr(server.repository, "page_events", bounded)
    server.sse_poll_interval = 0.001
    follow = FakeHttpRequest(
        method="GET", path=f"{path}?follow=1&run_id={state.run_id}&limit=3"
    )
    server._handle(follow)
    assert [row["id"] for row in _sse_events(follow.wfile.getvalue().decode())] == [
        row.id for row in server.repository.list_events(thread_id, run_id=state.run_id)
    ]
    assert {"artifact.created", "assistant.message", "turn.completed"} <= {
        row["event"] for row in _sse_events(follow.wfile.getvalue().decode())
    }
    assert len(calls) > 3 and set(calls) == {3}


@pytest.mark.parametrize("endpoint", ["messages", "events", "runs"])
@pytest.mark.parametrize("parameter", [
    "limit=0", "limit=-1", "limit=x", "limit=501", "limit=1&limit=2",
])
def test_runtime_read_pages_reject_invalid_limits(tmp_path, endpoint, parameter):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    path = "/v1/runs" if endpoint == "runs" else f"/v1/threads/{thread_id}/{endpoint}"
    status, result = _handle(server, FakeHttpRequest(method="GET", path=f"{path}?{parameter}"))
    assert status == 400 and result["error"]["code"] == "invalid_request"


def test_runtime_read_cursors_bind_kind_thread_and_filters(tmp_path):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    other = server.repository.create_thread()
    state = _page_turn(server, thread_id, 0)
    _, messages = _get_page(server, f"/v1/threads/{thread_id}/messages", limit=1)
    _page_turn(server, thread_id, 1)
    _, runs = _get_page(server, "/v1/runs", limit=1)
    event = FakeHttpRequest(method="GET", path=f"/v1/threads/{thread_id}/events?limit=1")
    server._handle(event)
    cursor = event.response_headers["x-axiom-events-next-cursor"]
    cases = [
        (f"/v1/threads/{other}/messages", {"before": messages["older_cursor"]}),
        (f"/v1/threads/{thread_id}/messages", {"before": cursor}),
        (f"/v1/threads/{thread_id}/messages", {"before": "bad"}),
        (f"/v1/threads/{thread_id}/messages", {"before": ""}),
        (f"/v1/threads/{thread_id}/messages", {
            "before": messages["older_cursor"], "after": messages["after_cursor"],
        }),
        (f"/v1/threads/{other}/events", {"cursor": cursor}),
        (f"/v1/threads/{thread_id}/events", {"cursor": cursor, "run_id": state.run_id}),
        (f"/v1/threads/{thread_id}/events", {"cursor": cursor, "after_id": 1}),
        (f"/v1/threads/{thread_id}/events", {"latest": 1, "after_id": 1}),
        ("/v1/runs", {"cursor": runs["next_cursor"], "status": "FAILED"}),
        ("/v1/runs", {"cursor": messages["after_cursor"]}),
        ("/v1/runs", {"cursor": "bad"}),
        ("/v1/runs", {"sort": "bogus"}),
    ]
    for path, params in cases:
        status, result = _get_page(server, path, **params)
        assert status == 400, (path, params, result)
        assert result["error"]["code"] == "invalid_request"
    for sort in ("duration", "status"):
        malformed = base64.urlsafe_b64encode(json.dumps({
            "v": 1, "kind": "runs", "scope": ["", "", "", sort],
            "position": [10 ** 400, "2026-10-19T00:00:00+00:00", state.run_id],
        }).encode()).decode().rstrip("=")
        status, result = _get_page(server, "/v1/runs", sort=sort, cursor=malformed)
        assert status == 400 and result["error"]["code"] == "invalid_request"


def test_runtime_runs_summary_pages_do_not_use_full_run_views(tmp_path, monkeypatch):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    states = [_page_turn(server, thread_id, index) for index in range(7)]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("list endpoint read complete Run state")

    monkeypatch.setattr(server, "_all_runs", forbidden)
    monkeypatch.setattr(server, "_run_view", forbidden)
    expected = sorted((state.run_id for state in states), reverse=True)
    _, first = _get_page(server, "/v1/runs", limit=3)
    assert [row["run_id"] for row in first["runs"]] == expected[:3]
    assert "output" not in first["runs"][0] and "checkpoint" not in first["runs"][0]
    inserted = _page_turn(server, server.repository.create_thread(), "new")
    inserted.created_at = "2026-10-20T00:00:00+00:00"
    asyncio.run(server.checkpoint_store.save(inserted))
    _, second = _get_page(server, "/v1/runs", limit=3, cursor=first["next_cursor"])
    _, last = _get_page(server, "/v1/runs", limit=3, cursor=second["next_cursor"])
    assert [row["run_id"] for row in second["runs"] + last["runs"]] == expected[3:]
    assert last["next_cursor"] is None and not last["has_more"]
    _, filtered = _get_page(server, "/v1/runs", search=expected[-1], limit=1)
    assert [row["run_id"] for row in filtered["runs"]] == [expected[-1]]


@pytest.mark.parametrize("same_key", [False, True])
def test_runtime_concurrent_submissions_start_one_execution(tmp_path, same_key):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    prepared = threading.Barrier(2, timeout=5)
    release = threading.Event()
    executions: list[str] = []

    class BlockingEngine:
        async def ask(self, message):
            executions.append(message)
            assert await asyncio.to_thread(release.wait, 5)
            yield {"type": "text_delta", "text": "one answer"}
            yield {"type": "done"}

    def factory(_context):
        prepared.wait()
        return BlockingEngine()

    server.engine_factory = factory
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [
            _submit_request(thread_id, "one question", "key-1"),
            _submit_request(thread_id, "one question", "key-1" if same_key else "key-2"),
        ]
        futures = [pool.submit(_handle, server, request) for request in requests]
        try:
            done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert done, "replay/rejection should return while the original execution is blocked"
            early_status, early = next(iter(done)).result()
            assert early_status == (202 if same_key else 409)
        finally:
            release.set()
        results = [future.result(timeout=5) for future in futures]

    accepted = next(payload for status, payload in results if status == 200)
    if same_key:
        assert early["run_id"] == accepted["run_id"]
        assert early["turn_id"] == accepted["turn_id"]
        assert early["status"] == "RUNNING"
    else:
        assert early["error"]["code"] == "THREAD_BUSY"
        assert early["error"]["details"]["active_run_id"] == accepted["run_id"]
    assert executions == ["one question"]
    assert len(asyncio.run(server.checkpoint_store.list(thread_id))) == 1
    events = server.repository.list_events(thread_id)
    for event_type in ("turn.started", "user.message", "assistant.message", "turn.completed"):
        matching = [event for event in events if event.type == event_type]
        assert len(matching) == 1
        assert matching[0].run_id == accepted["run_id"]
        assert matching[0].turn_id == accepted["turn_id"]


def test_runtime_replay_finds_completed_run_before_initialization_and_limits(tmp_path):
    server = _runtime_server(tmp_path)
    server.config.traffic.global_submission_rate = 0.001
    server.config.traffic.global_submission_burst = 1
    contexts: list[RuntimeTurnContext] = []
    server.engine_factory = _fake_engine_factory(contexts)
    thread_id = server.repository.create_thread()
    status, accepted = _handle(server, _submit_request(thread_id, "original", "stable-key"))
    assert status == 200
    events = server.repository.list_events(thread_id)
    other_thread = server.repository.create_thread()
    status, rejected = _handle(server, _submit_request(other_thread, "other", "new-key"))
    assert status == 429
    assert rejected["error"]["code"] == "RATE_LIMITED"
    assert asyncio.run(server.checkpoint_store.list(other_thread)) == []

    def unavailable(_context):
        raise AssertionError("a replay must not initialize or execute an engine")

    server.engine_factory = unavailable
    status, replay = _handle(server, _submit_request(thread_id, "original", "stable-key"))
    assert status == 200
    assert replay["run_id"] == accepted["run_id"]
    assert replay["turn_id"] == accepted["turn_id"]
    assert replay["status"] == "COMPLETED"
    assert replay["text"] == "fake response"
    assert server.repository.list_events(thread_id) == events
    status, conflict = _handle(server, _submit_request(thread_id, "changed", "stable-key"))
    assert status == 409
    assert conflict["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert len(asyncio.run(server.checkpoint_store.list(thread_id))) == 1


def test_runtime_unaccepted_initialization_failure_and_validation_leave_no_facts(tmp_path):
    server = _runtime_server(tmp_path)
    thread_id = server.repository.create_thread()
    before = server.repository.list_events(thread_id)

    def unavailable(_context):
        raise ValueError("test preparation failed")

    server.engine_factory = unavailable
    status, rejected = _handle(server, _submit_request(thread_id, "original", "reusable-key"))
    assert status == 503
    assert rejected["error"]["code"] == "SUBMISSION_INITIALIZATION_FAILED"
    invalid = _submit_request(thread_id, "", "reusable-key")
    assert _handle(server, invalid)[0] == 400
    assert asyncio.run(server.checkpoint_store.list(thread_id)) == []
    assert server.repository.list_events(thread_id) == before
    server.engine_factory = _fake_engine_factory([])
    status, accepted = _handle(server, _submit_request(thread_id, "original", "reusable-key"))
    assert status == 200
    assert accepted["status"] == "COMPLETED"


def test_runtime_accepted_execution_failure_is_replayed_without_reexecution(tmp_path):
    server = _runtime_server(tmp_path)
    calls: list[str] = []

    def factory(context):
        calls.append(context.message)
        return FailingEngine()

    server.engine_factory = factory
    thread_id = server.repository.create_thread()
    status, _ = _handle(server, _submit_request(thread_id, "failing", "accepted-key"))
    assert status == 500
    status, replay = _handle(server, _submit_request(thread_id, "failing", "accepted-key"))
    assert status == 200
    assert replay["status"] == "FAILED"
    assert replay["run_id"]
    assert replay["turn_id"]
    assert calls == ["failing"]
    assert len(asyncio.run(server.checkpoint_store.list(thread_id))) == 1


def test_runtime_different_threads_execute_without_a_global_execution_lock(tmp_path):
    server = _runtime_server(tmp_path)
    reached_model = threading.Barrier(2, timeout=5)

    class ConcurrentEngine:
        async def ask(self, message):
            await asyncio.to_thread(reached_model.wait)
            yield {"type": "text_delta", "text": message}
            yield {"type": "done"}

    server.engine_factory = lambda _context: ConcurrentEngine()
    threads = [server.repository.create_thread(), server.repository.create_thread()]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda thread_id: _handle(server, _submit_request(thread_id, "parallel", "key")),
            threads,
        ))
    assert [status for status, _ in results] == [200, 200]
    assert results[0][1]["run_id"] != results[1][1]["run_id"]


def test_runtime_completion_callback_repair_and_next_history_use_same_run(tmp_path):
    server = _runtime_server(tmp_path)
    contexts: list[RuntimeTurnContext] = []
    server.engine_factory = _fake_engine_factory(contexts)
    thread_id = server.repository.create_thread()
    _, result = _handle(server, _submit_request(thread_id, "first", "first-key"))
    state = asyncio.run(server.checkpoint_store.load(result["run_id"]))
    assert state is not None
    before = server.repository.list_events(thread_id)
    asyncio.run(server._finish_durable_turn(state))
    assert server.repository.list_events(thread_id) == before

    # A previous binary could have committed COMPLETED without projecting its answer.
    with server.checkpoint_store._connect() as conn:
        conn.execute(
            "delete from events where run_id = ? "
            "and type in ('assistant.message', 'turn.completed')",
            (state.run_id,),
        )
    status, second = _handle(server, _submit_request(thread_id, "second", "second-key"))
    assert status == 200
    assert [(message.role, message.content) for message in contexts[-1].history] == [
        ("user", "first"), ("assistant", "fake response"),
    ]
    repaired = server.repository.list_events(thread_id)
    for event_type in ("assistant.message", "turn.completed"):
        assert len([event for event in repaired
                    if event.run_id == state.run_id and event.type == event_type]) == 1
    assert second["run_id"] != state.run_id


def test_runtime_api_rejects_unauthorized_request(tmp_path):
    server = _runtime_server(tmp_path)
    status, payload = _handle(
        server,
        FakeHttpRequest(method="GET", path="/v1/tasks", api_key=None),
    )

    assert status == 401
    assert payload == {"error": "unauthorized"}


@pytest.mark.parametrize("header", ["x-api-key", "authorization"])
@pytest.mark.parametrize("api_key", ["test-api-key", "wrong", "", None])
def test_runtime_auth_check_uses_existing_auth_without_creating_work(tmp_path, header, api_key):
    server = _runtime_server(tmp_path)
    request = FakeHttpRequest(method="GET", path="/v1/auth/check", api_key=None)
    if api_key is not None:
        request.headers[header] = f"Bearer {api_key}" if header == "authorization" else api_key

    status, payload = _handle(server, request)

    if api_key == "test-api-key":
        assert status == 200
        assert payload == {"authenticated": True}
    else:
        assert status == 401
        assert payload == {"error": "unauthorized"}
    assert server.task_manager.list() == []
    assert server.repository.list_threads() == []


@pytest.mark.parametrize("api_key", ["test-api-key", "wrong", "", None])
def test_runtime_disabled_task_workers_reject_before_writing(tmp_path, api_key):
    server = _runtime_server(tmp_path)
    existing_id = server.task_manager.add("existing task")
    before = server.task_manager.list()
    request = FakeHttpRequest(
        method="POST", path="/v1/tasks", api_key=api_key, payload={"message": "new task"}
    )

    status, payload = _handle(server, request)

    if api_key == "test-api-key":
        assert status == 503
        assert payload["error"]["code"] == "TASK_WORKERS_DISABLED"
        assert "Thread/Run" in payload["error"]["message"]
        assert "retry-after" not in request.response_headers
    else:
        assert status == 401
        assert payload == {"error": "unauthorized"}
    assert server.task_manager.list() == before

    status, listed = _handle(server, FakeHttpRequest(method="GET", path="/v1/tasks"))
    assert status == 200
    assert [task["id"] for task in listed["tasks"]] == [existing_id]
    status, existing = _handle(
        server, FakeHttpRequest(method="GET", path=f"/v1/tasks/{existing_id}")
    )
    assert status == 200
    assert existing["status"] == "queued"
    status, canceled = _handle(
        server, FakeHttpRequest(method="POST", path=f"/v1/tasks/{existing_id}/cancel")
    )
    assert status == 200
    assert canceled == {"canceled": True}


def test_runtime_api_task_create_get_complete_and_cancel(tmp_path):
    server = _runtime_server(tmp_path, workers=1)

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
    server = _runtime_server(tmp_path, workers=1)

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


def test_runtime_api_enabled_task_worker_consumes_http_submission(tmp_path):
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=1,
        data_dir=tmp_path / "runtime-data",
        engine_factory=_fake_engine_factory([]),
    )
    with server.running() as running:
        host, port = running.address
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=5.0) as client:
            submitted = client.post(
                "/v1/tasks", headers=_headers(), json={"message": "do work"}
            )
            assert submitted.status_code == 200
            assert submitted.json()["status"] == "queued"
            task_id = submitted.json()["id"]
            deadline = time.monotonic() + 3
            while True:
                response = client.get(f"/v1/tasks/{task_id}", headers=_headers())
                assert response.status_code == 200
                task = response.json()
                if task["status"] not in {"queued", "running"} or time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            assert task["status"] == "completed"
            assert task["result"] == "task response"


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
            assert client.get("/v1/auth/check").status_code == 401
            assert client.get("/v1/auth/check", headers={"x-api-key": "wrong"}).status_code == 401
            verified = client.get("/v1/auth/check", headers=_headers("bearer"))
            assert verified.status_code == 200
            assert verified.json() == {"authenticated": True}

            created_thread = client.post("/v1/threads", headers=_headers()).json()
            thread_id = created_thread["id"]

            turn = client.post(
                f"/v1/threads/{thread_id}/turns",
                headers=_headers("bearer"),
                json={"message": "hello runtime"},
            )
            assert turn.status_code == 200
            assert turn.json()["thread_id"] == thread_id
            assert turn.json()["text"] == "fake response"
            assert turn.json()["status"] == "COMPLETED"
            assert turn.json()["run_id"]
            assert turn.json()["turn_id"]

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
            for invalid in (non_numeric, negative):
                assert invalid.status_code == 400
                assert invalid.json()["error"]["code"] == "invalid_request"

            missing_events = client.get("/v1/threads/thread_missing/events", headers=_headers())
            assert missing_events.status_code == 404
            assert missing_events.json() == {"error": "thread not found"}

            task = client.post("/v1/tasks", headers=_headers(), json={"message": "do work"})
            assert task.status_code == 503
            assert task.json()["error"]["code"] == "TASK_WORKERS_DISABLED"
            assert server.task_manager.list() == []
            task_id = server.task_manager.add("existing task")
            listed = client.get("/v1/tasks", headers=_headers()).json()["tasks"]
            assert any(item["id"] == task_id for item in listed)
            fetched = client.get(f"/v1/tasks/{task_id}", headers=_headers())
            assert fetched.status_code == 200
            assert fetched.json()["status"] == "queued"
            canceled = client.post(f"/v1/tasks/{task_id}/cancel", headers=_headers())
            assert canceled.status_code == 200
            assert canceled.json() == {"canceled": True}

            malformed = client.post("/v1/tasks", headers=_headers(), content=b"{")
            assert malformed.status_code == 503
            missing_field = client.post("/v1/tasks", headers=_headers(), json={})
            assert missing_field.status_code == 503
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
    previous = Checkpoint.create(thread_id=thread_id, input="first")
    asyncio.run(first.checkpoint_store.save(previous))
    previous.status = RunStatus.COMPLETED
    previous.output_text = "answer"
    asyncio.run(first.checkpoint_store.save(previous))
    first.repository.append_event(thread_id, "user.message", {
        "text": "first", "run_id": previous.run_id, "turn_id": previous.turn_id,
    })
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

    assert result["thread_id"] == thread_id
    assert result["text"] == "fake response"
    assert result["status"] == "COMPLETED"
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

    assert result["thread_id"] == thread_id
    assert result["text"] == "fake response"
    assert result["status"] == "COMPLETED"
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
    server.engine_factory = _fake_engine_factory([])
    active = Checkpoint.create(thread_id=thread_id, input="active")
    asyncio.run(server.checkpoint_store.save(active))
    before = server.repository.list_events(thread_id)
    status, payload = _handle(
        server,
        FakeHttpRequest(
            method="POST",
            path=f"/v1/threads/{thread_id}/turns",
            payload={"message": "blocked"},
        ),
    )

    assert status == 409
    assert payload["error"]["code"] == "THREAD_BUSY"
    assert payload["error"]["details"]["active_run_id"] == active.run_id
    assert server.repository.list_events(thread_id) == before
    assert len(asyncio.run(server.checkpoint_store.list(thread_id))) == 1


def test_runtime_thread_is_available_after_failed_execution(tmp_path):
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


def test_runtime_other_thread_can_submit_while_one_thread_is_occupied(tmp_path):
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
    active = Checkpoint.create(thread_id=first, input="active")
    asyncio.run(server.checkpoint_store.save(active))
    status, payload = _handle(
        server,
        FakeHttpRequest(
            method="POST",
            path=f"/v1/threads/{second}/turns",
            payload={"message": "different thread"},
        ),
    )

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
