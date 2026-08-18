from __future__ import annotations

import asyncio
import json
from io import BytesIO
from typing import Any

import pytest
from typer.testing import CliRunner

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.entrypoints.cli import app
from axiom.runtime import (
    Checkpoint,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RetryPolicy,
    RunStatus,
    SpanStatus,
    SpanType,
    SQLiteCheckpointStore,
    SQLiteObservabilityStore,
)
from axiom.runtime.api import RuntimeApiServer
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolResult, object_schema


class FinalLlm:
    provider_name = "test-provider"
    model_name = "test-model"
    max_context_window = 10000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        await asyncio.sleep(0)
        yield {"type": "text_delta", "text": "complete"}
        yield {
            "type": "usage",
            "usage": {"input_tokens": 7, "output_tokens": 3},
        }
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ToolLlm(FinalLlm):
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        if not any(message.role == "tool" for message in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": f"call_{self.tool_name}",
                    "function": {
                        "name": self.tool_name,
                        "arguments": json.dumps({"value": "test"}),
                    },
                },
            }
            yield {"type": "usage", "usage": {"input_tokens": 5, "output_tokens": 2}}
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        async for event in super().chat(messages, _tools, system_prompt="test"):
            yield event


def _tool(name: str, handler, *, read_only: bool = False, approval: bool = False) -> Tool:
    return Tool(
        name=name,
        description=f"{name} test tool",
        parameters=object_schema({"value": {"type": "string"}}, required=["value"]),
        handler=handler,
        is_read_only=read_only,
        requires_approval=approval,
    )


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(list(tools))
    return registry


def _runtime(
    *,
    llm,
    registry: ToolRegistry,
    checkpoint_store,
    observability_store,
    tmp_path,
    retry_policy: RetryPolicy | None = None,
    event_sink=None,
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=registry,
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=checkpoint_store,
        tracer=RunTracer(observability_store),
        retry_policy=retry_policy,
        event_sink=event_sink,
    )


def test_llm_trace_records_tokens_ttft_latency_and_model(tmp_path):
    async def scenario():
        observations = MemoryObservabilityStore()
        runtime = _runtime(
            llm=FinalLlm(),
            registry=_registry(),
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=observations,
            tmp_path=tmp_path,
        )
        state = await runtime.start(
            thread_id="thread_llm_trace",
            input="hello",
            run_id="run_llm_trace",
        )
        bundle = await ObservabilityService(observations).trace(state.run_id)
        metrics = await ObservabilityService(observations).metrics(state.run_id)

        assert state.status == RunStatus.COMPLETED
        assert bundle is not None and metrics is not None
        llm_span = next(span for span in bundle.spans if span.span_type == SpanType.LLM)
        assert llm_span.status == SpanStatus.SUCCEEDED
        assert llm_span.attributes["provider"] == "test-provider"
        assert llm_span.attributes["model"] == "test-model"
        assert llm_span.attributes["temperature"] == 0.7
        assert llm_span.attributes["prompt_tokens"] == 7
        assert llm_span.attributes["completion_tokens"] == 3
        assert llm_span.attributes["ttft_ms"] >= 0
        assert llm_span.attributes["latency_ms"] >= llm_span.attributes["ttft_ms"]
        assert llm_span.attributes["finish_reason"] == "end_turn"
        assert metrics.total_tokens == 10
        assert metrics.llm_calls == 1
        assert metrics.step_count == 1
        assert metrics.checkpoint_count == 2

    asyncio.run(scenario())


