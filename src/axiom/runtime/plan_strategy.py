from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from axiom.plan import ExecutionPlan, Planner, PlannerResult, PlanStatus, Task, TaskStatus
from axiom.runtime.budget import BudgetExceededError
from axiom.runtime.checkpoints import CheckpointConflictError
from axiom.runtime.models import Checkpoint, RunStatus
from axiom.runtime.observability import SpanStatus, SpanType
from axiom.runtime.observability_store import RunTracer
from axiom.runtime.progress import (
    ProgressDecisionType,
    ProgressObservation,
    ProgressState,
    recovery_message,
    stable_fingerprint,
    state_fingerprint,
)
from axiom.types import Message

if TYPE_CHECKING:
    from axiom.llm.base import LlmClient
    from axiom.runtime.durable import DurableAgentRuntime

_PLAN_KEY = "plan"
_LEGACY_TASK_KEYS = (
    "current_task_id",
    "current_task_output",
    "current_task_error",
    "current_task_complete",
    "current_task_turn_start",
    "current_task_message_start",
)


@dataclass(frozen=True, slots=True)
class PlanSchedulerSnapshot:
    ready_task_ids: tuple[str, ...]
    active_task_ids: tuple[str, ...]
    waiting_task_ids: tuple[str, ...]
    terminal_task_ids: tuple[str, ...]


