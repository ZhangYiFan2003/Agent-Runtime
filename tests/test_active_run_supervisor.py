from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.runtime import (
    ActiveRunRegistrationError,
    ActiveRunSupervisor,
    Checkpoint,
    DurableAgentRuntime,
    ExecutionHandle,
    RunStatus,
    SQLiteCheckpointStore,
)
from axiom.runtime.api import RuntimeApiServer, RuntimeTurnContext
from axiom.tools import ToolRegistry


class BlockingClient:
    provider_name = "supervisor-test"
    model_name = "blocking-model"
    max_context_window = 10_000

    def __init__(self) -> None:
        self.started = threading.Event()

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        self.started.set()
        await asyncio.Future()
        yield {"type": "message_end", "stop_reason": "end_turn"}


class CompletingClient:
    provider_name = "supervisor-test"
    model_name = "completing-model"
    max_context_window = 10_000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class FailingClient(CompletingClient):
    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        yield {"type": "error", "error": RuntimeError("expected failure")}


class CompletionBarrierStore(SQLiteCheckpointStore):
    def __init__(self, db_path: Path) -> None:
        super().__init__(db_path)
        self.completion_ready = threading.Event()
        self.release_completion = threading.Event()
        self.stale_save_finished = threading.Event()
        self._blocked_once = False

    def _save(self, checkpoint: Checkpoint) -> None:
        if checkpoint.status == RunStatus.COMPLETED and not self._blocked_once:
            self._blocked_once = True
            self.completion_ready.set()
            self.release_completion.wait(3)
            try:
                super()._save(checkpoint)
            finally:
                self.stale_save_finished.set()
            return
        super()._save(checkpoint)


class FakeRequest:
    def __init__(
        self,
        method: str = "GET",
        path: str = "/v1/runtime/active-runs",
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        body = json.dumps(payload or {}).encode()
        self.command = method
        self.path = path
        self.headers = {"content-length": str(len(body)), "x-api-key": "test-key"}
        if idempotency_key:
            self.headers["Idempotency-Key"] = idempotency_key
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status = 0

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _name: str, _value: str) -> None:
        return

    def end_headers(self) -> None:
        return

    def json(self) -> dict[str, Any]:
        return json.loads(self.wfile.getvalue())


def _config(tmp_path: Path) -> AxiomConfig:
    config = AxiomConfig()
    config.policy.hitl_mode = "never"
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return config


def _runtime(
    tmp_path: Path,
    store: SQLiteCheckpointStore,
    client: BlockingClient,
    supervisor: ActiveRunSupervisor,
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=client,
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=_config(tmp_path),
        store=store,
        active_run_supervisor=supervisor,
    )


def _handle(run_id: str, task: asyncio.Task[Any]) -> ExecutionHandle:
    return ExecutionHandle(
        run_id=run_id,
        thread_id="thread-test",
        turn_id="turn-test",
        parent_run_id=None,
        run_kind="agent",
        event_loop=asyncio.get_running_loop(),
        asyncio_task=task,
        owner_thread_id=threading.get_ident(),
        metadata={"execution_strategy": "react"},
    )


def _run_in_thread(
    runtime: DurableAgentRuntime,
    run_id: str,
    results: dict[str, Checkpoint],
    errors: dict[str, BaseException],
) -> None:
    try:
        results[run_id] = asyncio.run(
            runtime.start(thread_id=f"thread-{run_id}", run_id=run_id, input="block")
        )
    except BaseException as exc:  # test thread must report cancellation bugs
        errors[run_id] = exc


def test_registry_lookup_cleanup_conflict_and_stale_replacement():
    async def scenario() -> None:
        supervisor = ActiveRunSupervisor()
        current = asyncio.current_task()
        assert current is not None
        first = _handle("run-live", current)
        supervisor.register(first)

        assert supervisor.get("run-live") is first
        assert supervisor.is_active("run-live")
        with pytest.raises(ActiveRunRegistrationError, match="active execution"):
            supervisor.register(_handle("run-live", current))
        assert supervisor.unregister("run-live") is first
        assert supervisor.unregister("run-live") is None

        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        supervisor.register(_handle("run-stale", finished))
        replacement = _handle("run-stale", current)
        supervisor.register(replacement)
        assert supervisor.get("run-stale") is replacement

    asyncio.run(scenario())