def test_llm_retry_metrics_count_actual_retry_attempts(tmp_path):
    class RetryingLlm(FinalLlm):
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, tools, *, system_prompt):
            self.calls += 1
            if self.calls < 3:
                raise ConnectionError("temporary model failure")
            async for event in super().chat(messages, tools, system_prompt=system_prompt):
                yield event

    async def scenario():
        llm = RetryingLlm()
        observations = MemoryObservabilityStore()
        runtime = _runtime(
            llm=llm,
            registry=_registry(),
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=observations,
            tmp_path=tmp_path,
            retry_policy=RetryPolicy(max_attempts=3),
        )
        completed = await runtime.start(
            thread_id="thread_llm_retry",
            input="retry",
            run_id="run_llm_retry",
        )
        metrics = await ObservabilityService(observations).metrics(completed.run_id)

        assert metrics is not None
        assert metrics.llm_calls == 3
        assert metrics.retry_count == 2
        assert metrics.total_tokens == 10

    asyncio.run(scenario())


def test_tool_trace_records_retry_success_and_failure(tmp_path):
    async def scenario():
        attempts = 0

        async def flaky(_payload, _context):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionError("temporary")
            return ToolResult(content="recovered")

        observations = MemoryObservabilityStore()
        runtime = _runtime(
            llm=ToolLlm("flaky"),
            registry=_registry(_tool("flaky", flaky, read_only=True)),
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=observations,
            tmp_path=tmp_path,
            retry_policy=RetryPolicy(max_attempts=3),
        )
        completed = await runtime.start(
            thread_id="thread_tool_retry",
            input="retry",
            run_id="run_tool_retry",
        )
        bundle = await ObservabilityService(observations).trace(completed.run_id)
        metrics = await ObservabilityService(observations).metrics(completed.run_id)

        assert bundle is not None and metrics is not None
        tool_span = next(span for span in bundle.spans if span.span_type == SpanType.TOOL)
        assert tool_span.status == SpanStatus.SUCCEEDED
        assert tool_span.attributes["tool_name"] == "flaky"
        assert tool_span.attributes["retry_count"] == 2
        assert tool_span.attributes["attempt"] == 3
        assert tool_span.attributes["latency_ms"] >= 0
        assert metrics.tool_calls == 1
        assert metrics.tool_successes == 1
        assert metrics.retry_count == 2

        async def always_fails(_payload, _context):
            raise ConnectionError("still unavailable")

        failed_observations = MemoryObservabilityStore()
        failed_runtime = _runtime(
            llm=ToolLlm("failed"),
            registry=_registry(_tool("failed", always_fails, read_only=True)),
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=failed_observations,
            tmp_path=tmp_path,
            retry_policy=RetryPolicy(max_attempts=2),
        )
        failed_run = await failed_runtime.start(
            thread_id="thread_tool_failed",
            input="fail",
            run_id="run_tool_failed",
        )
        failed_bundle = await ObservabilityService(failed_observations).trace(failed_run.run_id)
        assert failed_bundle is not None
        failed_span = next(span for span in failed_bundle.spans if span.span_type == SpanType.TOOL)
        assert failed_span.status == SpanStatus.FAILED
        assert failed_span.attributes["retry_count"] == 1
        assert "still unavailable" in str(failed_span.attributes["error"])

    asyncio.run(scenario())