@dataclass(slots=True)
class LocalPlanTaskScheduler:
    max_parallel_tasks: int

    def snapshot(
        self,
        plan: ExecutionPlan,
        child_states: dict[str, Checkpoint | None],
    ) -> PlanSchedulerSnapshot:
        statuses = {task.id: task.status for task in plan.all_tasks()}
        ready = tuple(
            task.id
            for task in _tasks_in_plan_order(plan)
            if task.status == TaskStatus.PENDING
            and all(statuses.get(dep) == TaskStatus.COMPLETED for dep in task.dependencies)
        )
        active: list[str] = []
        waiting: list[str] = []
        terminal: list[str] = []
        for task in _tasks_in_plan_order(plan):
            child = child_states.get(task.id)
            if task.status in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.SKIPPED,
                TaskStatus.CANCELLED,
            }:
                terminal.append(task.id)
            elif child is not None and child.status in {
                RunStatus.WAITING_APPROVAL,
                RunStatus.INTERRUPTED,
            }:
                waiting.append(task.id)
            elif task.status == TaskStatus.RUNNING:
                active.append(task.id)
        return PlanSchedulerSnapshot(tuple(ready), tuple(active), tuple(waiting), tuple(terminal))

    async def execute(
        self,
        tasks: list[Task],
        runner: Callable[[Task], Awaitable[Checkpoint]],
        observer: Callable[[Task, Checkpoint], Awaitable[bool]] | None = None,
    ) -> dict[str, Checkpoint]:
        queue = list(tasks)
        running: dict[asyncio.Task[Checkpoint], Task] = {}
        results: dict[str, Checkpoint] = {}
        limit = max(1, int(self.max_parallel_tasks))
        stop_launching = False
        try:
            while running or (queue and not stop_launching):
                while queue and not stop_launching and len(running) < limit:
                    task = queue.pop(0)
                    execution = asyncio.create_task(
                        runner(task),
                        name=f"axiom-plan-task-{task.id}",
                    )
                    running[execution] = task
                if not running:
                    break
                done, _pending = await asyncio.wait(
                    running,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                completed: list[tuple[Task, Checkpoint]] = []
                for execution in done:
                    task = running.pop(execution)
                    child = execution.result()
                    results[task.id] = child
                    completed.append((task, child))
                    if child.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
                        stop_launching = True
                if observer is not None:
                    decisions = await asyncio.gather(
                        *(observer(task, child) for task, child in completed)
                    )
                    if not all(decisions):
                        stop_launching = True
        except BaseException:
            for execution in running:
                execution.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            raise
        return results


@dataclass(slots=True)
class PlanExecuteStrategy:
    planner: Planner
    max_task_turns: int = 8
    max_replans: int = 1
    max_parallel_tasks: int | None = None
    checkpoint_retry_attempts: int = 8
    name: str = "plan_execute"

    @classmethod
    def for_llm(cls, llm_client: LlmClient) -> PlanExecuteStrategy:
        return cls(planner=Planner(llm_client))

    async def advance(self, runtime: DurableAgentRuntime, state: Checkpoint) -> Checkpoint:
        while state.status == RunStatus.RUNNING:
            state = await runtime._refresh(state)
            if state.status != RunStatus.RUNNING:
                return state

            raw_plan = state.strategy_state.get(_PLAN_KEY)
            plan = self._load_plan(state)
            if plan is None:
                state = await self._create_plan(runtime, state)
                if state.status != RunStatus.RUNNING:
                    return state
                continue

            if _legacy_plan_state(state, raw_plan):
                self._clear_legacy_parent_task_state(state)
                self._store_plan(state, plan)
                await runtime._save_checkpoint(state, operation="plan.schema.migrated")
                continue

            state = await self._reconcile_children(runtime, state)
            plan = self._load_plan(state)
            if plan is None:
                raise RuntimeError("plan state disappeared during child reconciliation")
            if plan.is_all_completed():
                return await self._complete_plan(runtime, state, plan)

            child_states = await self._child_states(runtime, plan)
            scheduler = self._scheduler(runtime)
            if plan.has_failed():
                state = await self._release_unstarted_tasks(runtime, state, plan, child_states)
                plan = self._load_plan(state)
                if plan is None:
                    raise RuntimeError("plan state disappeared during replan barrier")
                state = await self._skip_failed_dependents(runtime, state, plan)
                plan = self._load_plan(state)
                if plan is None:
                    raise RuntimeError("plan state disappeared while skipping dependents")
                child_states = await self._child_states(runtime, plan)
                snapshot = scheduler.snapshot(plan, child_states)
                if snapshot.active_task_ids or snapshot.waiting_task_ids:
                    return await self._wait_for_children(runtime, state, plan, snapshot)
                if plan.replan_count < self.max_replans:
                    state = await self._replan(runtime, state, plan)
                    continue
                return await self._fail_plan(
                    runtime,
                    state,
                    plan,
                    "plan failed after exhausting replan attempts",
                )

            snapshot = scheduler.snapshot(plan, child_states)
            if snapshot.ready_task_ids:
                state = await self._persist_ready_tasks(
                    runtime,
                    state,
                    plan,
                    snapshot.ready_task_ids,
                )
                plan = self._load_plan(state)
                if plan is None:
                    raise RuntimeError("plan state disappeared after task scheduling")
                child_states = await self._child_states(runtime, plan)

            launchable = [
                task
                for task in _tasks_in_plan_order(plan)
                if task.status == TaskStatus.RUNNING
                and task.child_run_id
                and (
                    child_states.get(task.id) is None
                    or child_states[task.id].status == RunStatus.RUNNING
                )
            ]
            if launchable:
                try:
                    await runtime.budget_manager.preflight_child(state)
                except BudgetExceededError as exc:
                    return await runtime._fail(state, exc, step="child")
                await scheduler.execute(
                    launchable,
                    partial(self._start_child, runtime, state, plan),
                    partial(self._observe_scheduled_child, runtime, state.run_id),
                )
                state = await runtime._require(state.run_id)
                if state.status != RunStatus.RUNNING:
                    return state
                state = await self._reconcile_children(runtime, state)
                continue

            child_states = await self._child_states(runtime, plan)
            snapshot = scheduler.snapshot(plan, child_states)
            if snapshot.active_task_ids or snapshot.waiting_task_ids:
                return await self._wait_for_children(runtime, state, plan, snapshot)
            if plan.is_all_completed():
                return await self._complete_plan(runtime, state, plan)
            if plan.has_failed():
                continue
            return await self._fail_plan(
                runtime,
                state,
                plan,
                "plan stalled because dependencies were not satisfied",
            )
        return state

    async def on_cancel(self, runtime: DurableAgentRuntime, state: Checkpoint) -> None:
        plan = self._load_plan(state)
        if plan is None:
            return
        for task in plan.all_tasks():
            if task.status not in {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.SKIPPED,
                TaskStatus.CANCELLED,
            }:
                task.mark_cancelled()
                task.execution_state = RunStatus.CANCELLED.value
        plan.status = PlanStatus.CANCELLED
        plan.end_time = time.time()
        self._store_plan(state, plan)

    async def after_cancel(self, runtime: DurableAgentRuntime, state: Checkpoint) -> None:
        plan = self._load_plan(state)
        if plan is None:
            return
        for task in plan.all_tasks():
            if task.child_run_id:
                child = await runtime.store.load(task.child_run_id)
                if child is not None and not child.finished:
                    await self._child_runtime(runtime, task).cancel(child.run_id)
            if task.status == TaskStatus.CANCELLED and task.child_run_id:
                span = await self._plan_step_span(runtime, state, plan, task, reopen=True)
                await runtime._finish_span(span, SpanStatus.CANCELLED)
        await runtime._emit("plan.cancelled", self._plan_event(state, plan))

    async def resume_waiting_child(
        self,
        runtime: DurableAgentRuntime,
        parent: Checkpoint,
        *,
        decision: str,
        child_run_id: str | None = None,
    ) -> Checkpoint:
        plan = self._load_plan(parent)
        if plan is None:
            raise ValueError("parent has no active plan")
        waiting: list[Task] = []
        for task in plan.all_tasks():
            if not task.child_run_id or (child_run_id and task.child_run_id != child_run_id):
                continue
            child = await runtime.store.load(task.child_run_id)
            if child is not None and child.status == RunStatus.WAITING_APPROVAL:
                waiting.append(task)
        if len(waiting) != 1:
            raise ValueError("approval must identify exactly one waiting plan child run")
        task = waiting[0]
        return await self._child_runtime(runtime, task).resume(
            task.child_run_id or "",
            decision=decision,
        )

    async def _create_plan(self, runtime: DurableAgentRuntime, state: Checkpoint) -> Checkpoint:
        result, planning_span = await self._call_planner(
            runtime,
            state,
            name="plan.create",
            goal=state.input,
        )
        if result is None:
            return state
        plan = result.plan
        self._store_plan(state, plan)
        await runtime._save_checkpoint(
            state,
            operation="plan.created",
            parent_span_id=_span_id(planning_span),
        )
        await runtime._finish_span(
            planning_span,
            SpanStatus.SUCCEEDED,
            attributes={"plan_id": plan.id, "plan_version": plan.version},
        )
        await runtime._emit(
            "plan.created",
            {**self._plan_event(state, plan), "steps": len(plan.tasks)},
        )
        return state

    async def _replan(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        failed_plan: ExecutionPlan,
    ) -> Checkpoint:
        failed = next(
            (
                task
                for task in failed_plan.all_tasks()
                if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}
            ),
            None,
        )
        reason = failed.error if failed and failed.error else "plan step failed"
        progress_state = ProgressState.from_dict(state.progress_state)
        if progress_state.recovery_signal_pending:
            reason = f"{reason}\n\n{recovery_message(progress_state)}"
        result, replan_span = await self._call_planner(
            runtime,
            state,
            name="plan.replan",
            previous_plan=failed_plan,
            reason=reason,
        )
        if result is None:
            return state
        if progress_state.recovery_signal_pending:
            runtime.progress_detector.mark_recovery_delivered(progress_state)
            state.progress_state = progress_state.to_dict()
        replacement = result.plan
        replacement.version = failed_plan.version + 1
        replacement.replan_count = failed_plan.replan_count + 1
        replacement.history = [*failed_plan.history, failed_plan.snapshot(reason)]
        _reuse_completed_tasks(failed_plan, replacement)
        state.error = None
        self._store_plan(state, replacement)
        decision = await runtime.observe_progress(
            state,
            ProgressObservation(
                operation_id=f"plan:replan:{replacement.version}",
                step=state.step_index,
                action_fingerprint=_plan_fingerprint(replacement),
                state_fingerprint=state_fingerprint(
                    {
                        "completed": sorted(
                            task.description
                            for task in replacement.all_tasks()
                            if task.status == TaskStatus.COMPLETED
                        ),
                        "pending": sorted(
                            task.description
                            for task in replacement.all_tasks()
                            if task.status == TaskStatus.PENDING
                        ),
                    }
                ),
            ),
            parent_span_id=_span_id(replan_span),
        )
        if decision.decision == ProgressDecisionType.TERMINATE:
            return state
        await runtime._save_checkpoint(
            state,
            operation="plan.replanned",
            parent_span_id=_span_id(replan_span),
        )
        await runtime._finish_span(
            replan_span,
            SpanStatus.SUCCEEDED,
            attributes={
                "previous_plan_id": failed_plan.id,
                "plan_id": replacement.id,
                "plan_version": replacement.version,
                "reason": reason,
            },
        )
        await runtime._emit(
            "plan.replanned",
            {
                **self._plan_event(state, replacement),
                "previous_plan_id": failed_plan.id,
                "reason": reason,
            },
        )
        return state

    async def _call_planner(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        *,
        name: str,
        goal: str | None = None,
        previous_plan: ExecutionPlan | None = None,
        reason: str = "",
    ) -> tuple[PlannerResult | None, Any]:
        step_operation = (
            f"step:{state.run_id}:plan:{name}:{previous_plan.version if previous_plan else 0}"
        )
        try:
            await runtime.budget_manager.consume_step(state, step_operation)
        except BudgetExceededError as exc:
            await runtime._fail(state, exc, step=name)
            return None, None
        plan_span = await runtime._start_span(
            SpanType.AGENT,
            name,
            attributes={
                "kind": "planning" if previous_plan is None else "replan",
                "previous_plan_version": previous_plan.version if previous_plan else None,
            },
        )
        uses_llm = previous_plan is not None or self.planner.requires_llm(goal or state.input)
        llm_span = (
            await runtime._start_span(
                SpanType.LLM,
                "llm.plan",
                parent_span_id=_span_id(plan_span),
                attributes={
                    "provider": runtime.llm_client.provider_name,
                    "model": runtime.llm_client.model_name,
                    "temperature": runtime.config.llm.temperature,
                    "retry_count": 0,
                },
            )
            if uses_llm
            else None
        )
        started = time.perf_counter()
        model_operation_id: str | None = None
        model_reserved = False
        try:
            if uses_llm:
                model_operation_id = (
                    f"model:{state.run_id}:{_span_id(llm_span) or time.perf_counter_ns()}"
                )
                await runtime.budget_manager.reserve_model_call(
                    state,
                    model_operation_id,
                )
                model_reserved = True
            self.planner.context_manager = runtime.context_manager
            result = (
                await self.planner.create_plan_result(goal or state.input)
                if previous_plan is None
                else await self.planner.replan_result(previous_plan, reason)
            )
        except Exception as exc:  # noqa: BLE001 - persisted Run failure boundary
            if model_operation_id is not None and model_reserved:
                with suppress(BudgetExceededError):
                    await runtime.budget_manager.complete_model_call(
                        state,
                        model_operation_id,
                        input_tokens=0,
                        output_tokens=0,
                    )
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            await runtime._finish_span(
                llm_span,
                SpanStatus.FAILED,
                attributes={"error": str(exc), "latency_ms": latency_ms},
            )
            await runtime._finish_span(plan_span, SpanStatus.FAILED, attributes={"error": str(exc)})
            await runtime._fail(state, exc, step="planning" if previous_plan is None else "replan")
            return None, plan_span
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if model_operation_id is not None:
            try:
                await runtime.budget_manager.complete_model_call(
                    state,
                    model_operation_id,
                    input_tokens=result.prompt_tokens,
                    output_tokens=result.completion_tokens,
                )
            except BudgetExceededError as exc:
                await runtime._finish_span(
                    llm_span,
                    SpanStatus.FAILED,
                    attributes={"error": str(exc), "latency_ms": latency_ms},
                )
                await runtime._finish_span(
                    plan_span, SpanStatus.FAILED, attributes={"error": str(exc)}
                )
                await runtime._fail(state, exc, step=name)
                return None, plan_span
        state.total_tokens += result.prompt_tokens + result.completion_tokens
        await runtime._finish_span(
            llm_span,
            SpanStatus.SUCCEEDED,
            attributes={
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
                "total_tokens": result.prompt_tokens + result.completion_tokens,
                "ttft_ms": result.ttft_ms,
                "latency_ms": latency_ms,
                "finish_reason": result.finish_reason,
                "planner_shortcut": not result.used_llm,
                **result.context_attributes,
            },
        )
        return result, plan_span

    def _scheduler(self, runtime: DurableAgentRuntime) -> LocalPlanTaskScheduler:
        configured = self.max_parallel_tasks
        if configured is None:
            configured = runtime.config.plan.max_parallel_tasks
        return LocalPlanTaskScheduler(max(1, int(configured)))

    async def _child_states(
        self,
        runtime: DurableAgentRuntime,
        plan: ExecutionPlan,
    ) -> dict[str, Checkpoint | None]:
        tasks = plan.all_tasks()
        states = await asyncio.gather(
            *(
                runtime.store.load(task.child_run_id) if task.child_run_id else _none_checkpoint()
                for task in tasks
            )
        )
        return {task.id: child for task, child in zip(tasks, states, strict=True)}

    async def _persist_ready_tasks(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        ready_task_ids: tuple[str, ...],
    ) -> Checkpoint:
        scheduled: list[Task] = []
        if plan.status == PlanStatus.CREATED:
            plan.mark_started()
        for task_id in ready_task_ids:
            task = plan.get_task(task_id)
            if task is None or task.status != TaskStatus.PENDING:
                continue
            if task.child_run_id is None:
                task.mark_started()
                task.child_run_id = plan_task_child_run_id(
                    state.run_id,
                    plan.version,
                    task.id,
                    task.attempt,
                )
            else:
                task.status = TaskStatus.RUNNING
                if not task.start_time:
                    task.start_time = time.time()
            task.execution_state = "ASSIGNED"
            scheduled.append(task)
        if not scheduled:
            return state
        self._store_plan(state, plan)
        await runtime._save_checkpoint(state, operation="plan.step.batch.scheduled")
        for task in scheduled:
            await self._plan_step_span(runtime, state, plan, task, reopen=True)
            await runtime._emit("plan.step.scheduled", self._task_event(state, plan, task))
        return state

    async def _start_child(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        task: Task,
    ) -> Checkpoint:
        if task.child_run_id is None:
            raise RuntimeError("scheduled plan task is missing child_run_id")
        existing = await runtime.store.load(task.child_run_id)
        child_runtime = self._child_runtime(runtime, task)
        if existing is not None and existing.status == RunStatus.RUNNING:
            return await child_runtime.resume(existing.run_id)
        if existing is not None:
            return existing
        await runtime._emit("plan.step.started", self._task_event(state, plan, task))
        try:
            return await child_runtime.start(
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                run_id=task.child_run_id,
                input=_task_context(plan, task),
                parent_run_id=state.run_id,
                parent_step_id=plan_task_span_id(state.run_id, plan.version, task.id, task.attempt),
                run_kind="plan_task",
                budget_owner_run_id=state.budget_owner_run_id or state.run_id,
            )
        except ValueError as exc:
            existing = await runtime.store.load(task.child_run_id)
            if existing is None or "run already exists" not in str(exc):
                raise
            return existing

    async def _reconcile_children(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> Checkpoint:
        plan = self._load_plan(state)
        if plan is None:
            return state
        observable: list[str] = []
        for task in plan.all_tasks():
            if task.status != TaskStatus.RUNNING or not task.child_run_id:
                continue
            child = await runtime.store.load(task.child_run_id)
            if child is not None and child.status != RunStatus.RUNNING:
                observable.append(task.id)
        if observable:
            await asyncio.gather(
                *(self._observe_child(runtime, state.run_id, task_id) for task_id in observable)
            )
            return await runtime._require(state.run_id)
        return state

    async def _observe_scheduled_child(
        self,
        runtime: DurableAgentRuntime,
        parent_run_id: str,
        task: Task,
        _child: Checkpoint,
    ) -> bool:
        await self._observe_child(runtime, parent_run_id, task.id)
        parent = await runtime._require(parent_run_id)
        if parent.status != RunStatus.RUNNING:
            return False
        plan = self._load_plan(parent)
        return plan is not None and not plan.has_failed()

    async def _observe_child(
        self,
        runtime: DurableAgentRuntime,
        parent_run_id: str,
        task_id: str,
    ) -> None:
        for _attempt in range(max(1, self.checkpoint_retry_attempts)):
            parent = await runtime._require(parent_run_id)
            if parent.finished:
                return
            plan = self._load_plan(parent)
            task = plan.get_task(task_id) if plan else None
            if plan is None or task is None or not task.child_run_id:
                return
            if task.status != TaskStatus.RUNNING:
                return
            child = await runtime.store.load(task.child_run_id)
            if child is None or child.status == RunStatus.RUNNING:
                return

            event_type = "plan.step.waiting"
            span_status = SpanStatus.INTERRUPTED
            operation = "plan.step.waiting"
            if child.status in {RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED}:
                if task.execution_state == child.status.value:
                    return
                task.execution_state = child.status.value
            elif child.status == RunStatus.COMPLETED:
                task.mark_completed(child.output_text)
                task.error = ""
                task.execution_state = child.status.value
                parent.total_tokens += child.total_tokens
                operation = "plan.step.completed"
                event_type = "plan.step.completed"
                span_status = SpanStatus.SUCCEEDED
            else:
                error = child.error.message if child.error else child.status.value.lower()
                if child.status == RunStatus.CANCELLED:
                    task.mark_cancelled(error)
                    event_type = "plan.step.cancelled"
                    span_status = SpanStatus.CANCELLED
                else:
                    task.mark_failed(error)
                    event_type = "plan.step.failed"
                    span_status = SpanStatus.FAILED
                task.execution_state = child.status.value
                operation = event_type

            self._store_plan(parent, plan)
            span = await self._plan_step_span(runtime, parent, plan, task, reopen=True)
            if child.status == RunStatus.COMPLETED:
                decision = await runtime.observe_progress(
                    parent,
                    ProgressObservation(
                        operation_id=f"plan:task:{task.id}:completed",
                        step=parent.step_index,
                        action_fingerprint=stable_fingerprint(
                            {"kind": "plan_task_completed", "task": task.id}
                        ),
                        state_fingerprint=state_fingerprint(
                            {
                                "completed": sorted(
                                    item.id
                                    for item in plan.all_tasks()
                                    if item.status == TaskStatus.COMPLETED
                                )
                            }
                        ),
                    ),
                    parent_span_id=_span_id(span),
                )
                if decision.decision == ProgressDecisionType.TERMINATE:
                    return
            try:
                await runtime._save_checkpoint(
                    parent,
                    operation=operation,
                    parent_span_id=_span_id(span),
                )
            except CheckpointConflictError:
                continue
            attributes: dict[str, object] = {"child_run_id": child.run_id}
            if child.status == RunStatus.COMPLETED:
                attributes["output_chars"] = len(child.output_text)
            elif child.error:
                attributes["error"] = child.error.message
            await runtime._finish_span(span, span_status, attributes=attributes)
            await runtime._emit(event_type, self._task_event(parent, plan, task))
            return
        raise CheckpointConflictError(
            f"could not reconcile plan task {task_id} after concurrent parent updates"
        )

    async def _release_unstarted_tasks(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        child_states: dict[str, Checkpoint | None],
    ) -> Checkpoint:
        changed = False
        for task in plan.all_tasks():
            if (
                task.status == TaskStatus.RUNNING
                and task.child_run_id
                and child_states.get(task.id) is None
            ):
                task.status = TaskStatus.PENDING
                task.execution_state = ""
                changed = True
        if changed:
            self._store_plan(state, plan)
            await runtime._save_checkpoint(state, operation="plan.replan.barrier")
        return state

    async def _skip_failed_dependents(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
    ) -> Checkpoint:
        changed = False
        failed_states = {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}
        while True:
            round_changed = False
            statuses = {task.id: task.status for task in plan.all_tasks()}
            for task in plan.all_tasks():
                if task.status != TaskStatus.PENDING:
                    continue
                failed = [dep for dep in task.dependencies if statuses.get(dep) in failed_states]
                if not failed:
                    continue
                task.mark_skipped()
                task.error = f"dependency did not complete successfully: {', '.join(failed)}"
                task.execution_state = TaskStatus.SKIPPED.value
                round_changed = True
                changed = True
            if not round_changed:
                break
        if changed:
            self._store_plan(state, plan)
            await runtime._save_checkpoint(state, operation="plan.dependencies.skipped")
        return state

    async def _wait_for_children(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        snapshot: PlanSchedulerSnapshot,
    ) -> Checkpoint:
        state.status = RunStatus.WAITING_CHILD
        self._store_plan(state, plan)
        await runtime._save_checkpoint(state, operation="plan.scheduler.waiting")
        await runtime._update_run_trace(state.status)
        await runtime._emit(
            "plan.waiting",
            {
                **self._plan_event(state, plan),
                "active_task_ids": list(snapshot.active_task_ids),
                "waiting_task_ids": list(snapshot.waiting_task_ids),
            },
        )
        return state

    async def _complete_plan(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
    ) -> Checkpoint:
        plan.mark_completed()
        state.status = RunStatus.COMPLETED
        state.output_text = _build_plan_result(plan)
        state.messages.append(Message(role="assistant", content=state.output_text))
        self._store_plan(state, plan)
        await runtime._apply_completion_verification(state, allow_correction=False)
        await runtime._save_checkpoint(state, operation="plan.completed")
        await runtime._emit(
            "plan.completed",
            {
                **self._plan_event(state, plan),
                "steps": len(plan.tasks),
                "replan_count": plan.replan_count,
            },
        )
        await runtime._finish_run_trace(state.status)
        await runtime._emit(
            "run.completed" if state.status == RunStatus.COMPLETED else "run.failed",
            {
                "run_id": state.run_id,
                "total_tokens": state.total_tokens,
                **(
                    {"error": state.error.to_dict() if state.error else None}
                    if state.status == RunStatus.FAILED
                    else {}
                ),
            },
        )
        return state

    async def _fail_plan(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        reason: str,
    ) -> Checkpoint:
        plan.mark_failed()
        self._store_plan(state, plan)
        await runtime._save_checkpoint(state, operation="plan.failed")
        await runtime._emit(
            "plan.failed",
            {**self._plan_event(state, plan), "reason": reason},
        )
        return await runtime._fail(state, RuntimeError(reason), step="plan")

    def _child_runtime(self, runtime: DurableAgentRuntime, task: Task) -> DurableAgentRuntime:
        from axiom.runtime.durable import DurableAgentRuntime

        return DurableAgentRuntime(
            llm_client=runtime.llm_client,
            tool_registry=runtime.tool_registry,
            system_prompt=_task_system_prompt(runtime, task),
            cwd=runtime.cwd,
            config=runtime.config,
            store=runtime.store,
            retry_policy=runtime.retry_policy,
            event_sink=runtime.event_sink,
            tracer=RunTracer(runtime.tracer.store) if runtime.tracer is not None else None,
            permission_policy=runtime.permission_policy,
            execution_backend=runtime.execution_backend,
            execution_strategy="react",
            active_run_supervisor=runtime.active_run_supervisor,
            context_manager=runtime.context_manager,
            budget_manager=runtime.budget_manager,
            ownership=runtime.ownership,
            max_turns=self.max_task_turns,
        )

    async def _plan_step_span(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        task: Task,
        *,
        reopen: bool,
    ) -> Any:
        return await runtime._start_span(
            SpanType.AGENT,
            "plan.step",
            span_id=plan_task_span_id(state.run_id, plan.version, task.id, task.attempt),
            reopen=reopen,
            attributes={
                "plan_id": plan.id,
                "plan_version": plan.version,
                "step_id": task.id,
                "task_id": task.id,
                "description": task.description,
                "attempt": task.attempt,
                "child_run_id": task.child_run_id,
            },
        )

    def _load_plan(self, state: Checkpoint) -> ExecutionPlan | None:
        raw = state.strategy_state.get(_PLAN_KEY)
        return ExecutionPlan.from_dict(raw) if isinstance(raw, dict) else None

    def _store_plan(self, state: Checkpoint, plan: ExecutionPlan) -> None:
        state.strategy_state[_PLAN_KEY] = plan.to_dict()

    def _clear_legacy_parent_task_state(self, state: Checkpoint) -> None:
        for key in _LEGACY_TASK_KEYS:
            state.strategy_state.pop(key, None)
        state.pending_tool_calls = []
        state.next_tool_index = 0
        state.error = None

    def _plan_event(self, state: Checkpoint, plan: ExecutionPlan) -> dict[str, Any]:
        return {
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "turn_id": state.turn_id,
            "plan_id": plan.id,
            "plan_version": plan.version,
            "status": plan.status.value,
        }

    def _task_event(
        self,
        state: Checkpoint,
        plan: ExecutionPlan,
        task: Task,
    ) -> dict[str, Any]:
        return {
            **self._plan_event(state, plan),
            "step_id": task.id,
            "task_id": task.id,
            "attempt": task.attempt,
            "child_run_id": task.child_run_id,
            "task_status": task.status.value,
            "child_status": task.execution_state or None,
            "error": task.error or None,
        }


def plan_task_child_run_id(
    parent_run_id: str,
    plan_version: int,
    task_id: str,
    attempt: int,
) -> str:
    raw = f"{parent_run_id}:plan_v{plan_version}:{task_id}:{attempt}".encode()
    return f"run_plan_{hashlib.sha256(raw).hexdigest()[:32]}"


def plan_task_span_id(
    parent_run_id: str,
    plan_version: int,
    task_id: str,
    attempt: int,
) -> str:
    raw = f"{parent_run_id}:plan_v{plan_version}:{task_id}:{attempt}".encode()
    return f"span_plan_{hashlib.sha256(raw).hexdigest()[:32]}"


def _tasks_in_plan_order(plan: ExecutionPlan) -> list[Task]:
    return [plan.tasks[task_id] for task_id in plan.execution_order()]


def _task_context(plan: ExecutionPlan, task: Task) -> str:
    lines = [
        f"Goal: {plan.goal}",
        f"Plan version: {plan.version}",
        f"Current task [{task.id}]: {task.description}",
        "",
        "Completed dependency results:",
    ]
    for dep_id in task.dependencies:
        dep = plan.get_task(dep_id)
        if dep and dep.status == TaskStatus.COMPLETED:
            lines.append(f"- [{dep.id}] {dep.description}: {_preview(dep.result, 800)}")
    carried = _completed_history(plan)
    if carried:
        lines.extend(["", "Completed work from earlier plan versions:"])
        lines.extend(f"- {description}: {_preview(result, 800)}" for description, result in carried)
    return "\n".join(lines)


def _task_system_prompt(runtime: DurableAgentRuntime, task: Task) -> str:
    return (
        runtime.system_prompt
        + "\n\nYou are executing one durable task inside a Plan-and-Execute strategy.\n"
        + f"Task id: {task.id}\nTask type: {task.type.value}\n"
        + "Complete only this task. Use tools when needed and return the concrete result."
    )


def _build_plan_result(plan: ExecutionPlan) -> str:
    lines = ["Plan execution completed.", "", "Task summary:"]
    for task in plan.all_tasks():
        lines.append(f"- [{task.id}] {task.status.value}: {task.description}")
        if task.result:
            lines.append(f"  Result: {_preview(task.result)}")
    return "\n".join(lines) + "\n"


def _reuse_completed_tasks(previous: ExecutionPlan, replacement: ExecutionPlan) -> None:
    completed = {
        _normalized_description(task.description): task
        for task in previous.all_tasks()
        if task.status == TaskStatus.COMPLETED
    }
    for task in replacement.all_tasks():
        prior = completed.get(_normalized_description(task.description))
        if prior is None:
            continue
        task.status = TaskStatus.COMPLETED
        task.result = prior.result
        task.error = ""
        task.attempt = prior.attempt
        task.child_run_id = prior.child_run_id
        task.reused_from = f"plan_v{previous.version}:{prior.id}"
        task.execution_state = prior.execution_state
        task.start_time = prior.start_time
        task.end_time = prior.end_time


def _completed_history(plan: ExecutionPlan) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for revision in plan.history:
        for task in revision.tasks:
            key = _normalized_description(task.description)
            if task.status != TaskStatus.COMPLETED or not task.result or key in seen:
                continue
            seen.add(key)
            result.append((task.description, task.result))
    return result


def _legacy_plan_state(state: Checkpoint, raw_plan: object) -> bool:
    return (isinstance(raw_plan, dict) and int(raw_plan.get("schema_version") or 0) == 1) or any(
        key in state.strategy_state for key in _LEGACY_TASK_KEYS
    )


def _normalized_description(value: str) -> str:
    return " ".join(value.casefold().split())


def _plan_fingerprint(plan: ExecutionPlan) -> str:
    descriptions = {task.id: _normalized_description(task.description) for task in plan.all_tasks()}
    return stable_fingerprint(
        {
            "kind": "replan",
            "tasks": [
                {
                    "description": descriptions[task.id],
                    "type": task.type.value,
                    "dependencies": sorted(
                        descriptions.get(dependency, dependency) for dependency in task.dependencies
                    ),
                }
                for task in _tasks_in_plan_order(plan)
            ],
        }
    )


async def _none_checkpoint() -> Checkpoint | None:
    return None


def _span_id(span: Any) -> str | None:
    return str(span.span_id) if span is not None else None


def _preview(text: str, max_len: int = 160) -> str:
    value = (text or "").replace("\r\n", "\n").strip()
    return value if len(value) <= max_len else value[: max_len - 3] + "..."
