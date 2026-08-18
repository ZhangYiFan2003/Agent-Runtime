from __future__ import annotations

import asyncio
import json
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
from axiom.policy import PermissionAction, PermissionDecision
from axiom.runtime import (
    AssignmentStatus,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    MultiAgentExecutionStrategy,
    MultiAgentState,
    MultiAgentStatus,
    ObservabilityService,
    RetryPolicy,
    RunStatus,
    SQLiteCheckpointStore,
    ToolExecutionStatus,
    WorkerAssignment,
    child_run_id,
)
from axiom.runtime.models import ToolExecutionRecord
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry, get_builtin_tools
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema


class ScriptedTeamClient:
    provider_name = "team-test"
    model_name = "team-model"
    max_context_window = 10_000

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        *,
        tool_steps: dict[str, tuple[str, dict[str, Any]]] | None = None,
        fail_steps: set[str] | None = None,
        review_decisions: list[bool] | None = None,
    ) -> None:
        self.tasks = tasks
        self.tool_steps = tool_steps or {}
        self.fail_steps = fail_steps or set()
        self.review_decisions = list(review_decisions or [True])
        self.planner_calls = 0
        self.reviewer_calls = 0
        self.worker_calls: Counter[str] = Counter()
        self.worker_order: list[str] = []

    async def chat(self, messages, _tools, *, system_prompt):
        body = _content(messages[-1].content)
        if "Planner in a multi-agent workflow" in system_prompt:
            self.planner_calls += 1
            yield {"type": "text_delta", "text": json.dumps({"steps": self.tasks})}
            yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 2}}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "You are the Reviewer" in system_prompt:
            index = min(self.reviewer_calls, len(self.review_decisions) - 1)
            approved = self.review_decisions[index]
            self.reviewer_calls += 1
            yield {
                "type": "text_delta",
                "text": json.dumps(
                    {
                        "approved": approved,
                        "summary": "ok" if approved else "revise",
                        "issues": [] if approved else ["fix result"],
                    }
                ),
            }
            yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        worker_body = next(
            (_content(message.content) for message in reversed(messages) if message.role == "user"),
            body,
        )
        task = _current_task(worker_body)
        has_tool_result = any(message.role == "tool" for message in messages)
        if not has_tool_result:
            self.worker_calls[task] += 1
            self.worker_order.append(task)
        if task in self.fail_steps:
            yield {"type": "error", "error": RuntimeError(f"worker failed: {task}")}
            return
        tool = self.tool_steps.get(task)
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
            (_content(message.content) for message in reversed(messages) if message.role == "tool"),
            "",
        )
        suffix = f":{tool_result}" if tool_result else ""
        yield {
            "type": "text_delta",
            "text": f"worker-result:{task}:attempt-{self.worker_calls[task]}{suffix}",
        }
        yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class CrashOnEvent:
    def __init__(self, event_type: str) -> None:
        self.event_type = event_type
        self.triggered = False
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append((event_type, payload))
        if event_type == self.event_type and not self.triggered:
            self.triggered = True
            raise RuntimeError(f"simulated crash after {event_type}")


class CrashAfterSucceededToolStore(MemoryCheckpointStore):
    def __init__(self) -> None:
        super().__init__()
        self.triggered = False

    async def save_tool_execution(self, record: ToolExecutionRecord) -> None:
        await super().save_tool_execution(record)
        if record.status == ToolExecutionStatus.SUCCEEDED and not self.triggered:
            self.triggered = True
            raise RuntimeError("simulated crash after child tool success")


class AllowPolicy:
    async def evaluate(self, _request) -> PermissionDecision:
        return PermissionDecision(PermissionAction.ALLOW, "test allow", "test.allow")


class DenyPolicy:
    async def evaluate(self, _request) -> PermissionDecision:
        return PermissionDecision(PermissionAction.DENY, "test deny", "test.deny")


class RecordingBackend:
    name = "restricted"

    def __init__(self) -> None:
        self.calls: list[ExecutionRequest] = []

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        return ExecutionResult(
            stdout="restricted-ok",
            stderr="",
            exit_code=0,
            duration_ms=1.0,
            stdout_bytes=13,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            timeout_seconds=request.timeout_seconds,
            execution_backend=self.name,
            workspace=str(Path(request.workspace).resolve()),
            env_filtered_count=3,
        )


def _task(
    task_id: str,
    description: str,
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "description": description,
        "type": "worker",
        "dependencies": dependencies or [],
    }


