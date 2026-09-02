from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any

import pytest

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.evaluation import DurableEvaluationExecutor, EvaluationCase
from axiom.runtime import (
    ActiveRunSupervisor,
    AssignmentStatus,
    Checkpoint,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    MultiAgentExecutionStrategy,
    MultiAgentState,
    MultiAgentStatus,
    ObservabilityService,
    RetryPolicy,
    RunStatus,
    WorkerAssignment,
    child_run_id,
)
from axiom.runtime.checkpoints import CheckpointConflictError
from axiom.runtime.control_plane import run_view
from axiom.runtime.models import Interrupt
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema
from axiom.types import Message


def _task(task_id: str, dependencies: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": task_id.lower(),
        "description": task_id,
        "type": "worker",
        "dependencies": dependencies or [],
    }


class ParallelClient:
    provider_name = "parallel-test"
    model_name = "parallel-model"
    max_context_window = 10_000

    def __init__(
        self,
        tasks: list[dict[str, Any]],
        *,
        barrier_size: int = 0,
        approval_tasks: set[str] | None = None,
        fail_tasks: set[str] | None = None,
        b_waits_for_c: bool = False,
        review_decisions: list[bool] | None = None,
        hold_tasks: set[str] | None = None,
    ) -> None:
        self.tasks = tasks
        self.barrier_size = barrier_size
        self.approval_tasks = approval_tasks or set()
        self.fail_tasks = fail_tasks or set()
        self.b_waits_for_c = b_waits_for_c
        self.review_decisions = list(review_decisions or [True])
        self.hold_tasks = hold_tasks or set()
        self.planner_calls = 0
        self.reviewer_calls = 0
        self.worker_calls: Counter[str] = Counter()
        self.active_workers = 0
        self.peak_concurrency = 0
        self.timeline: list[str] = []
        self.started_tasks: set[str] = set()
        self.barrier = asyncio.Event()
        self.c_started = asyncio.Event()
        self.release: dict[str, asyncio.Event] = {task: asyncio.Event() for task in self.hold_tasks}

    async def chat(self, messages, _tools, *, system_prompt):
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
                "text": json.dumps({"approved": approved, "issues": []}),
            }
            yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        task = _current_task(messages)
        has_tool_result = any(message.role == "tool" for message in messages)
        if not has_tool_result:
            self.worker_calls[task] += 1
        if task in self.fail_tasks:
            yield {"type": "error", "error": RuntimeError(f"failed:{task}")}
            return
        if task in self.approval_tasks and not has_tool_result:
            self.timeline.append(f"start:{task}")
            self.timeline.append(f"waiting:{task}")
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": f"call-{task}",
                    "function": {
                        "name": "write_data",
                        "arguments": json.dumps({"path": f"{task}.txt"}),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return

        self.active_workers += 1
        self.peak_concurrency = max(self.peak_concurrency, self.active_workers)
        self.timeline.append(f"start:{task}")
        self.started_tasks.add(task)
        if task == "C":
            self.c_started.set()
        if self.barrier_size and len(self.started_tasks) >= self.barrier_size:
            self.barrier.set()
        if self.barrier_size and task in {"A", "B"}:
            await self.barrier.wait()
        if self.b_waits_for_c and task == "B":
            await asyncio.wait_for(self.c_started.wait(), timeout=2)
        if task in self.hold_tasks:
            await self.release[task].wait()
        await asyncio.sleep(0.01)
        self.timeline.append(f"end:{task}")
        self.active_workers -= 1
        yield {"type": "text_delta", "text": f"result:{task}"}
        yield {"type": "usage", "usage": {"input_tokens": 3, "output_tokens": 2}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class CrashOnFirstWorkerStart:
    def __init__(self) -> None:
        self.triggered = False

    async def __call__(self, event_type: str, _payload: dict[str, Any]) -> None:
        if event_type == "worker.started" and not self.triggered:
            self.triggered = True
            raise RuntimeError("crash after durable batch assignment")


class ConflictCountingStore(MemoryCheckpointStore):
    def __init__(self, *, inject_once: bool = False) -> None:
        super().__init__()
        self.conflicts = 0
        self.inject_once = inject_once
        self.injected = False

    async def save(self, checkpoint: Checkpoint) -> None:
        orchestration = MultiAgentExecutionStrategy.load_state(checkpoint)
        observing = bool(
            orchestration
            and any(
                assignment.status == AssignmentStatus.REVIEWING
                for assignment in orchestration.assignments
            )
        )
        if self.inject_once and observing and not self.injected:
            self.injected = True
            self.conflicts += 1
            raise CheckpointConflictError("injected parent CAS conflict")
        try:
            await super().save(checkpoint)
        except CheckpointConflictError:
            self.conflicts += 1
            raise


def _config(tmp_path, *, parallelism: int = 2, hitl: str = "never") -> AxiomConfig:
    config = AxiomConfig()
    config.multi_agent.max_parallel_workers = parallelism
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
    client: ParallelClient,
    store: MemoryCheckpointStore,
    tmp_path,
    *,
    parallelism: int = 2,
    hitl: str = "never",
    tools: ToolRegistry | None = None,
    events=None,
    observations=None,
    strategy: MultiAgentExecutionStrategy | None = None,
    supervisor: ActiveRunSupervisor | None = None,
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=client,
        tool_registry=tools or ToolRegistry(),
        system_prompt="parallel system",
        cwd=str(tmp_path),
        config=_config(tmp_path, parallelism=parallelism, hitl=hitl),
        store=store,
        retry_policy=RetryPolicy(max_attempts=1),
        event_sink=events,
        tracer=RunTracer(observations) if observations is not None else None,
        execution_strategy=strategy or MultiAgentExecutionStrategy(),
        active_run_supervisor=supervisor,
    )


def test_bounded_scheduler_runs_two_workers_with_real_overlap(tmp_path):
    async def scenario():
        client = ParallelClient([_task("A"), _task("B"), _task("C")], barrier_size=2)
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path).start(
            thread_id="thread-parallel",
            run_id="run-parallel",
            input="goal",
        )
        orchestration = MultiAgentExecutionStrategy.load_state(state)

        assert state.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 2
        assert max(client.timeline.index("start:A"), client.timeline.index("start:B")) < min(
            client.timeline.index("end:A"), client.timeline.index("end:B")
        )
        assert client.timeline.index("start:C") > min(
            client.timeline.index("end:A"), client.timeline.index("end:B")
        )
        assert orchestration.max_parallelism_observed == 2

    asyncio.run(scenario())


