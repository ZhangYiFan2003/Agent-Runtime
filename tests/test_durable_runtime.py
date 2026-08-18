from __future__ import annotations

import asyncio
import json
from io import BytesIO
from typing import Any

import pytest

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.runtime import (
    Checkpoint,
    CheckpointConflictError,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    RetryPolicy,
    RunStatus,
    SQLiteCheckpointStore,
    ToolExecutionStatus,
)
from axiom.runtime.api import RuntimeApiServer, RuntimeTurnContext
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolResult, object_schema


class ToolCallingLlm:
    model_name = "fake-model"
    provider_name = "fake"
    max_context_window = 10000

    def __init__(self, tool_names: list[str], final_text: str = "finished") -> None:
        self.tool_names = tool_names
        self.final_text = final_text
        self.calls = 0

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        self.calls += 1
        if not any(message.role == "tool" for message in messages):
            for index, name in enumerate(self.tool_names):
                yield {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": index,
                        "id": f"call_{name}",
                        "function": {"name": name, "arguments": json.dumps({"value": name})},
                    },
                }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": self.final_text}
        yield {
            "type": "usage",
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }
        yield {"type": "message_end", "stop_reason": "end_turn"}


class FinalLlm:
    model_name = "fake-model"
    provider_name = "fake"
    max_context_window = 10000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        yield {"type": "text_delta", "text": "resumed"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(list(tools))
    return registry


def _tool(
    name: str,
    handler,
    *,
    read_only: bool = False,
    requires_approval: bool = False,
    idempotency_key_parameter: str | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=f"{name} test tool",
        parameters=object_schema({"value": {"type": "string"}}, required=["value"]),
        handler=handler,
        is_read_only=read_only,
        requires_approval=requires_approval,
        idempotency_key_parameter=idempotency_key_parameter,
    )


def _runtime(
    *,
    llm,
    registry: ToolRegistry,
    store,
    tmp_path,
    event_sink=None,
    retry_policy: RetryPolicy | None = None,
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=registry,
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=store,
        event_sink=event_sink,
        retry_policy=retry_policy,
    )


def test_crash_after_tool_checkpoint_resumes_without_reexecuting_completed_tool(tmp_path):
    async def scenario():
        counts = {"tool_a": 0, "tool_b": 0}

        async def handler(payload, _context):
            counts[payload["value"]] += 1
            return ToolResult(content=f"done:{payload['value']}")

        store = MemoryCheckpointStore()
        registry = _registry(_tool("tool_a", handler), _tool("tool_b", handler))
        llm = ToolCallingLlm(["tool_a", "tool_b"])
        crashed = False

        def crash_after_first_tool(event_type: str, payload: dict[str, Any]) -> None:
            nonlocal crashed
            if event_type == "tool.completed" and payload["tool_name"] == "tool_a" and not crashed:
                crashed = True
                raise RuntimeError("simulated process crash")

        first = _runtime(
            llm=llm,
            registry=registry,
            store=store,
            tmp_path=tmp_path,
            event_sink=crash_after_first_tool,
        )
        with pytest.raises(RuntimeError, match="simulated process crash"):
            await first.start(thread_id="thread_case_1", input="run tools", run_id="run_case_1")

        checkpoint = await store.load("run_case_1")
        assert checkpoint is not None
        assert checkpoint.status == RunStatus.RUNNING
        assert checkpoint.next_tool_index == 1

        restarted = _runtime(llm=llm, registry=registry, store=store, tmp_path=tmp_path)
        completed = await restarted.resume("run_case_1")

        assert completed.status == RunStatus.COMPLETED
        assert completed.output_text == "finished"
        assert counts == {"tool_a": 1, "tool_b": 1}

    asyncio.run(scenario())


def test_success_record_deduplicates_tool_when_state_checkpoint_was_not_written(tmp_path):
    class CrashBeforeStateCheckpointStore(MemoryCheckpointStore):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        async def save(self, checkpoint: Checkpoint) -> None:
            record = await self.load_tool_execution(f"{checkpoint.run_id}:call_tool_a")
            if (
                not self.crashed
                and checkpoint.next_tool_index == 1
                and record is not None
                and record.status == ToolExecutionStatus.SUCCEEDED
            ):
                self.crashed = True
                raise RuntimeError("crash before state checkpoint")
            await super().save(checkpoint)

    async def scenario():
        executions = 0

        async def handler(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult(content="side effect complete")

        store = CrashBeforeStateCheckpointStore()
        registry = _registry(_tool("tool_a", handler))
        llm = ToolCallingLlm(["tool_a"])
        first = _runtime(llm=llm, registry=registry, store=store, tmp_path=tmp_path)
        with pytest.raises(RuntimeError, match="crash before state checkpoint"):
            await first.start(
                thread_id="thread_record_dedupe",
                input="run once",
                run_id="run_record_dedupe",
            )

        persisted = await store.load("run_record_dedupe")
        assert persisted is not None
        assert persisted.next_tool_index == 0

        restarted = _runtime(llm=llm, registry=registry, store=store, tmp_path=tmp_path)
        completed = await restarted.resume("run_record_dedupe")

        assert completed.status == RunStatus.COMPLETED
        assert executions == 1

    asyncio.run(scenario())


def test_tool_approval_interrupt_survives_sqlite_restart(tmp_path):
    async def scenario():
        executions = 0

        async def dangerous(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult(content="approved side effect")

        db_path = tmp_path / "runtime.db"
        llm = ToolCallingLlm(["dangerous"])
        registry = _registry(_tool("dangerous", dangerous, requires_approval=True))
        first = _runtime(
            llm=llm,
            registry=registry,
            store=SQLiteCheckpointStore(db_path),
            tmp_path=tmp_path,
        )
        waiting = await first.start(
            thread_id="thread_case_2",
            input="dangerous work",
            run_id="run_case_2",
        )

        assert waiting.status == RunStatus.WAITING_APPROVAL
        assert executions == 0

        restarted = _runtime(
            llm=llm,
            registry=registry,
            store=SQLiteCheckpointStore(db_path),
            tmp_path=tmp_path,
        )
        completed = await restarted.resume("run_case_2", decision="approve")

        assert completed.status == RunStatus.COMPLETED
        assert executions == 1

    asyncio.run(scenario())


def test_rejected_tool_is_not_executed_and_rejection_reaches_agent(tmp_path):
    async def scenario():
        executions = 0

        async def dangerous(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult(content="should not run")

        store = MemoryCheckpointStore()
        llm = ToolCallingLlm(["dangerous"], final_text="handled rejection")
        registry = _registry(_tool("dangerous", dangerous, requires_approval=True))
        runtime = _runtime(llm=llm, registry=registry, store=store, tmp_path=tmp_path)
        waiting = await runtime.start(thread_id="thread_case_3", input="request")
        completed = await runtime.resume(waiting.run_id, decision="reject")

        assert completed.status == RunStatus.COMPLETED
        assert completed.output_text == "handled rejection"
        assert executions == 0
        tool_messages = [
            message.content for message in completed.messages if message.role == "tool"
        ]
        assert any("rejected" in str(content) for content in tool_messages)

    asyncio.run(scenario())


def test_failed_tool_retries_and_persists_attempt_count(tmp_path):
    async def scenario():
        attempts = 0

        async def flaky(_payload, _context):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("temporary outage")
            return ToolResult(content="recovered")

        store = MemoryCheckpointStore()
        llm = ToolCallingLlm(["flaky"])
        registry = _registry(_tool("flaky", flaky, read_only=True))
        runtime = _runtime(
            llm=llm,
            registry=registry,
            store=store,
            tmp_path=tmp_path,
            retry_policy=RetryPolicy(max_attempts=3),
        )
        completed = await runtime.start(
            thread_id="thread_case_4",
            input="retry",
            run_id="run_case_4",
        )
        record = await store.load_tool_execution("run_case_4:call_flaky")

        assert completed.status == RunStatus.COMPLETED
        assert attempts == 3
        assert record is not None
        assert record.status == ToolExecutionStatus.SUCCEEDED
        assert record.attempt == 3

    asyncio.run(scenario())


def test_cancelled_run_cannot_be_resumed(tmp_path):
    async def scenario():
        async def dangerous(_payload, _context):
            return ToolResult(content="done")

        store = MemoryCheckpointStore()
        registry = _registry(_tool("dangerous", dangerous, requires_approval=True))
        runtime = _runtime(
            llm=ToolCallingLlm(["dangerous"]),
            registry=registry,
            store=store,
            tmp_path=tmp_path,
        )
        waiting = await runtime.start(thread_id="thread_case_5", input="cancel me")
        cancelled = await runtime.cancel(waiting.run_id)

        assert cancelled.status == RunStatus.CANCELLED
        with pytest.raises(ValueError, match="cannot be resumed"):
            await runtime.resume(waiting.run_id, decision="approve")

    asyncio.run(scenario())


def test_sqlite_store_reloads_latest_checkpoint_in_new_instance(tmp_path):
    async def scenario():
        path = tmp_path / "reload.db"
        first = SQLiteCheckpointStore(path)
        checkpoint = Checkpoint.create(
            thread_id="thread_case_6",
            input="persist",
            run_id="run_case_6",
        )
        await first.save(checkpoint)
        checkpoint.status = RunStatus.INTERRUPTED
        await first.save(checkpoint)

        second = SQLiteCheckpointStore(path)
        restored = await second.load("run_case_6")
        listed = await second.list("thread_case_6")

        assert restored is not None
        assert restored.status == RunStatus.INTERRUPTED
        assert restored.sequence == 2
        assert [item.run_id for item in listed] == ["run_case_6"]

    asyncio.run(scenario())


def test_checkpoint_optimistic_sequence_rejects_stale_worker(tmp_path):
    async def scenario():
        store = SQLiteCheckpointStore(tmp_path / "cas.db")
        checkpoint = Checkpoint.create(thread_id="thread_cas", input="work", run_id="run_cas")
        await store.save(checkpoint)
        first = await store.load(checkpoint.run_id)
        stale = await store.load(checkpoint.run_id)
        assert first is not None and stale is not None
        first.status = RunStatus.INTERRUPTED
        await store.save(first)
        stale.status = RunStatus.CANCELLED
        with pytest.raises(CheckpointConflictError):
            await store.save(stale)

    asyncio.run(scenario())


def test_manual_interrupt_can_resume_from_persisted_checkpoint(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        checkpoint = Checkpoint.create(
            thread_id="thread_manual",
            input="pause",
            run_id="run_manual",
        )
        await store.save(checkpoint)
        runtime = _runtime(
            llm=FinalLlm(),
            registry=_registry(),
            store=store,
            tmp_path=tmp_path,
        )

        interrupted = await runtime.interrupt(checkpoint.run_id, reason="operator pause")
        completed = await runtime.resume(checkpoint.run_id)

        assert interrupted.status == RunStatus.INTERRUPTED
        assert interrupted.interrupt is not None
        assert interrupted.interrupt.reason == "operator pause"
        assert completed.status == RunStatus.COMPLETED
        assert completed.output_text == "resumed"

    asyncio.run(scenario())


def test_concurrent_resume_advances_run_once_in_process(tmp_path):
    async def scenario():
        executions = 0

        async def dangerous(_payload, _context):
            nonlocal executions
            executions += 1
            await asyncio.sleep(0)
            return ToolResult(content="done")

        store = MemoryCheckpointStore()
        runtime = _runtime(
            llm=ToolCallingLlm(["dangerous"]),
            registry=_registry(_tool("dangerous", dangerous, requires_approval=True)),
            store=store,
            tmp_path=tmp_path,
        )
        waiting = await runtime.start(thread_id="thread_double", input="approve")
        results = await asyncio.gather(
            runtime.resume(waiting.run_id, decision="approve"),
            runtime.resume(waiting.run_id, decision="approve"),
            return_exceptions=True,
        )

        assert sum(isinstance(result, Checkpoint) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        assert executions == 1

    asyncio.run(scenario())


def test_ambiguous_side_effect_waits_for_explicit_recovery_decision(tmp_path):
    class SimulatedProcessDeath(BaseException):
        pass

    async def scenario():
        executions = 0

        async def side_effect(_payload, _context):
            nonlocal executions
            executions += 1
            raise SimulatedProcessDeath()

        store = MemoryCheckpointStore()
        registry = _registry(_tool("side_effect", side_effect))
        llm = ToolCallingLlm(["side_effect"])
        first = _runtime(llm=llm, registry=registry, store=store, tmp_path=tmp_path)
        with pytest.raises(SimulatedProcessDeath):
            await first.start(
                thread_id="thread_ambiguous",
                input="external effect",
                run_id="run_ambiguous",
            )

        restarted = _runtime(llm=llm, registry=registry, store=store, tmp_path=tmp_path)
        waiting = await restarted.resume("run_ambiguous")

        assert waiting.status == RunStatus.WAITING_APPROVAL
        assert waiting.interrupt is not None
        assert waiting.interrupt.kind == "ambiguous_tool_execution"
        assert executions == 1

    asyncio.run(scenario())


class FakeRequest:
    def __init__(self, method: str, path: str, payload: dict[str, Any] | None = None):
        raw = json.dumps(payload or {}).encode()
        self.command = method
        self.path = path
        self.headers = {"content-length": str(len(raw)), "x-api-key": "test-key"}
        self.rfile = BytesIO(raw)
        self.wfile = BytesIO()
        self.status = 0

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _key: str, _value: str) -> None:
        return

    def end_headers(self) -> None:
        return

    def json(self) -> dict[str, Any]:
        return json.loads(self.wfile.getvalue())


def test_http_run_resume_after_runtime_restart(tmp_path):
    executions = 0

    async def dangerous(_payload, _context):
        nonlocal executions
        executions += 1
        return ToolResult(content="approved")

    registry = _registry(_tool("dangerous", dangerous, requires_approval=True))
    llm = ToolCallingLlm(["dangerous"])

    def engine_factory(context: RuntimeTurnContext) -> QueryEngine:
        return QueryEngine(
            llm_client=llm,
            tool_registry=registry,
            config=context.config,
            cwd=context.cwd,
        )

    data_dir = tmp_path / "api-runtime"
    first = RuntimeApiServer(
        cwd=str(tmp_path),
        config=AxiomConfig(),
        api_key="test-key",
        workers=0,
        data_dir=data_dir,
        engine_factory=engine_factory,
    )
    thread_id = first.repository.create_thread()
    start = FakeRequest("POST", f"/v1/threads/{thread_id}/turns", {"message": "approve"})
    first._handle(start)
    started = start.json()

    assert start.status == 202
    assert started["status"] == RunStatus.WAITING_APPROVAL
    assert executions == 0

    second = RuntimeApiServer(
        cwd=str(tmp_path),
        config=AxiomConfig(),
        api_key="test-key",
        workers=0,
        data_dir=data_dir,
        engine_factory=engine_factory,
    )
    get_run = FakeRequest("GET", f"/v1/runs/{started['run_id']}")
    second._handle(get_run)
    resume = FakeRequest(
        "POST",
        f"/v1/runs/{started['run_id']}/resume",
        {"decision": "approve"},
    )
    second._handle(resume)

    assert get_run.status == 200
    assert get_run.json()["status"] == RunStatus.WAITING_APPROVAL
    assert resume.status == 200
    assert resume.json() == {"thread_id": thread_id, "text": "finished"}
    assert executions == 1
    event_types = [event.type for event in second.repository.list_events(thread_id)]
    assert {"run.interrupted", "run.resumed", "run.completed"} <= set(event_types)