def _config(tmp_path, *, hitl: str = "never") -> AxiomConfig:
    config = AxiomConfig()
    config.policy.hitl_mode = hitl
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return config


def _side_effect_tool(calls: list[dict[str, Any]]) -> Tool:
    async def execute(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
        calls.append(payload)
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


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(list(tools))
    return registry


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
    strategy: MultiAgentExecutionStrategy | None = None,
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=client,
        tool_registry=tools or ToolRegistry(),
        system_prompt="team system",
        cwd=str(tmp_path),
        config=_config(tmp_path, hitl=hitl),
        store=store,
        retry_policy=RetryPolicy(max_attempts=1),
        event_sink=event_sink,
        tracer=RunTracer(observations) if observations is not None else None,
        permission_policy=policy,
        execution_backend=backend,
        execution_strategy=strategy or MultiAgentExecutionStrategy(),
    )


def test_multi_agent_strategy_state_json_round_trip():
    original = MultiAgentState(
        orchestration_goal="goal",
        status=MultiAgentStatus.WAITING_CHILD,
        assignments=[
            WorkerAssignment(
                assignment_id="assignment_1",
                worker_role="researcher",
                task="find answer",
                status=AssignmentStatus.WAITING_CHILD,
                child_run_id="run_child_1",
                result="partial",
                error="",
                attempt=2,
                review_output='{"approved": false}',
                review_approved=False,
                review_issues="revise",
            )
        ],
        current_assignment_id="assignment_1",
        planner_output="{}",
        review_rounds=1,
    )

    restored = MultiAgentState.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.schema_version == 1
    assert restored.status == MultiAgentStatus.WAITING_CHILD
    assert restored.assignments[0].child_run_id == "run_child_1"
    assert restored.assignments[0].attempt == 2
    assert restored.assignments[0].review_approved is False


def test_parent_creates_child_in_same_thread_and_turn(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        runtime = _runtime(ScriptedTeamClient([_task("a", "A")]), store, tmp_path)
        parent = await runtime.start(
            thread_id="thread-team",
            turn_id="turn-team",
            run_id="run-parent",
            input="goal",
        )
        orchestration = MultiAgentExecutionStrategy.load_state(parent)
        child = await store.load(orchestration.assignments[0].child_run_id)

        assert child.thread_id == parent.thread_id
        assert child.turn_id == parent.turn_id
        assert child.run_id != parent.run_id
        assert parent.run_kind == "orchestrator"
        assert child.parent_run_id == parent.run_id
        assert child.parent_step_id
        assert child.run_kind == "worker"

    asyncio.run(scenario())


def test_stable_child_identity_survives_crash_before_child_creation(tmp_path):
    async def scenario():
        client = ScriptedTeamClient([_task("a", "A")])
        store = MemoryCheckpointStore()
        crash = CrashOnEvent("worker.assigned")
        with pytest.raises(RuntimeError, match="simulated crash"):
            await _runtime(client, store, tmp_path, event_sink=crash).start(
                thread_id="thread-stable",
                run_id="run-stable",
                input="goal",
            )
        persisted = await store.load("run-stable")
        orchestration = MultiAgentExecutionStrategy.load_state(persisted)
        expected = child_run_id("run-stable", "assignment_1", 1)
        assert orchestration.assignments[0].child_run_id == expected
        assert await store.load(expected) is None

        completed = await _runtime(client, store, tmp_path).resume("run-stable")
        runs = await store.list("thread-stable")
        assert completed.status == RunStatus.COMPLETED
        assert [item.run_id for item in runs].count(expected) == 1
        assert client.planner_calls == 1

    asyncio.run(scenario())


def test_child_completion_is_reused_when_parent_has_not_observed_it(tmp_path):
    async def scenario():
        client = ScriptedTeamClient([_task("a", "A")])
        store = MemoryCheckpointStore()
        crash = CrashOnEvent("run.completed")
        with pytest.raises(RuntimeError, match="simulated crash"):
            await _runtime(client, store, tmp_path, event_sink=crash).start(
                thread_id="thread-observe",
                run_id="run-observe",
                input="goal",
            )
        persisted = await store.load("run-observe")
        orchestration = MultiAgentExecutionStrategy.load_state(persisted)
        child_id = orchestration.assignments[0].child_run_id
        assert (await store.load(child_id)).status == RunStatus.COMPLETED

        completed = await _runtime(client, store, tmp_path).resume("run-observe")
        assert completed.status == RunStatus.COMPLETED
        assert client.worker_calls["A"] == 1
        worker_runs = [
            item for item in await store.list("thread-observe") if item.run_kind == "worker"
        ]
        assert len(worker_runs) == 1

    asyncio.run(scenario())


def test_multiple_workers_execute_sequentially_with_dependency_context(tmp_path):
    async def scenario():
        client = ScriptedTeamClient([_task("a", "A"), _task("b", "B", ["a"])])
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path).start(
            thread_id="thread-sequential",
            run_id="run-sequential",
            input="goal",
        )
        assert state.status == RunStatus.COMPLETED
        assert client.worker_order == ["A", "B"]
        assert "worker-result:A" in state.output_text
        assert "worker-result:B" in state.output_text

    asyncio.run(scenario())