def test_success_record_reuse_updates_single_tool_span(tmp_path):
    class CrashBeforeCheckpointStore(MemoryCheckpointStore):
        def __init__(self) -> None:
            super().__init__()
            self.crashed = False

        async def save(self, checkpoint: Checkpoint) -> None:
            record = await self.load_tool_execution(f"{checkpoint.run_id}:call_side_effect")
            if not self.crashed and checkpoint.next_tool_index == 1 and record is not None:
                self.crashed = True
                raise RuntimeError("checkpoint unavailable")
            await super().save(checkpoint)

    async def scenario():
        executions = 0

        async def side_effect(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult(content="done")

        checkpoints = CrashBeforeCheckpointStore()
        observations = MemoryObservabilityStore()
        registry = _registry(_tool("side_effect", side_effect))
        first = _runtime(
            llm=ToolLlm("side_effect"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
        )
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            await first.start(
                thread_id="thread_reused",
                input="once",
                run_id="run_reused",
            )

        restarted = _runtime(
            llm=ToolLlm("side_effect"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
        )
        completed = await restarted.resume("run_reused")
        bundle = await ObservabilityService(observations).trace(completed.run_id)

        assert bundle is not None
        tool_spans = [span for span in bundle.spans if span.span_type == SpanType.TOOL]
        root_spans = [
            span for span in bundle.spans if span.span_type == SpanType.AGENT and span.name == "run"
        ]
        assert len(tool_spans) == 1
        assert len(root_spans) == 1
        assert tool_spans[0].attributes["reused_result"] is True
        assert executions == 1

    asyncio.run(scenario())


def test_interrupt_resume_keeps_one_trace_and_records_lifecycle(tmp_path):
    async def scenario():
        executions = 0

        async def dangerous(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult(content="approved")

        checkpoints = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        registry = _registry(_tool("dangerous", dangerous, approval=True))
        first = _runtime(
            llm=ToolLlm("dangerous"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
        )
        waiting = await first.start(
            thread_id="thread_interrupt_trace",
            input="approve",
            run_id="run_interrupt_trace",
        )
        waiting_trace = await observations.load_trace(waiting.run_id)
        assert waiting.status == RunStatus.WAITING_APPROVAL
        assert waiting_trace is not None
        trace_id = waiting_trace.trace_id

        restarted = _runtime(
            llm=ToolLlm("dangerous"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
        )
        completed = await restarted.resume(waiting.run_id, decision="approve")
        bundle = await ObservabilityService(observations).trace(completed.run_id)
        metrics = await ObservabilityService(observations).metrics(completed.run_id)

        assert bundle is not None and metrics is not None
        assert bundle.trace.trace_id == trace_id
        assert bundle.trace.status == RunStatus.COMPLETED
        assert metrics.interrupt_count == 1
        assert metrics.resume_count == 1
        assert metrics.tool_calls == 1
        assert executions == 1

    asyncio.run(scenario())


def test_ambiguous_tool_execution_is_annotated(tmp_path):
    class SimulatedProcessCrash(BaseException):
        pass

    async def scenario():
        async def side_effect(_payload, _context):
            raise SimulatedProcessCrash()

        checkpoints = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        registry = _registry(_tool("ambiguous", side_effect))
        first = _runtime(
            llm=ToolLlm("ambiguous"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
        )
        with pytest.raises(SimulatedProcessCrash):
            await first.start(
                thread_id="thread_ambiguous_trace",
                input="side effect",
                run_id="run_ambiguous_trace",
            )

        restarted = _runtime(
            llm=ToolLlm("ambiguous"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
        )
        waiting = await restarted.resume("run_ambiguous_trace")
        bundle = await ObservabilityService(observations).trace(waiting.run_id)

        assert waiting.status == RunStatus.WAITING_APPROVAL
        assert bundle is not None
        tool_span = next(span for span in bundle.spans if span.span_type == SpanType.TOOL)
        assert tool_span.attributes["ambiguous_execution"] is True
        assert any(span.span_type == SpanType.INTERRUPT for span in bundle.spans)

    asyncio.run(scenario())


def test_crash_recovery_trace_is_continuous(tmp_path):
    class SimulatedProcessCrash(BaseException):
        pass

    async def scenario():
        async def tool_handler(_payload, _context):
            return ToolResult(content="done")

        checkpoints = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        registry = _registry(_tool("work", tool_handler))
        crashed = False

        def crash_after_tool(event_type: str, _payload: dict[str, Any]) -> None:
            nonlocal crashed
            if event_type == "tool.completed" and not crashed:
                crashed = True
                raise SimulatedProcessCrash("process crashed")

        first = _runtime(
            llm=ToolLlm("work"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
            event_sink=crash_after_tool,
        )
        with pytest.raises(SimulatedProcessCrash, match="process crashed"):
            await first.start(
                thread_id="thread_crash_trace",
                input="work",
                run_id="run_crash_trace",
            )
        before = await observations.load_trace("run_crash_trace")
        assert before is not None

        restarted = _runtime(
            llm=ToolLlm("work"),
            registry=registry,
            checkpoint_store=checkpoints,
            observability_store=observations,
            tmp_path=tmp_path,
        )
        completed = await restarted.resume("run_crash_trace")
        bundle = await ObservabilityService(observations).trace(completed.run_id)

        assert bundle is not None
        assert bundle.trace.trace_id == before.trace_id
        assert sum(span.name == "run" for span in bundle.spans) == 1
        assert any(span.attributes.get("recovered_after_crash") is True for span in bundle.spans)
        assert any(span.name == "resume" for span in bundle.spans)

    asyncio.run(scenario())


class FakeRequest:
    def __init__(self, path: str):
        self.command = "GET"
        self.path = path
        self.headers = {"content-length": "0", "x-api-key": "test-key"}
        self.rfile = BytesIO()
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


def test_sqlite_trace_metrics_api_and_cli_survive_new_instance(tmp_path):
    async def create_run(data_dir):
        database = data_dir / "runtime.db"
        runtime = _runtime(
            llm=FinalLlm(),
            registry=_registry(),
            checkpoint_store=SQLiteCheckpointStore(database),
            observability_store=SQLiteObservabilityStore(database),
            tmp_path=tmp_path,
        )
        return await runtime.start(
            thread_id="thread_sqlite_trace",
            input="persist",
            run_id="run_sqlite_trace",
        )

    data_dir = tmp_path / "runtime-data"
    completed = asyncio.run(create_run(data_dir))
    reloaded = ObservabilityService(SQLiteObservabilityStore(data_dir / "runtime.db"))
    trace = asyncio.run(reloaded.trace(completed.run_id))
    metrics = asyncio.run(reloaded.metrics(completed.run_id))

    assert trace is not None and metrics is not None
    assert trace.trace.status == RunStatus.COMPLETED
    assert metrics.total_tokens == 10

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=AxiomConfig(),
        api_key="test-key",
        workers=0,
        data_dir=data_dir,
    )
    trace_request = FakeRequest(f"/v1/runs/{completed.run_id}/trace")
    metrics_request = FakeRequest(f"/v1/runs/{completed.run_id}/metrics")
    server._handle(trace_request)
    server._handle(metrics_request)

    assert trace_request.status == 200
    assert trace_request.json()["trace"]["trace_id"] == trace.trace.trace_id
    assert metrics_request.status == 200
    assert metrics_request.json()["total_tokens"] == 10

    runner = CliRunner()
    show = runner.invoke(
        app,
        ["runs", "show", completed.run_id, "--data-dir", str(data_dir)],
    )
    rendered_trace = runner.invoke(
        app,
        ["runs", "trace", completed.run_id, "--data-dir", str(data_dir)],
    )

    assert show.exit_code == 0
    assert "Status: COMPLETED" in show.stdout
    assert "Tokens: 10" in show.stdout
    assert rendered_trace.exit_code == 0
    assert "LLM test-model" in rendered_trace.stdout


def test_runtime_api_persists_llm_and_agent_trace_events(tmp_path):
    registry = _registry()

    def engine_factory(context):
        return QueryEngine(
            llm_client=FinalLlm(),
            tool_registry=registry,
            config=context.config,
            cwd=context.cwd,
        )

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=AxiomConfig(),
        api_key="test-key",
        workers=0,
        data_dir=tmp_path / "api-observability",
        engine_factory=engine_factory,
    )
    thread_id = server.repository.create_thread()

    result = asyncio.run(server._run_turn(thread_id, "trace this"))
    events = server.repository.list_events(thread_id)
    event_types = {event.type for event in events}
    turn_started = next(event for event in events if event.type == "turn.started")
    run_id = str(turn_started.payload["run_id"])
    metrics = asyncio.run(server.observability.metrics(run_id))

    assert result == {"thread_id": thread_id, "text": "complete"}
    assert {
        "run.started",
        "agent.step.started",
        "llm.started",
        "llm.completed",
        "agent.step.completed",
        "run.completed",
    } <= event_types
    assert metrics is not None
    assert metrics.llm_calls == 1
