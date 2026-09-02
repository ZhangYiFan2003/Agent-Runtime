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
from axiom.evaluation import DurableEvaluationExecutor, EvaluationCase
from axiom.plan import ExecutionPlan, Planner, PlanStatus, Task, TaskStatus, TaskType
from axiom.runtime import (
    ActiveRunSupervisor,
    Checkpoint,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RetryPolicy,
    RunStatus,
    SQLiteCheckpointStore,
    plan_task_child_run_id,
)
from axiom.runtime.api import RuntimeApiServer
from axiom.runtime.checkpoints import CheckpointConflictError
from axiom.runtime.control_plane import child_view, run_view
from axiom.runtime.models import Interrupt
from axiom.runtime.observability_store import RunTracer
from axiom.runtime.plan_strategy import PlanExecuteStrategy
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema


def _task(description: str, dependencies: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": re.sub(r"\W+", "_", description).strip("_").lower(),
        "description": description,
        "type": "ANALYSIS",
        "dependencies": dependencies or [],
    }


class ParallelPlanClient:
    provider_name = "parallel-plan-test"
    model_name = "parallel-plan-model"
    max_context_window = 10_000

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        *,
        replan_tasks: list[dict[str, Any]] | None = None,
        barrier_size: int = 0,
        approval_tasks: set[str] | None = None,
        fail_tasks: set[str] | None = None,
        hold_tasks: set[str] | None = None,
        b_waits_for_c: bool = False,
    ) -> None:
        self.tasks = tasks
        self.replan_tasks = replan_tasks or tasks
        self.barrier_size = barrier_size
        self.approval_tasks = approval_tasks or set()
        self.fail_tasks = fail_tasks or set()
        self.hold_tasks = hold_tasks or set()
        self.b_waits_for_c = b_waits_for_c
        self.planner_calls = 0
        self.replan_calls = 0
        self.task_calls: Counter[str] = Counter()
        self.active = 0
        self.peak_concurrency = 0
        self.timeline: list[str] = []
        self.started: set[str] = set()
        self.barrier = asyncio.Event()
        self.failed = asyncio.Event()
        self.c_started = asyncio.Event()
        self.release: dict[str, asyncio.Event] = {task: asyncio.Event() for task in self.hold_tasks}
        self.contexts: dict[str, list[str]] = {}

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        body = str(messages[-1].content)
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
                "text": json.dumps({"summary": "parallel plan", "tasks": tasks}),
            }
            yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 2}}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        description = _current_task(messages)
        self.contexts.setdefault(description, []).append(body)
        has_tool_result = any(message.role == "tool" for message in messages)
        if not has_tool_result:
            self.task_calls[description] += 1
        if description in self.approval_tasks and not has_tool_result:
            self.timeline.extend((f"start:{description}", f"waiting:{description}"))
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": f"call-{description}",
                    "function": {
                        "name": "write_data",
                        "arguments": json.dumps({"path": f"{description}.txt"}),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return

        self.active += 1
        self.peak_concurrency = max(self.peak_concurrency, self.active)
        self.timeline.append(f"start:{description}")
        self.started.add(description)
        if description == "C":
            self.c_started.set()
        if self.barrier_size and len(self.started) >= self.barrier_size:
            self.barrier.set()
        if self.barrier_size and description in {"A", "B"}:
            await self.barrier.wait()
        if self.b_waits_for_c and description == "B":
            await asyncio.wait_for(self.c_started.wait(), timeout=2)
        if description in self.fail_tasks:
            self.timeline.append(f"failed:{description}")
            self.active -= 1
            self.failed.set()
            yield {"type": "error", "error": RuntimeError(f"failed:{description}")}
            return
        if description in self.hold_tasks:
            await self.release[description].wait()
        await asyncio.sleep(0.01)
        self.timeline.append(f"end:{description}")
        self.active -= 1
        yield {"type": "text_delta", "text": f"result:{description}"}
        yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class CrashOnFirstPlanTaskStart:
    def __init__(self) -> None:
        self.triggered = False

    async def __call__(self, event_type: str, _payload: dict[str, Any]) -> None:
        if event_type == "plan.step.started" and not self.triggered:
            self.triggered = True
            raise RuntimeError("crash after durable plan task identities")


class ConflictStore(MemoryCheckpointStore):
    def __init__(self, *, inject_once: bool = False) -> None:
        super().__init__()
        self.inject_once = inject_once
        self.injected = False
        self.conflicts = 0

    async def save(self, checkpoint: Checkpoint) -> None:
        raw = checkpoint.strategy_state.get("plan")
        plan = ExecutionPlan.from_dict(raw) if isinstance(raw, dict) else None
        observing = bool(
            plan and any(task.status == TaskStatus.COMPLETED for task in plan.all_tasks())
        )
        if self.inject_once and observing and not self.injected:
            self.injected = True
            self.conflicts += 1
            raise CheckpointConflictError("injected plan parent CAS conflict")
        try:
            await super().save(checkpoint)
        except CheckpointConflictError:
            self.conflicts += 1
            raise


def _config(tmp_path: Path, *, parallelism: int = 2, hitl: str = "never") -> AxiomConfig:
    config = AxiomConfig()
    config.plan.max_parallel_tasks = parallelism
    config.policy.hitl_mode = hitl
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return config


def _write_tool(calls: list[str]) -> Tool:
    async def execute(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
        calls.append(str(payload["path"]))
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
    client: ParallelPlanClient,
    store,
    tmp_path: Path,
    *,
    parallelism: int = 2,
    hitl: str = "never",
    tools: ToolRegistry | None = None,
    event_sink=None,
    observations=None,
    strategy: PlanExecuteStrategy | None = None,
    supervisor: ActiveRunSupervisor | None = None,
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=client,
        tool_registry=tools or ToolRegistry(),
        system_prompt="parallel plan system",
        cwd=str(tmp_path),
        config=_config(tmp_path, parallelism=parallelism, hitl=hitl),
        store=store,
        retry_policy=RetryPolicy(max_attempts=1),
        event_sink=event_sink,
        tracer=RunTracer(observations) if observations is not None else None,
        execution_strategy=strategy or PlanExecuteStrategy(Planner(client)),
        active_run_supervisor=supervisor,
    )


def test_plan_parallelism_two_has_real_overlap_and_bounded_refill(tmp_path):
    async def scenario():
        client = ParallelPlanClient([_task("A"), _task("B"), _task("C")], barrier_size=2)
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path).start(
            thread_id="thread-overlap", run_id="run-overlap", input="complex goal"
        )

        assert state.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 2
        assert max(client.timeline.index("start:A"), client.timeline.index("start:B")) < min(
            client.timeline.index("end:A"), client.timeline.index("end:B")
        )
        assert client.timeline.index("start:C") > min(
            client.timeline.index("end:A"), client.timeline.index("end:B")
        )

    asyncio.run(scenario())


