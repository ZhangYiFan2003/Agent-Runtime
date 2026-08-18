from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from axiom.agent import QueryEngine
from axiom.bootstrap import build_tool_registry
from axiom.config import AxiomConfig
from axiom.llm import create_llm_client
from axiom.memory import MemoryService, SummaryPolicy
from axiom.runtime.checkpoints import (
    CheckpointConflictError,
    RuntimeStore,
    SQLiteCheckpointStore,
)
from axiom.runtime.durable import DurableAgentRuntime, RetryPolicy
from axiom.runtime.models import Checkpoint, Interrupt, RunError, RunStatus
from axiom.runtime.observability import SpanStatus, SpanType
from axiom.runtime.observability_store import (
    ObservabilityService,
    ObservabilityStore,
    RunTracer,
    SQLiteObservabilityStore,
)
from axiom.runtime.tasks import DurableTaskManager
from axiom.types import Message


@dataclass(slots=True)
class RuntimeEvent:
    id: int
    thread_id: str
    type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class RuntimeTurnContext:
    thread_id: str | None
    message: str
    history: list[Message]
    cwd: str
    config: AxiomConfig
    turn_id: str | None = None
    run_id: str | None = None


EngineFactory = Callable[[RuntimeTurnContext], Any]
ToolRegistryFactory = Callable[[AxiomConfig, str], Any]


