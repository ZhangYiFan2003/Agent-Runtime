from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.evaluation import (
    DurableEvaluationExecutor,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunner,
    ScorerSpec,
)
from axiom.execution import ExecutionRequest, ExecutionResult
from axiom.plan import ExecutionPlan, Planner, PlanStatus, Task, TaskStatus, TaskType
from axiom.policy import PermissionAction, PermissionDecision
from axiom.runtime import (
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RetryPolicy,
    RunStatus,
    RuntimeApiServer,
    SQLiteCheckpointStore,
    ToolExecutionStatus,
)
from axiom.runtime.models import ToolExecutionRecord
from axiom.runtime.observability_store import RunTracer
from axiom.runtime.plan_strategy import PlanExecuteStrategy
from axiom.tools import ToolRegistry, get_builtin_tools
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema


class ScriptedPlanClient:
    provider_name = "plan-test"
    model_name = "plan-model"
    max_context_window = 10_000

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        *,
        replan_tasks: list[dict[str, Any]] | None = None,
        tool_steps: dict[str, tuple[str, dict[str, Any]]] | None = None,
        failing_steps: set[str] | None = None,
    ) -> None:
        self.tasks = tasks
        self.replan_tasks = replan_tasks or tasks
        self.tool_steps = tool_steps or {}
        self.failing_steps = failing_steps or set()
        self.planner_calls = 0
        self.replan_calls = 0
        self.task_calls: Counter[str] = Counter()

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        body = _content(messages[-1].content)
        if "Please create an execution plan" in body:
            is_replan = "Failure reason:" in body
            if is_replan:
                self.replan_calls += 1
                tasks = self.replan_tasks
            else:
                self.planner_calls += 1
                tasks = self.tasks
            yield {
                "type": "text_delta",
                "text": json.dumps({"summary": "test plan", "tasks": tasks}),
            }
            yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 2}}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        description = _current_task(messages)
        self.task_calls[description] += 1
        if description in self.failing_steps:
            yield {"type": "error", "error": RuntimeError(f"failed: {description}")}
            return

        task_start = _current_task_start(messages)
        has_tool_result = any(message.role == "tool" for message in messages[task_start:])
        tool = self.tool_steps.get(description)
        if tool and not has_tool_result:
            name, arguments = tool
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "provider_call",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return

        tool_result = next(
            (
                _content(message.content)
                for message in reversed(messages[task_start:])
                if message.role == "tool"
            ),
            "",
        )
        suffix = f":{tool_result}" if tool_result else ""
        yield {"type": "text_delta", "text": f"result:{description}{suffix}"}
        yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class InterruptiblePlanningClient(ScriptedPlanClient):
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        super().__init__(tasks)
        self.planning_started = asyncio.Event()
        self.block_planning = True

    async def chat(self, messages, tools, *, system_prompt):
        body = _content(messages[-1].content)
        if "Please create an execution plan" in body and self.block_planning:
            self.planner_calls += 1
            self.planning_started.set()
            await asyncio.Event().wait()
            return
        async for event in super().chat(messages, tools, system_prompt=system_prompt):
            yield event