def test_cancel_result_handles_missing_done_and_closed_loop():
    async def scenario() -> None:
        supervisor = ActiveRunSupervisor()
        current = asyncio.current_task()
        assert current is not None
        assert supervisor.request_cancel("missing").status == "not_active"

        finished = asyncio.create_task(asyncio.sleep(0))
        await finished
        supervisor.register(_handle("done", finished))
        assert supervisor.request_cancel("done").status == "already_done"

        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        closed = _handle("closed", current)
        closed.event_loop = closed_loop
        supervisor.register(closed)
        assert supervisor.request_cancel("closed").status == "loop_unavailable"
        supervisor.unregister("closed")

    asyncio.run(scenario())


def test_completed_and_failed_executions_unregister_handles(tmp_path):
    async def scenario() -> None:
        store = SQLiteCheckpointStore(tmp_path / "runtime.db")
        supervisor = ActiveRunSupervisor()

        completed_runtime = DurableAgentRuntime(
            llm_client=CompletingClient(),
            tool_registry=ToolRegistry(),
            system_prompt="test",
            cwd=str(tmp_path),
            config=_config(tmp_path),
            store=store,
            active_run_supervisor=supervisor,
        )
        completed = await completed_runtime.start(
            thread_id="thread-completed",
            run_id="run-completed",
            input="complete",
        )
        assert completed.status == RunStatus.COMPLETED
        assert supervisor.get(completed.run_id) is None

        failed_runtime = DurableAgentRuntime(
            llm_client=FailingClient(),
            tool_registry=ToolRegistry(),
            system_prompt="test",
            cwd=str(tmp_path),
            config=_config(tmp_path),
            store=store,
            active_run_supervisor=supervisor,
        )
        failed = await failed_runtime.start(
            thread_id="thread-failed",
            run_id="run-failed",
            input="fail",
        )
        assert failed.status == RunStatus.FAILED
        assert supervisor.get(failed.run_id) is None

    asyncio.run(scenario())