class ThreadEventRepository:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def create_thread(self) -> str:
        thread_id = f"thread_{uuid4().hex}"
        now = _now()
        with self._connect() as conn:
            conn.execute(
                "insert into threads(id, created_at) values (?, ?)",
                (thread_id, now),
            )
        self.append_event(thread_id, "thread.created", {"id": thread_id})
        return thread_id

    def thread_exists(self, thread_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("select 1 from threads where id = ?", (thread_id,)).fetchone()
        return row is not None

    def append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> int:
        if not self.thread_exists(thread_id):
            raise ValueError("thread not found")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert into events(thread_id, type, payload, created_at)
                values (?, ?, ?, ?)
                """,
                (
                    thread_id,
                    event_type,
                    json.dumps(_jsonable(payload), ensure_ascii=False),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_events(self, thread_id: str, after_id: int | None = None) -> list[RuntimeEvent]:
        if not self.thread_exists(thread_id):
            return []
        clause = "thread_id = ?"
        params: list[object] = [thread_id]
        if after_id is not None:
            clause += " and id > ?"
            params.append(after_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select id, thread_id, type, payload, created_at
                from events
                where {clause}
                order by id
                """,
                tuple(params),
            ).fetchall()
        return [
            RuntimeEvent(
                id=int(row[0]),
                thread_id=str(row[1]),
                type=str(row[2]),
                payload=_decode_payload(str(row[3])),
                created_at=str(row[4]),
            )
            for row in rows
        ]

    def database_ok(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("select 1").fetchone()
            return True
        except sqlite3.DatabaseError:
            return False

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists threads (
                    id text primary key,
                    created_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    thread_id text not null references threads(id) on delete cascade,
                    type text not null,
                    payload text not null,
                    created_at text not null
                )
                """
            )
            conn.execute("create index if not exists idx_events_thread_id on events(thread_id, id)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 30000")
        conn.execute("pragma foreign_keys = on")
        return conn


class RuntimeApiServer:
    def __init__(
        self,
        *,
        cwd: str,
        config: AxiomConfig,
        api_key: str,
        port: int = 8080,
        workers: int = 2,
        data_dir: str | Path | None = None,
        task_manager: DurableTaskManager | None = None,
        engine_factory: EngineFactory | None = None,
        tool_registry_factory: ToolRegistryFactory | None = None,
        memory_service: MemoryService | None = None,
        checkpoint_store: RuntimeStore | None = None,
        observability_store: ObservabilityStore | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        self.cwd = str(Path(cwd).resolve())
        self.config = config
        self.api_key = api_key
        self.port = port
        self.workers = workers
        self.data_dir = (
            Path(data_dir).expanduser() if data_dir else Path.home() / ".axiom" / "runtime"
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.repository = ThreadEventRepository(self.data_dir / "runtime.db")
        self.checkpoint_store = checkpoint_store or SQLiteCheckpointStore(
            self.data_dir / "runtime.db"
        )
        self.observability_store = observability_store or SQLiteObservabilityStore(
            self.data_dir / "runtime.db"
        )
        self.observability = ObservabilityService(self.observability_store)
        self.retry_policy = retry_policy or RetryPolicy()
        self.task_manager = task_manager or DurableTaskManager(self.data_dir / "tasks.db")
        self.memory_service = memory_service or MemoryService(
            self.data_dir / "memory.db",
            project_scope=self.cwd,
            summary_policy=_summary_policy_from_config(config),
        )
        self.engine_factory = engine_factory
        self.tool_registry_factory = tool_registry_factory or build_tool_registry
        self._stop = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._worker_threads: list[threading.Thread] = []
        self._thread_locks: dict[str, threading.Lock] = {}
        self._thread_locks_guard = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}
        self._run_locks_guard = threading.Lock()

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            return ("127.0.0.1", self.port)
        host, port = self._httpd.server_address[:2]
        return (str(host), int(port))

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._stop.clear()
        self._start_workers()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        self._server_thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="axiom-runtime-api",
            daemon=False,
        )
        self._server_thread.start()
        self.port = self.address[1]

    def serve_forever(self) -> None:
        self._stop.clear()
        self._start_workers()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        self.port = self.address[1]
        print(f"Axiom Runtime API listening on http://127.0.0.1:{self.port}", flush=True)
        try:
            self._httpd.serve_forever()
        finally:
            self._stop.set()
            if self._httpd is not None:
                self._httpd.server_close()
            for worker in self._worker_threads:
                if worker.is_alive():
                    worker.join(timeout=5)
            self._httpd = None
            self._worker_threads = []

    def shutdown(self) -> None:
        self._stop.set()
        httpd = self._httpd
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        thread = self._server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        for worker in self._worker_threads:
            if worker.is_alive():
                worker.join(timeout=5)
        self._httpd = None
        self._server_thread = None
        self._worker_threads = []

    def running(self):
        return _RunningRuntimeServer(self)

    def _start_workers(self) -> None:
        if self._worker_threads:
            return
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"axiom-task-{index}",
                daemon=False,
            )
            thread.start()
            self._worker_threads.append(thread)

    def _handler_class(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                outer._handle(self)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        return Handler

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        parsed = urlsplit(request.path)
        method = request.command
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and path == "/health":
            _send_json(
                request,
                200,
                {
                    "status": "ok",
                    "workers": self.workers,
                    "database": "ok" if self.repository.database_ok() else "error",
                },
            )
            return
        if not self._authorized(request):
            _send_json(request, 401, {"error": "unauthorized"})
            return
        try:
            body = _read_json(request)
            if method == "POST" and path == "/v1/threads":
                thread_id = self.repository.create_thread()
                _send_json(request, 200, {"id": thread_id})
            elif method == "POST" and path.startswith("/v1/threads/") and path.endswith("/turns"):
                thread_id = path.split("/")[3]
                if not self.repository.thread_exists(thread_id):
                    _send_json(request, 404, {"error": "thread not found"})
                    return
                message = str(body.get("message") or body.get("prompt") or "")
                if not message:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                lock = self._thread_lock(thread_id)
                if not lock.acquire(blocking=False):
                    _send_json(request, 409, {"error": "thread turn already running"})
                    return
                try:
                    result = asyncio.run(self._run_turn(thread_id, message))
                    response_status = (
                        202
                        if result.get("status")
                        in {
                            RunStatus.INTERRUPTED.value,
                            RunStatus.WAITING_APPROVAL.value,
                            RunStatus.WAITING_CHILD.value,
                        }
                        else 200
                    )
                    _send_json(request, response_status, result)
                finally:
                    lock.release()
            elif method == "GET" and path.startswith("/v1/threads/") and path.endswith("/events"):
                thread_id = path.split("/")[3]
                after_id = _first_int(query.get("after_id"))
                if not self.repository.thread_exists(thread_id):
                    _send_json(request, 404, {"error": "thread not found"})
                    return
                self._send_events(request, thread_id, after_id=after_id)
            elif method == "GET" and path.startswith("/v1/runs/") and path.endswith("/trace"):
                run_id = path.split("/")[3]
                bundle = asyncio.run(self.observability.trace(run_id))
                if bundle is None:
                    _send_json(request, 404, {"error": "trace not found"})
                    return
                _send_json(request, 200, bundle.to_dict())
            elif method == "GET" and path.startswith("/v1/runs/") and path.endswith("/metrics"):
                run_id = path.split("/")[3]
                metrics = asyncio.run(self.observability.metrics(run_id))
                if metrics is None:
                    _send_json(request, 404, {"error": "metrics not found"})
                    return
                _send_json(request, 200, metrics.to_dict())
            elif method == "GET" and path.startswith("/v1/runs/"):
                run_id = path.split("/")[3]
                state = asyncio.run(self.checkpoint_store.load(run_id))
                if state is None:
                    _send_json(request, 404, {"error": "run not found"})
                    return
                _send_json(request, 200, state.public_dict())
            elif method == "POST" and path.startswith("/v1/runs/") and path.endswith("/resume"):
                run_id = path.split("/")[3]
                lock = self._run_lock(run_id)
                if not lock.acquire(blocking=False):
                    _send_json(request, 409, {"error": "run already advancing"})
                    return
                try:
                    decision = body.get("decision")
                    if decision is None and "approved" in body:
                        decision = "approve" if body.get("approved") else "reject"
                    result = asyncio.run(
                        self._resume_run(
                            run_id,
                            decision=str(decision) if decision is not None else None,
                        )
                    )
                    response_status = (
                        202
                        if result.get("status")
                        in {RunStatus.WAITING_APPROVAL.value, RunStatus.WAITING_CHILD.value}
                        else 200
                    )
                    _send_json(request, response_status, result)
                finally:
                    lock.release()
            elif method == "POST" and path.startswith("/v1/runs/") and path.endswith("/cancel"):
                run_id = path.split("/")[3]
                state = asyncio.run(self._set_run_status(run_id, RunStatus.CANCELLED))
                _send_json(request, 200, state.public_dict())
            elif method == "POST" and path.startswith("/v1/runs/") and path.endswith("/interrupt"):
                run_id = path.split("/")[3]
                reason = str(body.get("reason") or "manual interrupt")
                state = asyncio.run(
                    self._set_run_status(run_id, RunStatus.INTERRUPTED, reason=reason)
                )
                _send_json(request, 200, state.public_dict())
            elif method == "POST" and path == "/v1/tasks":
                prompt = str(body.get("message") or body.get("prompt") or "")
                if not prompt:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                task_id = self.task_manager.add(prompt)
                _send_json(request, 200, {"id": task_id, "status": "queued"})
            elif method == "GET" and path == "/v1/tasks":
                _send_json(request, 200, {"tasks": [asdict(t) for t in self.task_manager.list()]})
            elif method == "GET" and path.startswith("/v1/tasks/"):
                task = self.task_manager.get(path.split("/")[3])
                payload = asdict(task) if task else {"error": "not found"}
                _send_json(request, 200 if task else 404, payload)
            elif method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[3]
                _send_json(request, 200, {"canceled": self.task_manager.cancel(task_id)})
            else:
                _send_json(request, 404, {"error": "not found"})
        except (CheckpointConflictError, ValueError) as exc:
            _send_json(request, 409, {"error": _safe_error(exc)})
        except Exception as exc:  # noqa: BLE001 - API boundary
            _send_json(request, 500, {"error": _safe_error(exc)})

    async def _run_turn(self, thread_id: str, message: str) -> dict[str, Any]:
        history_events = self.repository.list_events(thread_id)
        history = self.memory_service.history_from_runtime_events(history_events)
        turn_id = f"turn_{uuid4().hex}"
        run_id = f"run_{uuid4().hex}"
        context = RuntimeTurnContext(
            thread_id=thread_id,
            message=message,
            history=history,
            cwd=self.cwd,
            config=self.config,
            turn_id=turn_id,
            run_id=run_id,
        )
        self.repository.append_event(
            thread_id,
            "turn.started",
            {"turn_id": turn_id, "run_id": run_id, "message_chars": len(message)},
        )
        user_event_id = self.repository.append_event(thread_id, "user.message", {"text": message})
        self._derive_conversation_memory(
            thread_id,
            role="user",
            content=message,
            event_id=user_event_id,
        )
        engine = await self._engine(context)
        run_lock = self._run_lock(run_id)
        if not run_lock.acquire(blocking=False):
            raise RuntimeError("new run unexpectedly already advancing")
        try:
            if isinstance(engine, QueryEngine):
                runtime = self._durable_runtime(
                    engine,
                    thread_id,
                    execution_strategy=_execution_strategy_name(engine.config.prompt.agent_mode),
                )
                state = await runtime.start(
                    thread_id=thread_id,
                    input=message,
                    history=history,
                    run_id=run_id,
                    turn_id=turn_id,
                )
                return await self._finish_durable_turn(state)

            return await self._run_legacy_turn(
                engine=engine,
                context=context,
                thread_id=thread_id,
                message=message,
                history=history,
            )
        finally:
            run_lock.release()

    async def _run_legacy_turn(
        self,
        *,
        engine: Any,
        context: RuntimeTurnContext,
        thread_id: str,
        message: str,
        history: list[Message],
    ) -> dict[str, Any]:
        state = Checkpoint.create(
            thread_id=thread_id,
            input=message,
            history=history,
            run_id=context.run_id,
            turn_id=context.turn_id,
        )
        tracer = RunTracer(self.observability_store)
        await tracer.start_run(state)
        engine_span = await tracer.start_span(
            SpanType.AGENT,
            "legacy.engine",
            attributes={"durable_internal_steps": False},
        )
        await self.checkpoint_store.save(state)
        self.repository.append_event(
            thread_id,
            "run.started",
            {"run_id": state.run_id, "turn_id": state.turn_id, "legacy_engine": True},
        )
        text = ""
        done_payload: dict[str, Any] = {}
        try:
            async for event in _ask_events(engine, message, history):
                event_type = str(event.get("type"))
                if event_type == "text_delta":
                    delta = str(event.get("text") or "")
                    text += delta
                elif event_type == "tool_call":
                    self.repository.append_event(thread_id, "tool_call", _jsonable(event))
                elif event_type == "tool_result":
                    event_id = self.repository.append_event(
                        thread_id,
                        "tool_result",
                        _jsonable(event),
                    )
                    self._derive_tool_result_memory(
                        thread_id,
                        tool_name=str(event.get("name") or "unknown"),
                        success=not bool(event.get("error")),
                        content=_tool_result_content(event),
                        source_event_id=event_id,
                    )
                elif event_type == "error":
                    self.repository.append_event(thread_id, "error", _jsonable(event))
                elif event_type == "done":
                    done_payload = _jsonable(event)
        except Exception as exc:
            self.repository.append_event(thread_id, "error", {"error": _safe_error(exc)})
            state.status = RunStatus.FAILED
            state.error = RunError(type=type(exc).__name__, message=_safe_error(exc), step="engine")
            await self.checkpoint_store.save(state)
            await tracer.finish_span(
                engine_span,
                SpanStatus.FAILED,
                attributes={"error": _safe_error(exc)},
            )
            await tracer.update_run(state.status, terminal=True)
            self.repository.append_event(
                thread_id,
                "run.failed",
                {"run_id": state.run_id, "error": state.error.to_dict()},
            )
            raise
        state.messages.append(Message(role="assistant", content=text))
        state.output_text = text
        state.status = RunStatus.COMPLETED
        state.agent_turn = int(done_payload.get("total_turns") or 1)
        state.total_tokens = int(done_payload.get("total_tokens") or 0)
        await self.checkpoint_store.save(state)
        await tracer.finish_span(engine_span, SpanStatus.SUCCEEDED)
        await tracer.update_run(state.status, terminal=True)
        self.repository.append_event(
            thread_id,
            "run.completed",
            {"run_id": state.run_id, "legacy_engine": True},
        )
        assistant_event_id = self.repository.append_event(
            thread_id,
            "assistant.message",
            {"text": text},
        )
        self._derive_conversation_memory(
            thread_id,
            role="assistant",
            content=text,
            event_id=assistant_event_id,
        )
        self.repository.append_event(
            thread_id,
            "turn.completed",
            {**done_payload, "turn_id": state.turn_id, "run_id": state.run_id},
        )
        await self._summarize_thread_best_effort(thread_id)
        await self._extract_facts_best_effort(thread_id)
        return {"thread_id": thread_id, "text": text}

    async def _resume_run(self, run_id: str, *, decision: str | None) -> dict[str, Any]:
        state = await self.checkpoint_store.load(run_id)
        if state is None:
            raise ValueError("run not found")
        context = RuntimeTurnContext(
            thread_id=state.thread_id,
            message=state.input,
            history=list(state.messages),
            cwd=self.cwd,
            config=self.config,
            turn_id=state.turn_id,
            run_id=state.run_id,
        )
        engine = await self._engine(context)
        if not isinstance(engine, QueryEngine):
            raise ValueError("custom engine does not support durable resume")
        runtime = self._durable_runtime(
            engine,
            state.thread_id,
            execution_strategy=state.execution_strategy,
        )
        state = await runtime.resume(run_id, decision=decision)
        if state.parent_run_id and state.finished:
            parent = await self.checkpoint_store.load(state.parent_run_id)
            if parent is not None and parent.status == RunStatus.WAITING_CHILD:
                parent_runtime = self._durable_runtime(
                    engine,
                    parent.thread_id,
                    execution_strategy=parent.execution_strategy,
                )
                parent = await parent_runtime.resume(parent.run_id)
                result = await self._finish_durable_turn(parent)
                result["child_run_id"] = state.run_id
                result["child_status"] = state.status.value
                return result
        return await self._finish_durable_turn(state)

    async def _finish_durable_turn(self, state: Checkpoint) -> dict[str, Any]:
        if state.parent_run_id:
            return {
                "thread_id": state.thread_id,
                "turn_id": state.turn_id,
                "run_id": state.run_id,
                "parent_run_id": state.parent_run_id,
                "status": state.status.value,
                "interrupt": state.interrupt.to_dict() if state.interrupt else None,
                "text": state.output_text,
                "error": state.error.to_dict() if state.error else None,
            }
        if state.status in {
            RunStatus.WAITING_APPROVAL,
            RunStatus.WAITING_CHILD,
            RunStatus.INTERRUPTED,
        }:
            result = {
                "thread_id": state.thread_id,
                "turn_id": state.turn_id,
                "run_id": state.run_id,
                "status": state.status.value,
                "interrupt": state.interrupt.to_dict() if state.interrupt else None,
                "text": state.output_text,
            }
            if state.status == RunStatus.WAITING_CHILD:
                from axiom.runtime.multi_agent_strategy import MultiAgentExecutionStrategy

                orchestration = MultiAgentExecutionStrategy.load_state(state)
                assignment = (
                    orchestration.assignment(orchestration.current_assignment_id)
                    if orchestration
                    else None
                )
                child = (
                    await self.checkpoint_store.load(assignment.child_run_id)
                    if assignment and assignment.child_run_id
                    else None
                )
                result.update(
                    {
                        "child_run_id": assignment.child_run_id if assignment else None,
                        "child_status": child.status.value if child else None,
                        "child_interrupt": (
                            child.interrupt.to_dict() if child and child.interrupt else None
                        ),
                    }
                )
            return result
        if state.status == RunStatus.CANCELLED:
            return {
                "thread_id": state.thread_id,
                "turn_id": state.turn_id,
                "run_id": state.run_id,
                "status": state.status.value,
                "text": state.output_text,
            }
        if state.status == RunStatus.FAILED:
            message = state.error.message if state.error else "run failed"
            self.repository.append_event(state.thread_id, "error", {"error": message})
            raise RuntimeError(message)

        assistant_event_id = self.repository.append_event(
            state.thread_id,
            "assistant.message",
            {"text": state.output_text},
        )
        self._derive_conversation_memory(
            state.thread_id,
            role="assistant",
            content=state.output_text,
            event_id=assistant_event_id,
        )
        self.repository.append_event(
            state.thread_id,
            "turn.completed",
            {
                "turn_id": state.turn_id,
                "run_id": state.run_id,
                "total_turns": state.agent_turn,
                "total_tokens": state.total_tokens,
            },
        )
        await self._summarize_thread_best_effort(state.thread_id)
        await self._extract_facts_best_effort(state.thread_id)
        return {"thread_id": state.thread_id, "text": state.output_text}

    def _durable_runtime(
        self,
        engine: QueryEngine,
        thread_id: str,
        *,
        execution_strategy: str = "react",
    ) -> DurableAgentRuntime:
        return DurableAgentRuntime(
            llm_client=engine.llm_client,
            tool_registry=engine.tool_registry,
            system_prompt=engine.system_prompt,
            cwd=engine.cwd,
            config=engine.config,
            store=self.checkpoint_store,
            retry_policy=self.retry_policy,
            event_sink=self._runtime_event_sink(thread_id),
            tracer=RunTracer(self.observability_store),
            execution_strategy=execution_strategy,
        )

    def _runtime_event_sink(self, thread_id: str):
        def emit(event_type: str, payload: dict[str, Any]) -> None:
            self.repository.append_event(thread_id, event_type, payload)
            if event_type == "tool.started":
                self.repository.append_event(
                    thread_id,
                    "tool_call",
                    {
                        "name": payload.get("tool_name"),
                        "input": payload.get("arguments", {}),
                        "tool_call_id": payload.get("tool_call_id"),
                        "invocation_id": payload.get("invocation_id"),
                    },
                )
            elif event_type == "tool.completed":
                event_id = self.repository.append_event(
                    thread_id,
                    "tool_result",
                    {
                        "name": payload.get("tool_name"),
                        "result": payload.get("result", ""),
                        "is_error": bool(payload.get("is_error")),
                        "tool_call_id": payload.get("tool_call_id"),
                        "invocation_id": payload.get("invocation_id"),
                        "reused": bool(payload.get("reused")),
                    },
                )
                self._derive_tool_result_memory(
                    thread_id,
                    tool_name=str(payload.get("tool_name") or "unknown"),
                    success=not bool(payload.get("is_error")),
                    content=str(payload.get("result") or ""),
                    source_event_id=event_id,
                )

        return emit

    async def _set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        reason: str | None = None,
    ) -> Checkpoint:
        state = await self.checkpoint_store.load(run_id)
        if state is None:
            raise ValueError("run not found")
        if state.finished:
            if state.status == status:
                return state
            raise ValueError(f"run in {state.status.value} cannot change status")
        if status == RunStatus.INTERRUPTED and state.status != RunStatus.RUNNING:
            raise ValueError(f"run in {state.status.value} cannot be manually interrupted")
        tracer = RunTracer(self.observability_store)
        await tracer.start_run(state)
        state.status = status
        state.interrupt = (
            Interrupt(kind="manual", reason=reason or "manual interrupt")
            if status == RunStatus.INTERRUPTED
            else None
        )
        checkpoint_span = await tracer.start_span(
            SpanType.CHECKPOINT,
            "checkpoint.save",
            attributes={"sequence": state.sequence, "run_status": status.value},
        )
        try:
            await self.checkpoint_store.save(state)
        except Exception as exc:
            await tracer.finish_span(
                checkpoint_span,
                SpanStatus.FAILED,
                attributes={"error": _safe_error(exc)},
            )
            raise
        await tracer.finish_span(checkpoint_span, SpanStatus.SUCCEEDED)
        if status == RunStatus.INTERRUPTED:
            span = await tracer.start_span(
                SpanType.INTERRUPT,
                "interrupt",
                attributes={"kind": "manual", "reason": reason or "manual interrupt"},
            )
            await tracer.finish_span(span, SpanStatus.INTERRUPTED)
            await tracer.update_run(status)
        else:
            await tracer.update_run(status, terminal=True)
        event_type = "run.interrupted" if status == RunStatus.INTERRUPTED else "run.cancelled"
        self.repository.append_event(
            state.thread_id,
            event_type,
            {"run_id": state.run_id, "status": state.status.value, "reason": reason},
        )
        return state

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            task = self.task_manager.claim_next()
            if not task:
                self._stop.wait(0.05)
                continue
            try:
                result = asyncio.run(self._run_task(task.prompt))
                self.task_manager.complete(task.id, result)
            except Exception as exc:  # noqa: BLE001
                self.task_manager.fail(task.id, _safe_error(exc))

    async def _run_task(self, prompt: str) -> str:
        context = RuntimeTurnContext(
            thread_id=None,
            message=prompt,
            history=[],
            cwd=self.cwd,
            config=self.config,
        )
        engine = await self._engine(context)
        return (await engine.ask_complete_async(prompt)).text

    async def _engine(self, context: RuntimeTurnContext) -> Any:
        if self.engine_factory is not None:
            engine = self.engine_factory(context)
            if inspect.isawaitable(engine):
                return await engine
            return engine
        self._ensure_llm_key()
        registry_result = self.tool_registry_factory(config=self.config, cwd=self.cwd)
        if inspect.isawaitable(registry_result):
            registry, _manager = await registry_result
        else:
            registry, _manager = registry_result
        return QueryEngine(
            llm_client=create_llm_client(self.config.llm),
            tool_registry=registry,
            config=self.config,
            cwd=self.cwd,
        )

    def _ensure_llm_key(self) -> None:
        if not self.config.llm.api_key:
            raise ValueError(
                "AXIOM_API_KEY is not configured. Runtime turns/tasks need a working LLM key."
            )

    def _authorized(self, request: BaseHTTPRequestHandler) -> bool:
        auth = request.headers.get("authorization", "")
        token = request.headers.get("x-api-key", "")
        return auth == f"Bearer {self.api_key}" or token == self.api_key

    def _create_thread(self) -> str:
        return self.repository.create_thread()

    def _append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.repository.append_event(thread_id, event_type, payload)

    def _thread_lock(self, thread_id: str) -> threading.Lock:
        with self._thread_locks_guard:
            lock = self._thread_locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[thread_id] = lock
            return lock

    def _run_lock(self, run_id: str) -> threading.Lock:
        with self._run_locks_guard:
            lock = self._run_locks.get(run_id)
            if lock is None:
                lock = threading.Lock()
                self._run_locks[run_id] = lock
            return lock

    def _derive_conversation_memory(
        self,
        thread_id: str,
        *,
        role: str,
        content: str,
        event_id: int,
    ) -> None:
        try:
            self.memory_service.save_conversation(
                thread_id,
                role=role,
                content=content,
                event_id=event_id,
            )
        except Exception:
            return

    def _derive_tool_result_memory(
        self,
        thread_id: str,
        *,
        tool_name: str,
        success: bool,
        content: str,
        source_event_id: int,
    ) -> None:
        try:
            self.memory_service.save_tool_result(
                thread_id,
                tool_name=tool_name,
                success=success,
                content=content,
                source_event_id=source_event_id,
            )
        except Exception:
            return

    async def _summarize_thread_best_effort(self, thread_id: str) -> None:
        summarize = getattr(self.memory_service, "summarize_thread", None)
        if summarize is None:
            return
        try:
            result = summarize(thread_id)
            if inspect.isawaitable(result):
                await result
        except Exception:
            return

    async def _extract_facts_best_effort(self, thread_id: str) -> None:
        extract = getattr(self.memory_service, "extract_facts_from_thread", None)
        if extract is None:
            return
        try:
            result = extract(thread_id, self.repository.list_events(thread_id))
            if inspect.isawaitable(result):
                await result
        except Exception:
            return

    def _send_events(
        self,
        request: BaseHTTPRequestHandler,
        thread_id: str,
        *,
        after_id: int | None = None,
    ) -> None:
        rows = self.repository.list_events(thread_id, after_id=after_id)
        body = "".join(
            f"id: {event.id}\nevent: {event.type}\ndata: "
            f"{json.dumps(event.payload, ensure_ascii=False)}\n\n"
            for event in rows
        ).encode("utf-8")
        request.send_response(200)
        request.send_header("content-type", "text/event-stream")
        request.send_header("content-length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _ensure_schema(self) -> None:
        self.repository._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        return self.repository._connect()


class _RunningRuntimeServer:
    def __init__(self, server: RuntimeApiServer):
        self.server = server

    def __enter__(self) -> RuntimeApiServer:
        self.server.start()
        return self.server

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.server.shutdown()


def _read_json(request: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(request.headers.get("content-length") or 0)
    if length == 0:
        return {}
    try:
        value = json.loads(request.rfile.read(length).decode("utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _send_json(request: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request.send_response(status)
    request.send_header("content-type", "application/json")
    request.send_header("content-length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def _jsonable(event: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(value) for key, value in event.items()}


def _json_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _decode_payload(payload: str) -> dict[str, Any]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _first_int(values: list[str] | None) -> int | None:
    if not values:
        return None
    try:
        value = int(values[0])
    except ValueError:
        return None
    return value if value >= 0 else None


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    for secret in ["AXIOM_RUNTIME_API_KEY", "Authorization", "Bearer"]:
        text = text.replace(secret, "[redacted]")
    return text


def _execution_strategy_name(agent_mode: str) -> str:
    normalized = (agent_mode or "react").strip().lower().replace("-", "_")
    if normalized in {"plan", "plan_execute"}:
        return "plan_execute"
    if normalized in {"team", "multi_agent"}:
        return "multi_agent"
    return "react"


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _ask_events(engine: Any, message: str, history: list[Message]):
    method = engine.ask
    signature = inspect.signature(method)
    accepts_history = "history" in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_history:
        async for event in method(message, history=history):
            yield event
        return
    async for event in method(message):
        yield event


def _tool_result_content(event: dict[str, Any]) -> str:
    for key in ["content", "result", "output", "text"]:
        value = event.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(_jsonable(event), ensure_ascii=False)


def runtime_api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("AXIOM_RUNTIME_API_KEY")
    if not key:
        raise ValueError("AXIOM_RUNTIME_API_KEY is required for Runtime API")
    return key


def _summary_policy_from_config(config: AxiomConfig) -> SummaryPolicy:
    return SummaryPolicy(
        enabled=config.features.context_compression,
        threshold_messages=config.memory.summary_threshold_messages,
        map_chunk_estimated_tokens=config.memory.summary_map_chunk_estimated_tokens,
        reduce_input_estimated_tokens=config.memory.summary_reduce_input_estimated_tokens,
        minimum_unsummarized_messages=config.memory.summary_minimum_unsummarized_messages,
        recent_message_reserve=config.memory.summary_recent_message_reserve,
        max_summary_chars=config.memory.summary_max_chars,
        max_attempts=config.memory.summary_max_attempts,
    )