class CrashOnEvent:
    def __init__(self, event_type: str, *, step_id: str | None = None) -> None:
        self.event_type = event_type
        self.step_id = step_id
        self.triggered = False
        self.events: list[str] = []

    async def __call__(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append(event_type)
        if self.triggered or event_type != self.event_type:
            return
        if self.step_id is not None and payload.get("step_id") != self.step_id:
            return
        self.triggered = True
        raise RuntimeError(f"simulated crash after {event_type}")


class CrashAfterSucceededToolStore(MemoryCheckpointStore):
    def __init__(self) -> None:
        super().__init__()
        self.triggered = False

    async def save_tool_execution(self, record: ToolExecutionRecord) -> None:
        await super().save_tool_execution(record)
        if not self.triggered and record.status == ToolExecutionStatus.SUCCEEDED:
            self.triggered = True
            raise RuntimeError("simulated crash after tool success")


class DenyPolicy:
    async def evaluate(self, _request) -> PermissionDecision:
        return PermissionDecision(PermissionAction.DENY, "test hard deny", "test.deny")


class RecordingBackend:
    name = "restricted"

    def __init__(self) -> None:
        self.calls: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        return ExecutionResult(
            stdout="restricted-result",
            stderr="",
            exit_code=0,
            duration_ms=1.0,
            stdout_bytes=17,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            timeout_seconds=request.timeout_seconds,
            execution_backend=self.name,
            workspace=str(Path(request.workspace).resolve()),
            env_filtered_count=5,
        )


def _task(description: str, *, dependencies: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": re.sub(r"\W+", "_", description).strip("_").lower(),
        "description": description,
        "type": "ANALYSIS",
        "dependencies": dependencies or [],
    }


def _config(tmp_path, *, hitl: str = "never") -> AxiomConfig:
    config = AxiomConfig()
    config.policy.hitl_mode = hitl
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return config


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(list(tools))
    return registry


def _side_effect_tool(counter: list[dict[str, Any]]) -> Tool:
    async def execute(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
        counter.append(payload)
        return ToolResult("write-ok")

    return Tool(
        name="write_data",
        description="write test data",
        parameters=object_schema({"path": {"type": "string"}}, ["path"]),
        required_keys=["path"],
        handler=execute,
        capabilities=("filesystem.write",),
        path_argument_names=("path",),
    )


def _runtime(
    client,
    store,
    tmp_path,
    *,
    tools: ToolRegistry | None = None,
    observations=None,
    event_sink=None,
    policy=None,
    backend=None,
    hitl: str = "never",
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=client,
        tool_registry=tools or ToolRegistry(),
        system_prompt="plan system",
        cwd=str(tmp_path),
        config=_config(tmp_path, hitl=hitl),
        store=store,
        retry_policy=RetryPolicy(max_attempts=1),
        event_sink=event_sink,
        tracer=RunTracer(observations) if observations is not None else None,
        permission_policy=policy,
        execution_backend=backend,
        execution_strategy=PlanExecuteStrategy(Planner(client)),
    )


def test_plan_checkpoint_json_round_trip_preserves_versions_and_step_state():
    plan = ExecutionPlan(id="plan_1", goal="goal", version=2, replan_count=1)
    completed = Task("task_1", "done", TaskType.ANALYSIS, status=TaskStatus.COMPLETED)
    completed.attempt = 1
    completed.result = "output"
    failed = Task("task_2", "failed", TaskType.COMMAND, ["task_1"])
    failed.mark_started()
    failed.mark_failed("boom")
    plan.add_task(completed)
    plan.add_task(failed)
    plan.status = PlanStatus.FAILED
    plan.history.append(plan.snapshot("first failure"))

    restored = ExecutionPlan.from_dict(json.loads(json.dumps(plan.to_dict())))

    assert restored.schema_version == 1
    assert restored.version == 2
    assert restored.replan_count == 1
    assert restored.get_task("task_1").result == "output"
    assert restored.get_task("task_2").status == TaskStatus.FAILED
    assert restored.get_task("task_2").error == "boom"
    assert restored.history[0].reason == "first failure"


def test_plan_creation_is_persisted_in_sqlite_and_not_repeated_after_crash(tmp_path):
    async def scenario():
        database = tmp_path / "runtime.db"
        client = ScriptedPlanClient([_task("A")])
        crash = CrashOnEvent("plan.created")
        first = _runtime(client, SQLiteCheckpointStore(database), tmp_path, event_sink=crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await first.start(thread_id="thread-plan", run_id="run-plan", input="complex goal")

        persisted = await SQLiteCheckpointStore(database).load("run-plan")
        assert persisted is not None
        plan = ExecutionPlan.from_dict(persisted.strategy_state["plan"])
        assert plan.version == 1
        assert client.planner_calls == 1

        restarted = _runtime(client, SQLiteCheckpointStore(database), tmp_path)
        completed = await restarted.resume("run-plan")
        assert completed.status == RunStatus.COMPLETED
        assert client.planner_calls == 1

    asyncio.run(scenario())


def test_crash_before_plan_checkpoint_repeats_planning_safely(tmp_path):
    async def scenario():
        client = InterruptiblePlanningClient([_task("A")])
        store = MemoryCheckpointStore()
        runtime = _runtime(client, store, tmp_path)
        task = asyncio.create_task(
            runtime.start(
                thread_id="thread-planning-crash",
                run_id="run-planning-crash",
                input="complex goal",
            )
        )
        await asyncio.wait_for(client.planning_started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        persisted = await store.load("run-planning-crash")
        assert persisted.status == RunStatus.RUNNING
        assert "plan" not in persisted.strategy_state

        client.block_planning = False
        completed = await _runtime(client, store, tmp_path).resume("run-planning-crash")
        assert completed.status == RunStatus.COMPLETED
        assert client.planner_calls == 2

    asyncio.run(scenario())


def test_completed_step_is_not_reexecuted_and_next_steps_continue(tmp_path):
    async def scenario():
        client = ScriptedPlanClient(
            [_task("A"), _task("B", dependencies=["a"]), _task("C", dependencies=["b"])]
        )
        store = MemoryCheckpointStore()
        crash = CrashOnEvent("plan.step.completed", step_id="task_1")
        first = _runtime(client, store, tmp_path, event_sink=crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            await first.start(thread_id="thread-steps", run_id="run-steps", input="complex goal")

        completed = await _runtime(client, store, tmp_path).resume("run-steps")
        assert completed.status == RunStatus.COMPLETED
        assert client.task_calls["A"] == 1
        assert client.task_calls["B"] == 1
        assert client.task_calls["C"] == 1

    asyncio.run(scenario())


def test_succeeded_tool_record_is_reused_across_plan_checkpoint_gap(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        client = ScriptedPlanClient(
            [_task("Write")],
            tool_steps={"Write": ("write_data", {"path": "inside.txt"})},
        )
        store = CrashAfterSucceededToolStore()
        tools = _registry(_side_effect_tool(calls))
        first = _runtime(client, store, tmp_path, tools=tools)
        with pytest.raises(RuntimeError, match="tool success"):
            await first.start(thread_id="thread-tool", run_id="run-tool", input="complex goal")

        completed = await _runtime(client, store, tmp_path, tools=tools).resume("run-tool")
        assert completed.status == RunStatus.COMPLETED
        assert len(calls) == 1
        record = await store.load_tool_execution("run-tool:plan_v1:task_1:turn_0:provider_call")
        assert record is not None
        assert record.status == ToolExecutionStatus.SUCCEEDED

    asyncio.run(scenario())


def test_plan_write_step_waits_for_approval(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        client = ScriptedPlanClient(
            [_task("Write")],
            tool_steps={"Write": ("write_data", {"path": "inside.txt"})},
        )
        state = await _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            tools=_registry(_side_effect_tool(calls)),
            hitl="auto",
        ).start(thread_id="thread-approval", input="complex goal")
        assert state.status == RunStatus.WAITING_APPROVAL
        assert state.interrupt.invocation_id.startswith(f"{state.run_id}:plan_v1:task_1")
        assert calls == []

    asyncio.run(scenario())


def test_plan_approval_survives_sqlite_restart_and_continues_same_step(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        database = tmp_path / "approval.db"
        client = ScriptedPlanClient(
            [_task("Write"), _task("Verify", dependencies=["write"])],
            tool_steps={"Write": ("write_data", {"path": "inside.txt"})},
        )
        tools = _registry(_side_effect_tool(calls))
        waiting = await _runtime(
            client,
            SQLiteCheckpointStore(database),
            tmp_path,
            tools=tools,
            hitl="auto",
        ).start(thread_id="thread-restart", run_id="run-restart", input="complex goal")

        completed = await _runtime(
            client,
            SQLiteCheckpointStore(database),
            tmp_path,
            tools=tools,
            hitl="auto",
        ).resume(waiting.run_id, decision="approve")
        assert completed.status == RunStatus.COMPLETED
        assert len(calls) == 1
        assert client.planner_calls == 1
        assert client.task_calls["Write"] == 2
        assert client.task_calls["Verify"] == 1

    asyncio.run(scenario())


def test_plan_reject_does_not_execute_tool_and_agent_handles_result(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        store = MemoryCheckpointStore()
        client = ScriptedPlanClient(
            [_task("Write")],
            tool_steps={"Write": ("write_data", {"path": "inside.txt"})},
        )
        tools = _registry(_side_effect_tool(calls))
        waiting = await _runtime(client, store, tmp_path, tools=tools, hitl="auto").start(
            thread_id="thread-reject", input="complex goal"
        )
        completed = await _runtime(client, store, tmp_path, tools=tools, hitl="auto").resume(
            waiting.run_id, decision="reject"
        )
        assert completed.status == RunStatus.COMPLETED
        assert calls == []
        assert "denied by permission policy" in completed.output_text

    asyncio.run(scenario())


def test_hard_deny_cannot_be_bypassed_by_persisted_approval(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        store = MemoryCheckpointStore()
        client = ScriptedPlanClient(
            [_task("Write")],
            tool_steps={"Write": ("write_data", {"path": "inside.txt"})},
        )
        tools = _registry(_side_effect_tool(calls))
        waiting = await _runtime(client, store, tmp_path, tools=tools, hitl="auto").start(
            thread_id="thread-deny", input="complex goal"
        )
        completed = await _runtime(
            client,
            store,
            tmp_path,
            tools=tools,
            policy=DenyPolicy(),
            hitl="auto",
        ).resume(waiting.run_id, decision="approve")
        assert completed.status == RunStatus.COMPLETED
        assert calls == []
        assert "test hard deny" in completed.output_text

    asyncio.run(scenario())


def test_plan_shell_step_uses_restricted_execution_backend(tmp_path):
    async def scenario():
        client = ScriptedPlanClient(
            [_task("Shell")],
            tool_steps={"Shell": ("bash", {"command": "echo isolated"})},
        )
        backend = RecordingBackend()
        bash = next(tool for tool in get_builtin_tools() if tool.name == "bash")
        completed = await _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            tools=_registry(bash),
            backend=backend,
        ).start(thread_id="thread-shell", input="complex goal")
        assert completed.status == RunStatus.COMPLETED
        assert len(backend.calls) == 1
        assert backend.calls[0].invocation_id.startswith(f"{completed.run_id}:plan_v1:task_1")

    asyncio.run(scenario())


def test_replan_version_and_history_survive_crash(tmp_path):
    async def scenario():
        client = ScriptedPlanClient(
            [_task("Fail first")],
            replan_tasks=[_task("Recovery")],
            failing_steps={"Fail first"},
        )
        store = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        crash = CrashOnEvent("plan.replanned")
        with pytest.raises(RuntimeError, match="simulated crash"):
            await _runtime(
                client,
                store,
                tmp_path,
                event_sink=crash,
                observations=observations,
            ).start(thread_id="thread-replan", run_id="run-replan", input="complex goal")

        persisted = await store.load("run-replan")
        plan = ExecutionPlan.from_dict(persisted.strategy_state["plan"])
        assert plan.version == 2
        assert plan.replan_count == 1
        assert len(plan.history) == 1
        assert plan.history[0].tasks[0].status == TaskStatus.FAILED

        completed = await _runtime(
            client,
            store,
            tmp_path,
            observations=observations,
        ).resume("run-replan")
        assert completed.status == RunStatus.COMPLETED
        assert client.planner_calls == 1
        assert client.replan_calls == 1
        bundle = await ObservabilityService(observations).trace("run-replan")
        assert len([span for span in bundle.spans if span.name == "plan.replan"]) == 1

    asyncio.run(scenario())


def test_plan_trace_contains_planning_step_and_nested_llm_tool_spans(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        client = ScriptedPlanClient(
            [_task("Read")],
            tool_steps={"Read": ("read_data", {})},
        )

        async def read(_payload, _context):
            calls.append({})
            return ToolResult("read-ok")

        tool = Tool(
            name="read_data",
            description="read",
            parameters=object_schema({}),
            required_keys=[],
            handler=read,
            is_read_only=True,
            capabilities=("filesystem.read",),
        )
        observations = MemoryObservabilityStore()
        completed = await _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            tools=_registry(tool),
            observations=observations,
        ).start(thread_id="thread-trace", run_id="run-trace", input="complex goal")
        bundle = await ObservabilityService(observations).trace(completed.run_id)
        names = {span.name for span in bundle.spans}
        assert {"plan.create", "plan.step", "llm.plan", "llm.chat", "tool.read_data"} <= names
        plan_step = next(span for span in bundle.spans if span.name == "plan.step")
        agent_steps = [span for span in bundle.spans if span.name == "agent.step"]
        assert all(span.parent_span_id == plan_step.span_id for span in agent_steps)
        llm = next(span for span in bundle.spans if span.name == "llm.chat")
        assert llm.parent_span_id in {span.span_id for span in agent_steps}
        assert len(calls) == 1

    asyncio.run(scenario())


def test_external_cancellation_stops_plan_before_next_step(tmp_path):
    async def scenario():
        client = ScriptedPlanClient([_task("A"), _task("B")])
        store = MemoryCheckpointStore()
        strategy = PlanExecuteStrategy(Planner(client))
        runtime: DurableAgentRuntime

        async def cancel_after_first(event_type: str, payload: dict[str, Any]) -> None:
            if event_type != "plan.step.completed" or payload.get("step_id") != "task_1":
                return
            current = await store.load("run-cancel-plan")
            current.status = RunStatus.CANCELLED
            await strategy.on_cancel(runtime, current)
            await store.save(current)

        runtime = DurableAgentRuntime(
            llm_client=client,
            tool_registry=ToolRegistry(),
            system_prompt="plan",
            cwd=str(tmp_path),
            config=_config(tmp_path),
            store=store,
            event_sink=cancel_after_first,
            execution_strategy=strategy,
        )
        state = await runtime.start(
            thread_id="thread-cancel-plan",
            run_id="run-cancel-plan",
            input="complex goal",
        )
        persisted = await store.load(state.run_id)
        plan = ExecutionPlan.from_dict(persisted.strategy_state["plan"])
        assert state.status == RunStatus.CANCELLED
        assert plan.status == PlanStatus.CANCELLED
        assert client.task_calls["A"] == 1
        assert client.task_calls["B"] == 0

    asyncio.run(scenario())


def test_runtime_cancel_persists_cancelled_plan_and_blocks_resume(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        client = ScriptedPlanClient(
            [_task("Write")],
            tool_steps={"Write": ("write_data", {"path": "inside.txt"})},
        )
        events: list[str] = []

        async def record(event_type: str, _payload: dict[str, Any]) -> None:
            events.append(event_type)

        runtime = _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            tools=_registry(_side_effect_tool(calls)),
            event_sink=record,
            hitl="auto",
        )
        waiting = await runtime.start(thread_id="thread-cancel", input="complex goal")
        cancelled = await runtime.cancel(waiting.run_id)
        plan = ExecutionPlan.from_dict(cancelled.strategy_state["plan"])
        assert cancelled.status == RunStatus.CANCELLED
        assert plan.status == PlanStatus.CANCELLED
        assert "plan.cancelled" in events
        assert calls == []
        with pytest.raises(ValueError, match="cancelled run"):
            await runtime.resume(waiting.run_id)

    asyncio.run(scenario())


def test_runtime_api_uses_durable_plan_strategy_from_config(tmp_path):
    async def scenario():
        client = ScriptedPlanClient([_task("API task")])
        config = _config(tmp_path)
        config.prompt.agent_mode = "plan_execute"

        def engine_factory(context):
            return QueryEngine(
                llm_client=client,
                tool_registry=ToolRegistry(),
                config=context.config,
                cwd=context.cwd,
            )

        server = RuntimeApiServer(
            cwd=str(tmp_path),
            config=config,
            api_key="test-key",
            workers=0,
            data_dir=tmp_path / "api-plan",
            engine_factory=engine_factory,
        )
        thread_id = server.repository.create_thread()
        result = await server._run_turn(thread_id, "perform a complex API plan task")
        runs = await server.checkpoint_store.list(thread_id)
        assert "result:API task" in result["text"]
        assert len(runs) == 1
        assert runs[0].execution_strategy == "plan_execute"
        assert ExecutionPlan.from_dict(runs[0].strategy_state["plan"]).is_all_completed()

    asyncio.run(scenario())


def test_plan_evaluation_uses_real_durable_runtime_and_metrics(tmp_path):
    async def scenario():
        checkpoints = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()

        def engine_factory(_case):
            client = ScriptedPlanClient([_task("Evaluate")])
            return QueryEngine(
                llm_client=client,
                tool_registry=ToolRegistry(),
                config=_config(tmp_path),
                cwd=str(tmp_path),
            )

        executor = DurableEvaluationExecutor(
            engine_factory=engine_factory,
            checkpoint_store=checkpoints,
            observability_store=observations,
            execution_strategy="plan_execute",
        )
        case = EvaluationCase(
            id="plan-eval",
            prompt="perform a deterministic multi-step evaluation task",
            scorers=(
                ScorerSpec(type="run_status"),
                ScorerSpec(type="contains", config={"expected": "result:Evaluate"}),
            ),
        )
        suite = await EvaluationRunner(executor).run(
            EvaluationDataset(name="plan", version="1", cases=(case,))
        )
        result = suite.results[0]
        state = await checkpoints.load(result.run_id)
        assert result.passed
        assert result.step_count > 0
        assert result.total_tokens > 0
        assert state.execution_strategy == "plan_execute"

    asyncio.run(scenario())


def _content(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _current_task(messages) -> str:
    for message in reversed(messages):
        match = re.search(r"Current task \[[^]]+\]: ([^\n]+)", _content(message.content))
        if match:
            return match.group(1).strip()
    return "unknown"


def _current_task_start(messages) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if "Current task [" in _content(messages[index].content):
            return index
    return 0