def test_cross_thread_durable_cancel_signals_owner_loop_and_cleans_handle(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.db")
    supervisor = ActiveRunSupervisor()
    client = BlockingClient()
    runtime = _runtime(tmp_path, store, client, supervisor)
    results: dict[str, Checkpoint] = {}
    errors: dict[str, BaseException] = {}
    thread = threading.Thread(
        target=_run_in_thread,
        args=(runtime, "run-cross-thread", results, errors),
    )
    thread.start()
    assert client.started.wait(2)
    handle = supervisor.get("run-cross-thread")
    assert handle is not None
    assert handle.owner_thread_id == thread.ident

    canceller = _runtime(tmp_path, store, client, supervisor)
    cancelled = asyncio.run(canceller.cancel("run-cross-thread"))
    repeated = asyncio.run(canceller.cancel("run-cross-thread"))
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert not errors
    assert cancelled.status == RunStatus.CANCELLED
    assert repeated.status == RunStatus.CANCELLED
    assert results["run-cross-thread"].status == RunStatus.CANCELLED
    assert not supervisor.is_active("run-cross-thread")
    assert asyncio.run(store.load("run-cross-thread")).status == RunStatus.CANCELLED


def test_two_owner_loops_can_be_cancelled_without_cross_loop_task_access(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.db")
    supervisor = ActiveRunSupervisor()
    clients = [BlockingClient(), BlockingClient()]
    results: dict[str, Checkpoint] = {}
    errors: dict[str, BaseException] = {}
    threads = [
        threading.Thread(
            target=_run_in_thread,
            args=(_runtime(tmp_path, store, client, supervisor), run_id, results, errors),
        )
        for client, run_id in zip(clients, ("run-a", "run-b"), strict=True)
    ]
    for thread in threads:
        thread.start()
    assert all(client.started.wait(2) for client in clients)

    cancellation = supervisor.request_cancel_many(("run-a", "missing", "run-b"))
    for thread in threads:
        thread.join(timeout=3)

    assert cancellation.signalled == ("run-a", "run-b")
    assert cancellation.not_active == ("missing",)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert {state.status for state in results.values()} == {RunStatus.CANCELLED}
    assert supervisor.list_active() == ()


def test_cancel_wins_completion_race_without_stale_terminal_overwrite(tmp_path):
    store = CompletionBarrierStore(tmp_path / "runtime.db")
    supervisor = ActiveRunSupervisor()
    client = CompletingClient()
    runtime = DurableAgentRuntime(
        llm_client=client,
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=_config(tmp_path),
        store=store,
        active_run_supervisor=supervisor,
    )
    results: dict[str, Checkpoint] = {}
    errors: dict[str, BaseException] = {}
    thread = threading.Thread(
        target=_run_in_thread,
        args=(runtime, "run-race", results, errors),
    )
    thread.start()
    assert store.completion_ready.wait(2)

    canceller = DurableAgentRuntime(
        llm_client=client,
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=_config(tmp_path),
        store=store,
        active_run_supervisor=supervisor,
    )
    cancelled = asyncio.run(canceller.cancel("run-race"))
    store.release_completion.set()
    assert store.stale_save_finished.wait(2)
    thread.join(timeout=3)
    final = asyncio.run(store.load("run-race"))
    with sqlite3.connect(store.db_path) as conn:
        terminal_rows = conn.execute(
            "select status from checkpoints where run_id = ? and status in (?, ?)",
            ("run-race", RunStatus.CANCELLED.value, RunStatus.COMPLETED.value),
        ).fetchall()

    assert not errors
    assert not thread.is_alive()
    assert cancelled.status == RunStatus.CANCELLED
    assert results["run-race"].status == RunStatus.CANCELLED
    assert final is not None and final.status == RunStatus.CANCELLED
    assert terminal_rows == [(RunStatus.CANCELLED.value,)]


def test_waiting_approval_cancel_is_durable_when_not_active(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.db")
    supervisor = ActiveRunSupervisor()
    state = Checkpoint.create(
        thread_id="thread-waiting",
        run_id="run-waiting",
        input="approve",
    )
    state.status = RunStatus.WAITING_APPROVAL
    asyncio.run(store.save(state))

    cancelled = asyncio.run(
        _runtime(tmp_path, store, BlockingClient(), supervisor).cancel(state.run_id)
    )

    assert cancelled.status == RunStatus.CANCELLED
    assert supervisor.request_cancel(state.run_id).status == "not_active"


def test_new_supervisor_is_empty_while_checkpoint_remains_running(tmp_path):
    async def scenario() -> None:
        store = SQLiteCheckpointStore(tmp_path / "runtime.db")
        state = Checkpoint.create(
            thread_id="thread-restart",
            run_id="run-restart",
            input="recover later",
        )
        await store.save(state)
        old = ActiveRunSupervisor()
        current = asyncio.current_task()
        assert current is not None
        old.register(_handle(state.run_id, current))

        replacement_process_registry = ActiveRunSupervisor()
        assert replacement_process_registry.list_active() == ()
        assert (await store.load(state.run_id)).status == RunStatus.RUNNING
        old.unregister(state.run_id)

    asyncio.run(scenario())


def test_active_run_inspection_exposes_only_safe_fields(tmp_path):
    async def scenario() -> None:
        supervisor = ActiveRunSupervisor()
        current = asyncio.current_task()
        assert current is not None
        supervisor.register(_handle("run-inspect", current))
        server = RuntimeApiServer(
            cwd=str(tmp_path),
            config=_config(tmp_path),
            api_key="test-key",
            workers=0,
            data_dir=tmp_path / "api",
            active_run_supervisor=supervisor,
        )
        request = FakeRequest()

        server._handle(request)
        payload = request.json()
        view = payload["active_runs"][0]

        assert request.status == 200
        assert view["run_id"] == "run-inspect"
        assert set(view) == {
            "run_id",
            "thread_id",
            "turn_id",
            "parent_run_id",
            "run_kind",
            "execution_strategy",
            "registered_at",
            "owner_thread_id",
            "task_done",
            "cancellation_requested",
        }
        assert "event_loop" not in json.dumps(payload)
        assert "asyncio_task" not in json.dumps(payload)
        supervisor.unregister("run-inspect")

    asyncio.run(scenario())


def test_cancel_endpoint_persists_then_signals_active_request_thread(tmp_path):
    client = BlockingClient()
    registry = ToolRegistry()
    config = _config(tmp_path)

    def factory(context: RuntimeTurnContext) -> QueryEngine:
        return QueryEngine(
            llm_client=client,
            tool_registry=registry,
            config=context.config,
            cwd=context.cwd,
        )

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-key",
        workers=0,
        data_dir=tmp_path / "cancel-api",
        engine_factory=factory,
    )
    thread_id = server.repository.create_thread()
    errors: list[BaseException] = []

    def run_turn() -> None:
        try:
            asyncio.run(server._run_turn(thread_id, "block"))
        except BaseException as exc:  # test thread must surface control-plane bugs
            errors.append(exc)

    execution_thread = threading.Thread(target=run_turn)
    execution_thread.start()
    assert client.started.wait(3)
    handle = server.active_run_supervisor.list_active()[0]
    request = FakeRequest(
        "POST",
        f"/v1/runs/{handle.run_id}/cancel",
        idempotency_key="cancel-active-once",
    )

    server._handle(request)
    repeated = FakeRequest(
        "POST",
        f"/v1/runs/{handle.run_id}/cancel",
        idempotency_key="cancel-active-once",
    )
    server._handle(repeated)
    execution_thread.join(timeout=3)
    final = asyncio.run(server.checkpoint_store.load(handle.run_id))

    assert request.status == 200
    assert repeated.status == 200
    assert request.json()["status"] == RunStatus.CANCELLED.value
    assert repeated.json() == request.json()
    assert not execution_thread.is_alive()
    assert not errors
    assert final is not None and final.status == RunStatus.CANCELLED
    assert server.active_run_supervisor.list_active() == ()


def test_idle_server_shutdown_is_bounded(tmp_path):
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=_config(tmp_path),
        api_key="test-key",
        workers=0,
        data_dir=tmp_path / "idle",
        shutdown_timeout=0.2,
    )
    started = time.monotonic()

    server.shutdown()

    assert time.monotonic() - started < 1


def test_server_shutdown_cancels_and_drains_multiple_active_runs(tmp_path):
    client = BlockingClient()
    registry = ToolRegistry()
    config = _config(tmp_path)

    def factory(context: RuntimeTurnContext) -> QueryEngine:
        return QueryEngine(
            llm_client=client,
            tool_registry=registry,
            config=context.config,
            cwd=context.cwd,
        )

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-key",
        workers=1,
        data_dir=tmp_path / "shutdown",
        engine_factory=factory,
        shutdown_timeout=3,
        port=0,
    )
    server.start()
    thread_ids = [server.repository.create_thread(), server.repository.create_thread()]
    errors: list[BaseException] = []

    def run_turn(thread_id: str) -> None:
        try:
            asyncio.run(server._run_turn(thread_id, "block"))
        except BaseException as exc:  # test thread must surface shutdown bugs
            errors.append(exc)

    request_threads = [threading.Thread(target=run_turn, args=(item,)) for item in thread_ids]
    for thread in request_threads:
        thread.start()
    deadline = time.monotonic() + 3
    while len(server.active_run_supervisor.list_active()) != 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(server.active_run_supervisor.list_active()) == 2

    server.shutdown()
    for thread in request_threads:
        thread.join(timeout=3)

    states = [asyncio.run(server.checkpoint_store.list(thread_id))[-1] for thread_id in thread_ids]
    assert not errors
    assert all(not thread.is_alive() for thread in request_threads)
    assert {state.status for state in states} == {RunStatus.CANCELLED}
    assert server.active_run_supervisor.list_active() == ()
    assert server._worker_threads == []


def test_background_worker_durable_run_registers_and_shutdown_cancels_it(tmp_path):
    client = BlockingClient()
    registry = ToolRegistry()
    config = _config(tmp_path)

    def factory(context: RuntimeTurnContext) -> QueryEngine:
        return QueryEngine(
            llm_client=client,
            tool_registry=registry,
            config=context.config,
            cwd=context.cwd,
        )

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-key",
        workers=1,
        data_dir=tmp_path / "background-shutdown",
        engine_factory=factory,
        shutdown_timeout=3,
        port=0,
    )
    server.start()
    task_id = server.task_manager.add("block in worker")
    assert client.started.wait(3)
    handles = server.active_run_supervisor.list_active()
    assert len(handles) == 1
    assert handles[0].run_id == f"run_{task_id}"
    assert handles[0].run_kind == "background_task"

    server.shutdown()
    task = server.task_manager.get(task_id)
    state = asyncio.run(server.checkpoint_store.load(f"run_{task_id}"))

    assert task is not None and task.status == "canceled"
    assert state is not None and state.status == RunStatus.CANCELLED
    assert server.active_run_supervisor.list_active() == ()
    assert server._worker_threads == []