def test_parallelism_one_preserves_sequential_semantics(tmp_path):
    async def scenario():
        client = ParallelClient([_task("A"), _task("B"), _task("C")])
        state = await _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            parallelism=1,
        ).start(thread_id="thread-one", run_id="run-one", input="goal")

        assert state.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 1
        assert [item for item in client.timeline if item.startswith("start:")] == [
            "start:A",
            "start:B",
            "start:C",
        ]

    asyncio.run(scenario())


def test_parent_cancel_signals_active_workers_and_stops_new_assignments(tmp_path):
    async def scenario():
        client = ParallelClient(
            [_task("A"), _task("B"), _task("C")],
            hold_tasks={"A", "B"},
        )
        store = MemoryCheckpointStore()
        supervisor = ActiveRunSupervisor()
        runtime = _runtime(client, store, tmp_path, supervisor=supervisor)
        parent_task = asyncio.create_task(
            runtime.start(
                thread_id="thread-team-supervisor-cancel",
                run_id="run-team-supervisor-cancel",
                input="goal",
            )
        )
        for _attempt in range(200):
            if client.started_tasks == {"A", "B"}:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("worker children did not start")
        assert len(supervisor.list_active()) == 3

        cancelled = await _runtime(
            client,
            store,
            tmp_path,
            supervisor=supervisor,
        ).cancel("run-team-supervisor-cancel")
        observed = await asyncio.wait_for(parent_task, timeout=3)
        children = [
            state
            for state in await store.list("thread-team-supervisor-cancel")
            if state.parent_run_id == cancelled.run_id
        ]

        assert cancelled.status == RunStatus.CANCELLED
        assert observed.status == RunStatus.CANCELLED
        assert len(children) == 2
        assert {child.status for child in children} == {RunStatus.CANCELLED}
        assert "C" not in client.started_tasks
        assert supervisor.list_active() == ()

    asyncio.run(scenario())