def test_child_tool_execution_is_reused_after_success_checkpoint_gap(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        store = CrashAfterSucceededToolStore()
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        tools = _registry(_side_effect_tool(calls))
        with pytest.raises(RuntimeError, match="child tool success"):
            await _runtime(
                client,
                store,
                tmp_path,
                tools=tools,
                policy=AllowPolicy(),
            ).start(thread_id="thread-tool-reuse", run_id="run-tool-reuse", input="goal")

        completed = await _runtime(
            client,
            store,
            tmp_path,
            tools=tools,
            policy=AllowPolicy(),
        ).resume("run-tool-reuse")
        assert completed.status == RunStatus.COMPLETED
        assert len(calls) == 1

    asyncio.run(scenario())


def test_child_approval_sets_parent_waiting_child(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        store = MemoryCheckpointStore()
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        parent = await _runtime(
            client,
            store,
            tmp_path,
            tools=_registry(_side_effect_tool(calls)),
            hitl="always",
        ).start(thread_id="thread-approval", run_id="run-approval", input="goal")
        orchestration = MultiAgentExecutionStrategy.load_state(parent)
        child = await store.load(orchestration.assignments[0].child_run_id)
        assert parent.status == RunStatus.WAITING_CHILD
        assert child.status == RunStatus.WAITING_APPROVAL
        assert calls == []

    asyncio.run(scenario())


def test_child_approval_survives_sqlite_restart_and_parent_continues(tmp_path):
    async def scenario():
        database = tmp_path / "runtime.db"
        calls: list[dict[str, Any]] = []
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        tools = _registry(_side_effect_tool(calls))
        first_strategy = MultiAgentExecutionStrategy()
        first = _runtime(
            client,
            SQLiteCheckpointStore(database),
            tmp_path,
            tools=tools,
            hitl="always",
            strategy=first_strategy,
        )
        waiting = await first.start(
            thread_id="thread-sql-approval",
            run_id="run-sql-approval",
            input="goal",
        )
        assert waiting.status == RunStatus.WAITING_CHILD

        strategy = MultiAgentExecutionStrategy()
        restarted = _runtime(
            client,
            SQLiteCheckpointStore(database),
            tmp_path,
            tools=tools,
            hitl="always",
            strategy=strategy,
        )
        await strategy.resume_waiting_child(restarted, waiting, decision="approve")
        completed = await restarted.resume(waiting.run_id)
        assert completed.status == RunStatus.COMPLETED
        assert calls == [{"path": "inside.txt"}]

    asyncio.run(scenario())


def test_child_rejection_does_not_execute_tool(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        store = MemoryCheckpointStore()
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        strategy = MultiAgentExecutionStrategy()
        runtime = _runtime(
            client,
            store,
            tmp_path,
            tools=_registry(_side_effect_tool(calls)),
            hitl="always",
            strategy=strategy,
        )
        waiting = await runtime.start(
            thread_id="thread-reject",
            run_id="run-reject",
            input="goal",
        )
        await strategy.resume_waiting_child(runtime, waiting, decision="reject")
        completed = await runtime.resume(waiting.run_id)
        assert completed.status == RunStatus.COMPLETED
        assert calls == []

    asyncio.run(scenario())


def test_hard_deny_cannot_be_overridden_by_parent(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        state = await _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            tools=_registry(_side_effect_tool(calls)),
            policy=DenyPolicy(),
        ).start(thread_id="thread-deny", run_id="run-deny", input="goal")
        assert state.status == RunStatus.COMPLETED
        assert calls == []
        assert "denied" in state.output_text.lower()

    asyncio.run(scenario())


def test_child_shell_uses_restricted_execution_backend(tmp_path):
    async def scenario():
        bash = next(tool for tool in get_builtin_tools() if tool.name == "bash")
        backend = RecordingBackend()
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("bash", {"command": "echo ok"})},
        )
        state = await _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            tools=_registry(bash),
            policy=AllowPolicy(),
            backend=backend,
        ).start(thread_id="thread-backend", run_id="run-backend", input="goal")
        assert state.status == RunStatus.COMPLETED
        assert len(backend.calls) == 1
        assert backend.calls[0].run_id.startswith("run_child_")
        assert backend.calls[0].workspace == str(tmp_path)

    asyncio.run(scenario())