def test_plan_parallelism_one_preserves_sequential_order(tmp_path):
    async def scenario():
        client = ParallelPlanClient([_task("A"), _task("B"), _task("C")])
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path, parallelism=1).start(
            thread_id="thread-one", run_id="run-one", input="complex goal"
        )

        assert state.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 1
        assert [item for item in client.timeline if item.startswith("start:")] == [
            "start:A",
            "start:B",
            "start:C",
        ]

    asyncio.run(scenario())


def test_parent_cancel_signals_active_plan_children_and_stops_refill(tmp_path):
    async def scenario():
        client = ParallelPlanClient(
            [_task("A"), _task("B"), _task("C")],
            hold_tasks={"A", "B"},
        )
        store = MemoryCheckpointStore()
        supervisor = ActiveRunSupervisor()
        runtime = _runtime(client, store, tmp_path, supervisor=supervisor)
        parent_task = asyncio.create_task(
            runtime.start(
                thread_id="thread-plan-supervisor-cancel",
                run_id="run-plan-supervisor-cancel",
                input="complex goal",
            )
        )
        for _attempt in range(200):
            if client.started == {"A", "B"}:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("plan children did not start")
        assert len(supervisor.list_active()) == 3

        cancelled = await _runtime(
            client,
            store,
            tmp_path,
            supervisor=supervisor,
        ).cancel("run-plan-supervisor-cancel")
        observed = await asyncio.wait_for(parent_task, timeout=3)
        children = [
            state
            for state in await store.list("thread-plan-supervisor-cancel")
            if state.parent_run_id == cancelled.run_id
        ]

        assert cancelled.status == RunStatus.CANCELLED
        assert observed.status == RunStatus.CANCELLED
        assert len(children) == 2
        assert {child.status for child in children} == {RunStatus.CANCELLED}
        assert "C" not in client.started
        assert supervisor.list_active() == ()

    asyncio.run(scenario())