def test_dependency_join_starts_c_only_after_a_and_b_succeed(tmp_path):
    async def scenario():
        client = ParallelClient(
            [_task("A"), _task("B"), _task("C", ["a", "b"])],
            barrier_size=2,
        )
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path).start(
            thread_id="thread-deps",
            run_id="run-deps",
            input="goal",
        )

        assert state.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 2
        assert client.timeline.index("start:C") > client.timeline.index("end:A")
        assert client.timeline.index("start:C") > client.timeline.index("end:B")

    asyncio.run(scenario())


def test_ready_child_identities_are_durable_before_parallel_spawn(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        client = ParallelClient([_task("A"), _task("B"), _task("C")])
        crash = CrashOnFirstWorkerStart()
        with pytest.raises(RuntimeError, match="durable batch assignment"):
            await _runtime(client, store, tmp_path, events=crash).start(
                thread_id="thread-batch-crash",
                run_id="run-batch-crash",
                input="goal",
            )
        persisted = await store.load("run-batch-crash")
        before = MultiAgentExecutionStrategy.load_state(persisted)
        ids = [assignment.child_run_id for assignment in before.assignments]

        assert all(ids)
        assert len(set(ids)) == 3
        assert all(
            assignment.status == AssignmentStatus.ASSIGNED for assignment in before.assignments
        )

        completed = await _runtime(client, store, tmp_path).resume("run-batch-crash")
        after = MultiAgentExecutionStrategy.load_state(completed)
        runs = await store.list("thread-batch-crash")

        assert completed.status == RunStatus.COMPLETED
        assert [assignment.child_run_id for assignment in after.assignments] == ids
        assert sum(run.run_kind == "worker" for run in runs) == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("inject_conflict", [False, True])
def test_parallel_terminal_observation_is_idempotent_and_cas_safe(tmp_path, inject_conflict):
    async def scenario():
        store = ConflictCountingStore(inject_once=inject_conflict)
        assignments = [
            WorkerAssignment(
                f"assignment_{index}",
                "worker",
                name,
                status=AssignmentStatus.ASSIGNED,
                child_run_id=f"child-{name}",
            )
            for index, name in enumerate(("A", "B"), start=1)
        ]
        parent = Checkpoint.create(
            thread_id="thread-cas",
            turn_id="turn-cas",
            run_id="run-cas",
            input="goal",
            execution_strategy="multi_agent",
        )
        MultiAgentExecutionStrategy.store_state(
            parent,
            MultiAgentState(
                "goal",
                status=MultiAgentStatus.RUNNING,
                assignments=assignments,
                active_assignment_ids=[item.assignment_id for item in assignments],
            ),
        )
        await store.save(parent)
        for assignment in assignments:
            child = Checkpoint.create(
                thread_id=parent.thread_id,
                turn_id=parent.turn_id,
                run_id=assignment.child_run_id,
                input=assignment.task,
                parent_run_id=parent.run_id,
                run_kind="worker",
            )
            child.status = RunStatus.COMPLETED
            child.output_text = f"result:{assignment.task}"
            child.total_tokens = 5
            await store.save(child)

        strategy = MultiAgentExecutionStrategy()
        runtime = _runtime(ParallelClient([]), store, tmp_path, strategy=strategy)
        await strategy._reconcile_children(runtime, await store.load(parent.run_id))
        await strategy._reconcile_children(runtime, await store.load(parent.run_id))
        restored = await store.load(parent.run_id)
        orchestration = strategy.load_state(restored)

        assert [item.result for item in orchestration.assignments] == ["result:A", "result:B"]
        assert all(item.status == AssignmentStatus.REVIEWING for item in orchestration.assignments)
        assert restored.total_tokens == 10
        if inject_conflict:
            assert store.conflicts >= 1

    asyncio.run(scenario())


def test_waiting_approval_releases_slot_for_independent_worker(tmp_path):
    async def scenario():
        calls: list[str] = []
        client = ParallelClient(
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
        ).start(thread_id="thread-slot", run_id="run-slot", input="goal")
        orchestration = MultiAgentExecutionStrategy.load_state(parent)
        child_a = await store.load(orchestration.assignments[0].child_run_id)

        assert parent.status == RunStatus.WAITING_CHILD
        assert child_a.status == RunStatus.WAITING_APPROVAL
        assert client.c_started.is_set()
        assert client.timeline.index("start:C") < client.timeline.index("end:B")
        assert calls == []

    asyncio.run(scenario())


def test_multiple_approvals_are_targeted_to_one_child(tmp_path):
    async def scenario():
        calls: list[str] = []
        client = ParallelClient([_task("A"), _task("B")], approval_tasks={"A", "B"})
        store = MemoryCheckpointStore()
        strategy = MultiAgentExecutionStrategy(max_parallel_workers=2)
        runtime = _runtime(
            client,
            store,
            tmp_path,
            hitl="always",
            tools=_registry(_write_tool(calls)),
            strategy=strategy,
        )
        parent = await runtime.start(
            thread_id="thread-approvals",
            run_id="run-approvals",
            input="goal",
        )
        orchestration = strategy.load_state(parent)
        children = [await store.load(item.child_run_id) for item in orchestration.assignments]
        view = run_view(parent, children=children)

        assert len(view["pending_interrupts"]) == 2
        child_a_id = orchestration.assignments[0].child_run_id
        child_b_id = orchestration.assignments[1].child_run_id
        await strategy.resume_waiting_child(
            runtime,
            parent,
            child_run_id=child_a_id,
            decision="approve",
        )
        parent = await runtime.resume(parent.run_id)
        child_b = await store.load(child_b_id)

        assert parent.status == RunStatus.WAITING_CHILD
        assert child_b.status == RunStatus.WAITING_APPROVAL
        assert calls == ["A.txt"]

    asyncio.run(scenario())


def test_parent_cancel_cascades_waiters_and_never_starts_dependent_pending(tmp_path):
    async def scenario():
        calls: list[str] = []
        client = ParallelClient(
            [_task("A"), _task("B"), _task("C", ["a"])],
            approval_tasks={"A", "B"},
        )
        store = MemoryCheckpointStore()
        runtime = _runtime(
            client,
            store,
            tmp_path,
            hitl="always",
            tools=_registry(_write_tool(calls)),
        )
        parent = await runtime.start(
            thread_id="thread-cancel-many",
            run_id="run-cancel-many",
            input="goal",
        )
        cancelled = await runtime.cancel(parent.run_id)
        orchestration = MultiAgentExecutionStrategy.load_state(cancelled)
        children = [
            await store.load(item.child_run_id)
            for item in orchestration.assignments
            if item.child_run_id
        ]

        assert cancelled.status == RunStatus.CANCELLED
        assert all(child.status == RunStatus.CANCELLED for child in children)
        assert orchestration.assignments[2].child_run_id is None
        assert orchestration.assignments[2].status == AssignmentStatus.CANCELLED
        assert client.worker_calls["C"] == 0
        assert calls == []

    asyncio.run(scenario())


def test_cancelling_one_child_does_not_stop_other_child(tmp_path):
    async def scenario():
        client = ParallelClient([_task("A"), _task("B")], approval_tasks={"A", "B"})
        store = MemoryCheckpointStore()
        strategy = MultiAgentExecutionStrategy(max_parallel_workers=2)
        runtime = _runtime(
            client,
            store,
            tmp_path,
            hitl="always",
            tools=_registry(_write_tool([])),
            strategy=strategy,
        )
        parent = await runtime.start(
            thread_id="thread-cancel-one",
            run_id="run-cancel-one",
            input="goal",
        )
        orchestration = strategy.load_state(parent)
        child_a, child_b = [item.child_run_id for item in orchestration.assignments]
        await strategy._child_runtime(runtime, orchestration.assignments[0]).cancel(child_a)
        parent = await runtime.resume(parent.run_id)

        assert parent.status == RunStatus.WAITING_CHILD
        assert (await store.load(child_a)).status == RunStatus.CANCELLED
        assert (await store.load(child_b)).status == RunStatus.WAITING_APPROVAL

    asyncio.run(scenario())


def test_independent_worker_completes_when_sibling_fails(tmp_path):
    async def scenario():
        client = ParallelClient(
            [_task("A"), _task("B"), _task("C", ["a"])],
            fail_tasks={"A"},
        )
        state = await _runtime(client, MemoryCheckpointStore(), tmp_path).start(
            thread_id="thread-failure-parallel",
            run_id="run-failure-parallel",
            input="goal",
        )
        orchestration = MultiAgentExecutionStrategy.load_state(state)

        assert state.status == RunStatus.COMPLETED
        assert orchestration.assignments[0].status == AssignmentStatus.FAILED
        assert orchestration.assignments[1].status == AssignmentStatus.COMPLETED
        assert orchestration.assignments[2].status == AssignmentStatus.SKIPPED
        assert "result:B" in state.output_text

    asyncio.run(scenario())


def test_reviewer_retry_uses_new_child_and_respects_parallelism_limit(tmp_path):
    async def scenario():
        client = ParallelClient(
            [_task("A"), _task("B")],
            review_decisions=[False, True, True],
        )
        store = MemoryCheckpointStore()
        state = await _runtime(
            client,
            store,
            tmp_path,
            parallelism=1,
        ).start(thread_id="thread-retry-limit", run_id="run-retry-limit", input="goal")
        orchestration = MultiAgentExecutionStrategy.load_state(state)
        worker_runs = [run for run in await store.list(state.thread_id) if run.run_kind == "worker"]

        assert state.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 1
        assert orchestration.assignments[0].attempt == 2
        assert orchestration.assignments[0].child_run_id == child_run_id(
            state.run_id,
            "assignment_1",
            2,
        )
        assert len(worker_runs) == 3

    asyncio.run(scenario())


def test_mixed_state_restart_reconciles_without_duplicate_children(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        client = ParallelClient([_task("A"), _task("B"), _task("C"), _task("D")])
        parent = Checkpoint.create(
            thread_id="thread-mixed",
            turn_id="turn-mixed",
            run_id="run-mixed",
            input="goal",
            execution_strategy="multi_agent",
            run_kind="orchestrator",
        )
        parent.status = RunStatus.WAITING_CHILD
        assignments = [
            WorkerAssignment(
                f"assignment_{index}",
                "worker",
                name,
                status=status,
                child_run_id=(
                    child_run_id(parent.run_id, f"assignment_{index}", 1) if name != "D" else None
                ),
            )
            for index, (name, status) in enumerate(
                (
                    ("A", AssignmentStatus.WAITING_CHILD),
                    ("B", AssignmentStatus.WAITING_CHILD),
                    ("C", AssignmentStatus.WAITING_CHILD),
                    ("D", AssignmentStatus.PENDING),
                ),
                start=1,
            )
        ]
        MultiAgentExecutionStrategy.store_state(
            parent,
            MultiAgentState(
                "goal",
                status=MultiAgentStatus.WAITING_CHILD,
                assignments=assignments,
                active_assignment_ids=["assignment_1", "assignment_2", "assignment_3"],
            ),
        )
        await store.save(parent)

        child_a = _child_checkpoint(parent, assignments[0], RunStatus.RUNNING)
        child_b = _child_checkpoint(parent, assignments[1], RunStatus.WAITING_APPROVAL)
        child_b.interrupt = Interrupt(
            kind="tool_approval",
            reason="approval required",
            invocation_id="invocation-b",
            tool_name="write_data",
        )
        child_c = _child_checkpoint(parent, assignments[2], RunStatus.COMPLETED)
        child_c.output_text = "result:C"
        await store.save(child_a)
        await store.save(child_b)
        await store.save(child_c)

        state = await _runtime(client, store, tmp_path).resume(parent.run_id)
        orchestration = MultiAgentExecutionStrategy.load_state(state)
        runs = await store.list(parent.thread_id)

        assert state.status == RunStatus.WAITING_CHILD
        assert orchestration.assignments[0].child_run_id == child_a.run_id
        assert orchestration.assignments[1].child_run_id == child_b.run_id
        assert orchestration.assignments[2].result == "result:C"
        assert orchestration.assignments[3].child_run_id
        assert client.worker_calls["A"] == 1
        assert client.worker_calls["D"] == 1
        assert len({run.run_id for run in runs}) == len(runs)
        assert (await store.load(child_b.run_id)).status == RunStatus.WAITING_APPROVAL

    asyncio.run(scenario())


def test_parallel_events_keep_assignment_and_child_lineage(tmp_path):
    async def scenario():
        events: list[tuple[str, dict[str, Any]]] = []

        async def sink(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))

        client = ParallelClient([_task("A"), _task("B")], barrier_size=2)
        state = await _runtime(
            client,
            MemoryCheckpointStore(),
            tmp_path,
            events=sink,
        ).start(thread_id="thread-events", run_id="run-events", input="goal")
        started = [payload for name, payload in events if name == "worker.started"]

        assert state.status == RunStatus.COMPLETED
        assert [item["assignment_id"] for item in started] == ["assignment_1", "assignment_2"]
        assert all(item["child_run_id"].startswith("run_child_") for item in started)
        first_completed = next(
            index for index, item in enumerate(events) if item[0] == "worker.completed"
        )
        started_indexes = [
            index for index, item in enumerate(events) if item[0] == "worker.started"
        ]
        assert max(started_indexes) < first_completed

    asyncio.run(scenario())


def test_evaluation_aggregates_parallel_child_metrics(tmp_path):
    async def scenario():
        client = ParallelClient([_task("A"), _task("B")], barrier_size=2)
        observations = MemoryObservabilityStore()

        def engine_factory(_case):
            return QueryEngine(
                llm_client=client,
                tool_registry=ToolRegistry(),
                config=_config(tmp_path, parallelism=2),
                cwd=str(tmp_path),
            )

        executor = DurableEvaluationExecutor(
            engine_factory=engine_factory,
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=observations,
            execution_strategy="multi_agent",
        )
        result = await executor.execute(
            EvaluationCase(id="parallel", name="parallel", prompt="goal")
        )
        metrics = await ObservabilityService(observations).metrics(result.run_id)

        assert result.status == RunStatus.COMPLETED
        assert client.peak_concurrency == 2
        assert result.total_tokens > metrics.total_tokens
        assert result.step_count >= metrics.step_count + 2

    asyncio.run(scenario())


def _current_task(messages) -> str:
    for message in reversed(messages):
        if message.role != "user":
            continue
        text = str(message.content)
        marker = "Current task:\n"
        if marker in text:
            return text.rsplit(marker, 1)[1].strip()
        return text.strip()
    return "unknown"


def _child_checkpoint(
    parent: Checkpoint,
    assignment: WorkerAssignment,
    status: RunStatus,
) -> Checkpoint:
    child = Checkpoint.create(
        thread_id=parent.thread_id,
        turn_id=parent.turn_id,
        run_id=assignment.child_run_id,
        input=f"Overall goal: goal\n\nCurrent task:\n{assignment.task}",
        history=[Message(role="user", content=assignment.task)],
        execution_strategy="react",
        parent_run_id=parent.run_id,
        parent_step_id=f"span-{assignment.assignment_id}",
        run_kind="worker",
    )
    child.status = status
    return child