def test_parent_cancel_cancels_waiting_child_and_stops_scheduling(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        store = MemoryCheckpointStore()
        client = ScriptedTeamClient(
            [_task("a", "A"), _task("b", "B")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        runtime = _runtime(
            client,
            store,
            tmp_path,
            tools=_registry(_side_effect_tool(calls)),
            hitl="always",
        )
        waiting = await runtime.start(
            thread_id="thread-cancel",
            run_id="run-cancel",
            input="goal",
        )
        orchestration = MultiAgentExecutionStrategy.load_state(waiting)
        child_id = orchestration.assignments[0].child_run_id
        cancelled = await runtime.cancel(waiting.run_id)
        assert cancelled.status == RunStatus.CANCELLED
        assert (await store.load(child_id)).status == RunStatus.CANCELLED
        assert client.worker_calls["B"] == 0
        assert calls == []

    asyncio.run(scenario())


def test_child_failure_is_handled_by_strategy_before_parent_completion(tmp_path):
    async def scenario():
        client = ScriptedTeamClient(
            [_task("a", "Fail"), _task("b", "Success")],
            fail_steps={"Fail"},
        )
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path).start(
            thread_id="thread-failure",
            run_id="run-failure",
            input="goal",
        )
        orchestration = MultiAgentExecutionStrategy.load_state(state)
        assert state.status == RunStatus.COMPLETED
        assert orchestration.assignments[0].status == AssignmentStatus.FAILED
        assert orchestration.assignments[1].status == AssignmentStatus.COMPLETED
        assert "did not fully complete" in state.output_text

    asyncio.run(scenario())


def test_reviewer_retry_creates_new_attempt_child_without_rewriting_history(tmp_path):
    async def scenario():
        client = ScriptedTeamClient(
            [_task("a", "A")],
            review_decisions=[False, True],
        )
        store = MemoryCheckpointStore()
        state = await _runtime(client, store, tmp_path).start(
            thread_id="thread-worker-retry",
            run_id="run-worker-retry",
            input="goal",
        )
        orchestration = MultiAgentExecutionStrategy.load_state(state)
        assignment = orchestration.assignments[0]
        runs = await store.list("thread-worker-retry")
        expected = {
            child_run_id(state.run_id, assignment.assignment_id, 1),
            child_run_id(state.run_id, assignment.assignment_id, 2),
        }
        assert state.status == RunStatus.COMPLETED
        assert assignment.attempt == 2
        assert {item.run_id for item in runs if item.run_kind == "worker"} == expected
        assert client.worker_calls["A"] == 2

    asyncio.run(scenario())


def test_cancelled_child_is_observed_without_automatically_cancelling_parent(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        store = MemoryCheckpointStore()
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        strategy = MultiAgentExecutionStrategy()
        runtime = _runtime(
            client,
            store,
            tmp_path,
            tools=_registry(_side_effect_tool(calls)),
            hitl="always",
            strategy=strategy,
        )
        waiting = await runtime.start(
            thread_id="thread-child-cancel",
            run_id="run-child-cancel",
            input="goal",
        )
        orchestration = MultiAgentExecutionStrategy.load_state(waiting)
        assignment = orchestration.assignments[0]
        await strategy._child_runtime(runtime, assignment).cancel(assignment.child_run_id)
        completed = await runtime.resume(waiting.run_id)
        restored = MultiAgentExecutionStrategy.load_state(completed)
        assert completed.status == RunStatus.COMPLETED
        assert restored.assignments[0].status == AssignmentStatus.CANCELLED
        assert calls == []

    asyncio.run(scenario())


def test_review_checkpoint_prevents_duplicate_reviewer_call(tmp_path):
    async def scenario():
        client = ScriptedTeamClient([_task("a", "A")])
        store = MemoryCheckpointStore()
        crash = CrashOnEvent("review.completed")
        with pytest.raises(RuntimeError, match="simulated crash"):
            await _runtime(client, store, tmp_path, event_sink=crash).start(
                thread_id="thread-review-crash",
                run_id="run-review-crash",
                input="goal",
            )
        assert client.reviewer_calls == 1
        completed = await _runtime(client, store, tmp_path).resume("run-review-crash")
        assert completed.status == RunStatus.COMPLETED
        assert client.reviewer_calls == 1

    asyncio.run(scenario())


def test_synthesis_checkpoint_prevents_duplicate_synthesis(tmp_path):
    async def scenario():
        client = ScriptedTeamClient([_task("a", "A")])
        store = MemoryCheckpointStore()
        crash = CrashOnEvent("synthesis.completed")
        with pytest.raises(RuntimeError, match="simulated crash"):
            await _runtime(client, store, tmp_path, event_sink=crash).start(
                thread_id="thread-synthesis-crash",
                run_id="run-synthesis-crash",
                input="goal",
            )
        persisted = await store.load("run-synthesis-crash")
        before = MultiAgentExecutionStrategy.load_state(persisted).synthesis_result
        completed = await _runtime(client, store, tmp_path).resume("run-synthesis-crash")
        after = MultiAgentExecutionStrategy.load_state(completed).synthesis_result
        assert completed.status == RunStatus.COMPLETED
        assert before == after
        assert client.planner_calls == 1
        assert client.reviewer_calls == 1

    asyncio.run(scenario())


def test_parent_and_child_traces_have_queryable_lineage(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        state = await _runtime(
            ScriptedTeamClient([_task("a", "A")]),
            store,
            tmp_path,
            observations=observations,
        ).start(thread_id="thread-trace", run_id="run-trace", input="goal")
        orchestration = MultiAgentExecutionStrategy.load_state(state)
        assignment = orchestration.assignments[0]
        service = ObservabilityService(observations)
        parent_bundle = await service.trace(state.run_id)
        child_bundle = await service.trace(assignment.child_run_id)
        worker_span = next(
            span for span in parent_bundle.spans if span.name == "multi_agent.worker"
        )
        child_root = next(span for span in child_bundle.spans if span.name == "run")
        assert child_root.parent_span_id == worker_span.span_id
        assert child_root.attributes["parent_run_id"] == state.run_id
        assert child_root.attributes["run_kind"] == "worker"

    asyncio.run(scenario())


def test_multi_agent_event_taxonomy_is_emitted(tmp_path):
    async def scenario():
        events: list[tuple[str, dict[str, Any]]] = []
        await _runtime(
            ScriptedTeamClient([_task("a", "A")]),
            MemoryCheckpointStore(),
            tmp_path,
            event_sink=lambda name, payload: events.append((name, payload)),
        ).start(thread_id="thread-events", run_id="run-events", input="goal")
        names = [name for name, _ in events]
        for expected in (
            "multi_agent.started",
            "worker.assigned",
            "worker.started",
            "worker.completed",
            "review.started",
            "review.completed",
            "synthesis.started",
            "synthesis.completed",
            "multi_agent.completed",
        ):
            assert expected in names

    asyncio.run(scenario())


def test_multi_agent_evaluation_uses_real_child_run_and_metrics(tmp_path):
    async def scenario():
        calls: list[dict[str, Any]] = []
        client = ScriptedTeamClient(
            [_task("a", "A")],
            tool_steps={"A": ("write_data", {"path": "inside.txt"})},
        )
        tool = _side_effect_tool(calls)

        def engine_factory(_case):
            return QueryEngine(
                llm_client=client,
                tool_registry=_registry(tool),
                config=_config(tmp_path),
                cwd=str(tmp_path),
            )

        executor = DurableEvaluationExecutor(
            engine_factory=engine_factory,
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=MemoryObservabilityStore(),
            execution_strategy="multi_agent",
        )
        dataset = EvaluationDataset(
            name="multi-agent-smoke",
            version="1",
            cases=(
                EvaluationCase(
                    id="team_case",
                    name="team case",
                    prompt="goal",
                    scorers=(
                        ScorerSpec(type="run_status", config={"expected": "COMPLETED"}),
                        ScorerSpec(type="contains", config={"expected": "worker-result:A"}),
                        ScorerSpec(
                            type="tool_usage",
                            config={"required_tools": ["write_data"]},
                        ),
                    ),
                ),
            ),
        )
        suite = await EvaluationRunner(executor).run(dataset)
        result = suite.results[0]
        assert result.passed
        assert result.tool_calls == ["write_data"]
        assert result.total_tokens > 0
        assert result.step_count > 0
        assert calls == [{"path": "inside.txt"}]

    asyncio.run(scenario())


def _content(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _current_task(body: str) -> str:
    marker = "Current task:\n"
    return body.split(marker, 1)[1].split("\n\n", 1)[0].strip() if marker in body else body