def test_plan_dependency_join_waits_for_both_parallel_dependencies(tmp_path):
    async def scenario():
        client = ParallelPlanClient(
            [_task("A"), _task("B"), _task("C", ["a", "b"])],
            barrier_size=2,
        )
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path).start(
            thread_id="thread-join", run_id="run-join", input="complex goal"
        )

        assert state.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 2
        assert client.timeline.index("start:C") > client.timeline.index("end:A")
        assert client.timeline.index("start:C") > client.timeline.index("end:B")
        assert "result:A" in client.contexts["C"][0]
        assert "result:B" in client.contexts["C"][0]

    asyncio.run(scenario())


def test_plan_child_identities_are_persisted_before_any_spawn(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        client = ParallelPlanClient([_task("A"), _task("B"), _task("C")])
        with pytest.raises(RuntimeError, match="durable plan task identities"):
            await _runtime(
                client,
                store,
                tmp_path,
                event_sink=CrashOnFirstPlanTaskStart(),
            ).start(thread_id="thread-spawn", run_id="run-spawn", input="complex goal")

        persisted = await store.load("run-spawn")
        before = ExecutionPlan.from_dict(persisted.strategy_state["plan"])
        ids = [task.child_run_id for task in before.all_tasks()]
        assert all(ids)
        assert len(set(ids)) == 3
        assert all(task.status == TaskStatus.RUNNING for task in before.all_tasks())

        completed = await _runtime(client, store, tmp_path).resume("run-spawn")
        after = ExecutionPlan.from_dict(completed.strategy_state["plan"])
        runs = await store.list(completed.thread_id)
        assert completed.status == RunStatus.COMPLETED
        assert [task.child_run_id for task in after.all_tasks()] == ids
        assert sum(run.run_kind == "plan_task" for run in runs) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("inject_conflict", [False, True])
def test_plan_parallel_terminal_observation_is_idempotent_and_cas_safe(tmp_path, inject_conflict):
    async def scenario():
        store = ConflictStore(inject_once=inject_conflict)
        parent = Checkpoint.create(
            thread_id="thread-cas",
            turn_id="turn-cas",
            run_id="run-cas",
            input="goal",
            execution_strategy="plan_execute",
        )
        plan = ExecutionPlan("plan-cas", "goal", status=PlanStatus.RUNNING)
        for index, name in enumerate(("A", "B"), start=1):
            task = Task(f"task_{index}", name, TaskType.ANALYSIS)
            task.mark_started()
            task.child_run_id = plan_task_child_run_id(parent.run_id, 1, task.id, task.attempt)
            task.execution_state = "ASSIGNED"
            plan.add_task(task)
        parent.strategy_state["plan"] = plan.to_dict()
        await store.save(parent)
        for task in plan.all_tasks():
            child = _child_checkpoint(parent, task, RunStatus.COMPLETED)
            child.output_text = f"result:{task.description}"
            child.total_tokens = 5
            await store.save(child)

        strategy = PlanExecuteStrategy(Planner(ParallelPlanClient([])))
        runtime = _runtime(ParallelPlanClient([]), store, tmp_path, strategy=strategy)
        await strategy._reconcile_children(runtime, await store.load(parent.run_id))
        await strategy._reconcile_children(runtime, await store.load(parent.run_id))
        restored = await store.load(parent.run_id)
        restored_plan = ExecutionPlan.from_dict(restored.strategy_state["plan"])

        assert [task.result for task in restored_plan.all_tasks()] == ["result:A", "result:B"]
        assert restored.total_tokens == 10
        if inject_conflict:
            assert store.conflicts >= 1

    asyncio.run(scenario())


def test_plan_waiting_approval_releases_slot_for_ready_task(tmp_path):
    async def scenario():
        calls: list[str] = []
        client = ParallelPlanClient(
            [_task("A"), _task("B"), _task("C")],
            approval_tasks={"A"},
            b_waits_for_c=True,
        )
        store = MemoryCheckpointStore()
        parent = await _runtime(
            client,
            store,
            tmp_path,
            hitl="always",
            tools=_registry(_write_tool(calls)),
        ).start(thread_id="thread-slot", run_id="run-slot", input="complex goal")
        plan = ExecutionPlan.from_dict(parent.strategy_state["plan"])
        child_a = await store.load(plan.get_task("task_1").child_run_id)

        assert parent.status == RunStatus.WAITING_CHILD
        assert child_a.status == RunStatus.WAITING_APPROVAL
        assert client.c_started.is_set()
        assert client.timeline.index("start:C") < client.timeline.index("end:B")
        assert calls == []

    asyncio.run(scenario())


def test_plan_multiple_approvals_are_independent_and_restart_safe(tmp_path):
    async def scenario():
        calls: list[str] = []
        database = tmp_path / "approval.db"
        client = ParallelPlanClient([_task("A"), _task("B")], approval_tasks={"A", "B"})
        tools = _registry(_write_tool(calls))
        first = _runtime(
            client,
            SQLiteCheckpointStore(database),
            tmp_path,
            hitl="always",
            tools=tools,
        )
        parent = await first.start(
            thread_id="thread-approval", run_id="run-approval", input="complex goal"
        )
        plan = ExecutionPlan.from_dict(parent.strategy_state["plan"])
        child_ids = [task.child_run_id for task in plan.all_tasks()]
        children = [await first.store.load(run_id) for run_id in child_ids]
        assert len(run_view(parent, children=children)["pending_interrupts"]) == 2

        restarted = _runtime(
            client,
            SQLiteCheckpointStore(database),
            tmp_path,
            hitl="always",
            tools=tools,
        )
        await restarted.execution_strategy.resume_waiting_child(
            restarted,
            parent,
            child_run_id=child_ids[0],
            decision="approve",
        )
        parent = await restarted.resume(parent.run_id)
        assert parent.status == RunStatus.WAITING_CHILD
        assert (await restarted.store.load(child_ids[1])).status == RunStatus.WAITING_APPROVAL
        assert calls == ["A.txt"]
        assert (
            ExecutionPlan.from_dict(parent.strategy_state["plan"]).get_task("task_1").child_run_id
            == child_ids[0]
        )

    asyncio.run(scenario())


def test_runtime_api_lists_parallel_plan_children_and_pending_interrupts(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        parent = Checkpoint.create(
            thread_id="thread-api-plan",
            turn_id="turn-api-plan",
            run_id="run-api-plan",
            input="complex goal",
            execution_strategy="plan_execute",
        )
        parent.status = RunStatus.WAITING_CHILD
        plan = ExecutionPlan(id="plan-api", goal="complex goal", status=PlanStatus.RUNNING)
        for description in ("A", "B"):
            task = Task(id=description.lower(), description=description)
            task.mark_started()
            task.child_run_id = plan_task_child_run_id(
                parent.run_id,
                plan.version,
                task.id,
                task.attempt,
            )
            plan.add_task(task)
        parent.strategy_state["plan"] = plan.to_dict()
        await store.save(parent)

        children: list[Checkpoint] = []
        for task in plan.all_tasks():
            child = _child_checkpoint(parent, task, RunStatus.WAITING_APPROVAL)
            child.interrupt = Interrupt(
                kind="tool_approval",
                reason=f"approve {task.description}",
                invocation_id=f"invocation-{task.id}",
                tool_name="write_data",
            )
            await store.save(child)
            children.append(child)

        server = RuntimeApiServer(
            cwd=str(tmp_path),
            config=_config(tmp_path),
            api_key="test-key",
            workers=0,
            data_dir=tmp_path / "api-runtime",
            checkpoint_store=store,
            observability_store=MemoryObservabilityStore(),
        )
        view = await server._run_view(parent)
        listed = await server._children(parent)
        metadata = server._assignment_metadata(parent)
        child_views = [child_view(child, assignment=metadata[child.run_id]) for child in listed]

        assert view["children_count"] == 2
        assert view["waiting_children_count"] == 2
        assert {item["run_id"] for item in view["pending_interrupts"]} == {
            child.run_id for child in children
        }
        assert {item["assignment_id"] for item in child_views} == {"a", "b"}
        assert all(item["run_kind"] == "plan_task" for item in child_views)

    asyncio.run(scenario())


def test_plan_replan_waits_for_active_wave_and_reuses_completed_child(tmp_path):
    async def scenario():
        client = ParallelPlanClient(
            [_task("A"), _task("B"), _task("C")],
            replan_tasks=[_task("B"), _task("D", ["b"])],
            barrier_size=2,
            fail_tasks={"A"},
            hold_tasks={"B"},
        )
        store = MemoryCheckpointStore()
        running = asyncio.create_task(
            _runtime(client, store, tmp_path).start(
                thread_id="thread-replan", run_id="run-replan", input="complex goal"
            )
        )
        await asyncio.wait_for(client.failed.wait(), timeout=2)
        assert "start:C" not in client.timeline
        assert client.replan_calls == 0

        client.release["B"].set()
        completed = await asyncio.wait_for(running, timeout=3)
        plan = ExecutionPlan.from_dict(completed.strategy_state["plan"])
        history = plan.history[0]
        old_b = next(task for task in history.tasks if task.description == "B")
        new_b = next(task for task in plan.all_tasks() if task.description == "B")

        assert completed.status == RunStatus.COMPLETED
        assert client.replan_calls == 1
        assert client.timeline.index("end:B") < client.timeline.index("start:D")
        assert "start:C" not in client.timeline
        assert new_b.reused_from == "plan_v1:task_2"
        assert new_b.child_run_id == old_b.child_run_id
        assert client.task_calls["B"] == 1

    asyncio.run(scenario())


def test_plan_replan_checkpoint_survives_crash_without_replanning_again(tmp_path):
    async def scenario():
        class CrashOnReplan:
            async def __call__(self, event_type, _payload):
                if event_type == "plan.replanned":
                    raise RuntimeError("crash after plan v2")

        client = ParallelPlanClient([_task("A")], replan_tasks=[_task("D")], fail_tasks={"A"})
        store = MemoryCheckpointStore()
        with pytest.raises(RuntimeError, match="plan v2"):
            await _runtime(client, store, tmp_path, event_sink=CrashOnReplan()).start(
                thread_id="thread-v2", run_id="run-v2", input="complex goal"
            )
        persisted = await store.load("run-v2")
        assert ExecutionPlan.from_dict(persisted.strategy_state["plan"]).version == 2

        completed = await _runtime(client, store, tmp_path).resume("run-v2")
        assert completed.status == RunStatus.COMPLETED
        assert client.replan_calls == 1

    asyncio.run(scenario())


def test_plan_parent_cancel_cascades_children_and_pending_never_starts(tmp_path):
    async def scenario():
        client = ParallelPlanClient(
            [_task("A"), _task("B"), _task("C", ["a"])],
            approval_tasks={"A", "B"},
        )
        store = MemoryCheckpointStore()
        runtime = _runtime(
            client,
            store,
            tmp_path,
            hitl="always",
            tools=_registry(_write_tool([])),
        )
        parent = await runtime.start(
            thread_id="thread-cancel", run_id="run-cancel", input="complex goal"
        )
        cancelled = await runtime.cancel(parent.run_id)
        plan = ExecutionPlan.from_dict(cancelled.strategy_state["plan"])
        children = [
            await store.load(task.child_run_id) for task in plan.all_tasks() if task.child_run_id
        ]

        assert cancelled.status == RunStatus.CANCELLED
        assert all(child.status == RunStatus.CANCELLED for child in children)
        assert plan.get_task("task_3").child_run_id is None
        assert plan.get_task("task_3").status == TaskStatus.CANCELLED
        assert client.task_calls["C"] == 0

    asyncio.run(scenario())


def test_cancelling_one_plan_child_does_not_cancel_sibling(tmp_path):
    async def scenario():
        client = ParallelPlanClient([_task("A"), _task("B")], approval_tasks={"A", "B"})
        store = MemoryCheckpointStore()
        strategy = PlanExecuteStrategy(Planner(client))
        runtime = _runtime(
            client,
            store,
            tmp_path,
            hitl="always",
            tools=_registry(_write_tool([])),
            strategy=strategy,
        )
        parent = await runtime.start(
            thread_id="thread-child-cancel", run_id="run-child-cancel", input="complex goal"
        )
        plan = ExecutionPlan.from_dict(parent.strategy_state["plan"])
        child_a, child_b = [task.child_run_id for task in plan.all_tasks()]
        await strategy._child_runtime(runtime, plan.get_task("task_1")).cancel(child_a)
        parent = await runtime.resume(parent.run_id)

        assert parent.status == RunStatus.WAITING_CHILD
        assert (await store.load(child_a)).status == RunStatus.CANCELLED
        assert (await store.load(child_b)).status == RunStatus.WAITING_APPROVAL

    asyncio.run(scenario())


def test_plan_mixed_state_restart_reconciles_without_duplicate_children(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        client = ParallelPlanClient(
            [_task("A"), _task("B"), _task("C"), _task("D"), _task("E", ["c"])]
        )
        parent = Checkpoint.create(
            thread_id="thread-mixed",
            turn_id="turn-mixed",
            run_id="run-mixed",
            input="goal",
            execution_strategy="plan_execute",
        )
        parent.status = RunStatus.WAITING_CHILD
        plan = ExecutionPlan("plan-mixed", "goal", status=PlanStatus.RUNNING)
        for index, (name, status) in enumerate(
            (("A", TaskStatus.RUNNING), ("B", TaskStatus.RUNNING), ("C", TaskStatus.RUNNING)),
            start=1,
        ):
            task = Task(f"task_{index}", name, TaskType.ANALYSIS, status=status)
            task.attempt = 1
            task.child_run_id = plan_task_child_run_id(parent.run_id, 1, task.id, 1)
            task.execution_state = "ASSIGNED"
            plan.add_task(task)
        plan.add_task(Task("task_4", "D", TaskType.ANALYSIS))
        plan.add_task(Task("task_5", "E", TaskType.ANALYSIS, ["task_3"]))
        parent.strategy_state["plan"] = plan.to_dict()
        await store.save(parent)

        child_a = _child_checkpoint(parent, plan.get_task("task_1"), RunStatus.RUNNING)
        child_b = _child_checkpoint(parent, plan.get_task("task_2"), RunStatus.WAITING_APPROVAL)
        child_b.interrupt = Interrupt(
            kind="tool_approval",
            reason="approval required",
            invocation_id="invocation-b",
            tool_name="write_data",
        )
        child_c = _child_checkpoint(parent, plan.get_task("task_3"), RunStatus.COMPLETED)
        child_c.output_text = "result:C"
        await store.save(child_a)
        await store.save(child_b)
        await store.save(child_c)

        state = await _runtime(client, store, tmp_path).resume(parent.run_id)
        restored = ExecutionPlan.from_dict(state.strategy_state["plan"])
        runs = await store.list(parent.thread_id)

        assert state.status == RunStatus.WAITING_CHILD
        assert restored.get_task("task_1").child_run_id == child_a.run_id
        assert restored.get_task("task_2").child_run_id == child_b.run_id
        assert restored.get_task("task_3").result == "result:C"
        assert restored.get_task("task_4").child_run_id
        assert restored.get_task("task_5").child_run_id
        assert len({run.run_id for run in runs}) == len(runs)
        assert (await store.load(child_b.run_id)).status == RunStatus.WAITING_APPROVAL

    asyncio.run(scenario())


def test_parallel_plan_results_keep_child_histories_isolated(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        client = ParallelPlanClient([_task("A"), _task("B")], barrier_size=2)
        parent = await _runtime(client, store, tmp_path).start(
            thread_id="thread-messages", run_id="run-messages", input="complex goal"
        )
        plan = ExecutionPlan.from_dict(parent.strategy_state["plan"])
        children = [await store.load(task.child_run_id) for task in plan.all_tasks()]

        assert parent.status == RunStatus.COMPLETED
        assert [message.role for message in parent.messages] == ["user", "assistant"]
        assert all(child.messages[0].role == "user" for child in children)
        assert all(len(child.messages) == 2 for child in children)
        assert "Current task [task_1]: A" in str(children[0].messages[0].content)
        assert "Current task [task_2]: B" in str(children[1].messages[0].content)

    asyncio.run(scenario())


def test_plan_parallel_events_and_linked_traces_preserve_lineage(tmp_path):
    async def scenario():
        events: list[tuple[str, dict[str, Any]]] = []

        async def sink(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        observations = MemoryObservabilityStore()
        store = MemoryCheckpointStore()
        client = ParallelPlanClient([_task("A"), _task("B")], barrier_size=2)
        parent = await _runtime(
            client,
            store,
            tmp_path,
            event_sink=sink,
            observations=observations,
        ).start(thread_id="thread-events", run_id="run-events", input="complex goal")
        started = [payload for name, payload in events if name == "plan.step.started"]
        plan = ExecutionPlan.from_dict(parent.strategy_state["plan"])
        parent_trace = await ObservabilityService(observations).trace(parent.run_id)

        assert [item["task_id"] for item in started] == ["task_1", "task_2"]
        assert all(item["plan_version"] == 1 and item["child_run_id"] for item in started)
        assert all(task.child_run_id for task in plan.all_tasks())
        assert len([span for span in parent_trace.spans if span.name == "plan.step"]) == 2
        for task in plan.all_tasks():
            child = await store.load(task.child_run_id)
            assert child.parent_run_id == parent.run_id
            assert child.parent_step_id

    asyncio.run(scenario())


def test_plan_evaluation_aggregates_parallel_child_metrics(tmp_path):
    async def scenario():
        client = ParallelPlanClient([_task("A"), _task("B")], barrier_size=2)
        observations = MemoryObservabilityStore()

        def engine_factory(_case):
            return QueryEngine(
                llm_client=client,
                tool_registry=ToolRegistry(),
                config=_config(tmp_path),
                cwd=str(tmp_path),
            )

        executor = DurableEvaluationExecutor(
            engine_factory=engine_factory,
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=observations,
            execution_strategy="plan_execute",
        )
        result = await executor.execute(EvaluationCase(id="plan", prompt="complex goal"))
        parent_metrics = await ObservabilityService(observations).metrics(result.run_id)

        assert result.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 2
        assert result.total_tokens > parent_metrics.total_tokens
        assert result.step_count >= parent_metrics.step_count + 2

    asyncio.run(scenario())


def test_plan_v1_schema_migrates_completed_and_restarts_inflight_task():
    payload = {
        "schema_version": 1,
        "id": "plan-v1",
        "goal": "goal",
        "status": "RUNNING",
        "tasks": [
            {
                "id": "done",
                "description": "done",
                "status": "COMPLETED",
                "result": "saved",
                "attempt": 1,
            },
            {
                "id": "active",
                "description": "active",
                "status": "RUNNING",
                "attempt": 1,
            },
        ],
    }

    restored = ExecutionPlan.from_dict(payload)

    assert restored.schema_version == 2
    assert restored.get_task("done").result == "saved"
    assert restored.get_task("active").status == TaskStatus.PENDING
    assert restored.get_task("active").attempt == 0


def _current_task(messages) -> str:
    for message in reversed(messages):
        match = re.search(r"Current task \[[^]]+\]: ([^\n]+)", str(message.content))
        if match:
            return match.group(1).strip()
    return "unknown"


def _child_checkpoint(parent: Checkpoint, task: Task, status: RunStatus) -> Checkpoint:
    child = Checkpoint.create(
        thread_id=parent.thread_id,
        turn_id=parent.turn_id,
        run_id=task.child_run_id,
        input=f"Goal: goal\nPlan version: 1\nCurrent task [{task.id}]: {task.description}",
        execution_strategy="react",
        parent_run_id=parent.run_id,
        parent_step_id=f"span-{task.id}",
        run_kind="plan_task",
    )
    child.status = status
    return child
