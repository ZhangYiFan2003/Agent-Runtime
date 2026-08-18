from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from axiom.plan import ExecutionPlan, Planner, PlannerResult, PlanStatus, Task, TaskStatus
from axiom.runtime.models import Checkpoint, RunStatus
from axiom.runtime.observability import SpanStatus, SpanType
from axiom.types import Message

if TYPE_CHECKING:
    from axiom.llm.base import LlmClient
    from axiom.runtime.durable import DurableAgentRuntime

_PLAN_KEY = "plan"
_CURRENT_TASK_KEY = "current_task_id"
_TASK_OUTPUT_KEY = "current_task_output"
_TASK_ERROR_KEY = "current_task_error"
_TASK_COMPLETE_KEY = "current_task_complete"
_TASK_TURN_START_KEY = "current_task_turn_start"
_TASK_MESSAGE_START_KEY = "current_task_message_start"


@dataclass(slots=True)
class PlanExecuteStrategy:
    planner: Planner
    max_task_turns: int = 8
    max_replans: int = 1
    name: str = "plan_execute"

    @classmethod
    def for_llm(cls, llm_client: LlmClient) -> PlanExecuteStrategy:
        return cls(planner=Planner(llm_client))

    async def advance(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> Checkpoint:
        while state.status == RunStatus.RUNNING:
            state = await runtime._refresh(state)
            if state.status != RunStatus.RUNNING:
                return state

            plan = self._load_plan(state)
            if plan is None:
                state = await self._create_plan(runtime, state)
                if state.status != RunStatus.RUNNING:
                    return state
                continue

            current_task = self._current_task(state, plan)
            if current_task is not None:
                state = await self._advance_task(runtime, state, plan, current_task)
                continue

            if plan.is_all_completed():
                return await self._complete_plan(runtime, state, plan)

            executable = _executable_tasks_in_order(plan)
            if executable:
                state = await self._start_task(runtime, state, plan, executable[0])
                continue

            if plan.has_failed():
                if plan.replan_count < self.max_replans:
                    state = await self._replan(runtime, state, plan)
                    continue
                return await self._fail_plan(
                    runtime,
                    state,
                    plan,
                    "plan failed after exhausting replan attempts",
                )

            return await self._fail_plan(
                runtime,
                state,
                plan,
                "plan stalled because dependencies were not satisfied",
            )
        return state

    async def on_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None:
        del runtime
        plan = self._load_plan(state)
        if plan is None:
            return
        plan.status = PlanStatus.CANCELLED
        plan.end_time = time.time()
        self._store_plan(state, plan)

    async def after_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None:
        plan = self._load_plan(state)
        if plan is None:
            return
        task = self._current_task(state, plan)
        if task is not None:
            span = await self._plan_step_span(runtime, state, plan, task, reopen=True)
            await runtime._finish_span(span, SpanStatus.CANCELLED)
        await runtime._emit(
            "plan.cancelled",
            {
                "run_id": state.run_id,
                "plan_id": plan.id,
                "plan_version": plan.version,
            },
        )

    async def _create_plan(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> Checkpoint:
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
            {
                "run_id": state.run_id,
                "plan_id": plan.id,
                "plan_version": plan.version,
                "steps": len(plan.tasks),
            },
        )
        return state

    async def _replan(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        failed_plan: ExecutionPlan,
    ) -> Checkpoint:
        failed = next(
            (task for task in failed_plan.all_tasks() if task.status == TaskStatus.FAILED),
            None,
        )
        reason = failed.error if failed and failed.error else "plan step failed"
        result, replan_span = await self._call_planner(
            runtime,
            state,
            name="plan.replan",
            previous_plan=failed_plan,
            reason=reason,
        )
        if result is None:
            return state

        replacement = result.plan
        replacement.version = failed_plan.version + 1
        replacement.replan_count = failed_plan.replan_count + 1
        replacement.history = [*failed_plan.history, failed_plan.snapshot(reason)]
        _reuse_completed_tasks(failed_plan, replacement)
        self._clear_current_task(state)
        state.error = None
        self._store_plan(state, replacement)
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
                "run_id": state.run_id,
                "previous_plan_id": failed_plan.id,
                "plan_id": replacement.id,
                "plan_version": replacement.version,
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
        try:
            result = (
                await self.planner.create_plan_result(goal or state.input)
                if previous_plan is None
                else await self.planner.replan_result(previous_plan, reason)
            )
        except Exception as exc:  # noqa: BLE001 - persisted Run failure boundary
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            await runtime._finish_span(
                llm_span,
                SpanStatus.FAILED,
                attributes={"error": str(exc), "latency_ms": latency_ms},
            )
            await runtime._finish_span(
                plan_span,
                SpanStatus.FAILED,
                attributes={"error": str(exc)},
            )
            await runtime._fail(state, exc, step="planning" if previous_plan is None else "replan")
            return None, plan_span

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
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
            },
        )
        return result, plan_span

    async def _start_task(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        task: Task,
    ) -> Checkpoint:
        if plan.status == PlanStatus.CREATED:
            plan.mark_started()
        task.mark_started()
        state.strategy_state[_CURRENT_TASK_KEY] = task.id
        state.strategy_state[_TASK_OUTPUT_KEY] = ""
        state.strategy_state[_TASK_ERROR_KEY] = ""
        state.strategy_state[_TASK_COMPLETE_KEY] = False
        state.strategy_state[_TASK_TURN_START_KEY] = state.agent_turn
        state.strategy_state[_TASK_MESSAGE_START_KEY] = len(state.messages)
        state.pending_tool_calls = []
        state.next_tool_index = 0
        state.messages.append(Message(role="user", content=_task_context(plan, task)))
        self._store_plan(state, plan)
        step_span = await self._plan_step_span(runtime, state, plan, task, reopen=True)
        await runtime._save_checkpoint(
            state,
            operation="plan.step.started",
            parent_span_id=_span_id(step_span),
        )
        await runtime._emit(
            "plan.step.started",
            {
                "run_id": state.run_id,
                "plan_id": plan.id,
                "plan_version": plan.version,
                "step_id": task.id,
                "attempt": task.attempt,
            },
        )
        return state

    async def _advance_task(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        task: Task,
    ) -> Checkpoint:
        step_span = await self._plan_step_span(runtime, state, plan, task, reopen=True)
        if state.strategy_state.get(_TASK_ERROR_KEY):
            return await self._fail_task(runtime, state, plan, task, step_span)
        if bool(state.strategy_state.get(_TASK_COMPLETE_KEY)):
            return await self._complete_task(runtime, state, plan, task, step_span)

        if state.pending_tool_calls and state.next_tool_index < len(state.pending_tool_calls):
            return await runtime._execute_pending_tool(
                state,
                parent_span_id=_span_id(step_span),
            )

        turn_start = int(state.strategy_state.get(_TASK_TURN_START_KEY) or 0)
        if state.agent_turn - turn_start >= self.max_task_turns:
            state.strategy_state[_TASK_ERROR_KEY] = (
                f"plan step exceeded max_task_turns={self.max_task_turns}"
            )
            return await self._fail_task(runtime, state, plan, task, step_span)

        state.pending_tool_calls = []
        state.next_tool_index = 0
        return await runtime._execute_llm_step(
            state,
            system_prompt=_task_system_prompt(runtime, task),
            parent_span_id=_span_id(step_span),
            complete_run=False,
            fail_run=False,
            output_state_key=_TASK_OUTPUT_KEY,
            tool_call_scope=f"plan_v{plan.version}:{task.id}:turn_{state.agent_turn}",
        )

    async def _complete_task(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        task: Task,
        step_span: Any,
    ) -> Checkpoint:
        result = str(state.strategy_state.get(_TASK_OUTPUT_KEY) or "").strip()
        if not result:
            start = int(state.strategy_state.get(_TASK_MESSAGE_START_KEY) or 0)
            result = "\n".join(
                str(message.content)
                for message in state.messages[start:]
                if message.role == "tool" and message.content
            ).strip()
        task.mark_completed(result)
        self._clear_current_task(state)
        self._store_plan(state, plan)
        await runtime._save_checkpoint(
            state,
            operation="plan.step.completed",
            parent_span_id=_span_id(step_span),
        )
        await runtime._finish_span(
            step_span,
            SpanStatus.SUCCEEDED,
            attributes={"attempt": task.attempt, "output_chars": len(result)},
        )
        await runtime._emit(
            "plan.step.completed",
            {
                "run_id": state.run_id,
                "plan_id": plan.id,
                "plan_version": plan.version,
                "step_id": task.id,
                "attempt": task.attempt,
            },
        )
        return state

    async def _fail_task(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        plan: ExecutionPlan,
        task: Task,
        step_span: Any,
    ) -> Checkpoint:
        error = str(state.strategy_state.get(_TASK_ERROR_KEY) or "plan step failed")
        task.mark_failed(error)
        self._clear_current_task(state)
        self._store_plan(state, plan)
        await runtime._save_checkpoint(
            state,
            operation="plan.step.failed",
            parent_span_id=_span_id(step_span),
        )
        await runtime._finish_span(
            step_span,
            SpanStatus.FAILED,
            attributes={"attempt": task.attempt, "error": error},
        )
        await runtime._emit(
            "plan.step.failed",
            {
                "run_id": state.run_id,
                "plan_id": plan.id,
                "plan_version": plan.version,
                "step_id": task.id,
                "attempt": task.attempt,
                "error": error,
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
        await runtime._save_checkpoint(state, operation="plan.completed")
        await runtime._emit(
            "plan.completed",
            {
                "run_id": state.run_id,
                "plan_id": plan.id,
                "plan_version": plan.version,
                "steps": len(plan.tasks),
                "replan_count": plan.replan_count,
            },
        )
        await runtime._finish_run_trace(state.status)
        await runtime._emit(
            "run.completed",
            {"run_id": state.run_id, "total_tokens": state.total_tokens},
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
            {
                "run_id": state.run_id,
                "plan_id": plan.id,
                "plan_version": plan.version,
                "reason": reason,
            },
        )
        return await runtime._fail(state, RuntimeError(reason), step="plan")

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
            span_id=_plan_step_span_id(state.run_id, plan.version, task.id),
            reopen=reopen,
            attributes={
                "plan_id": plan.id,
                "plan_version": plan.version,
                "step_id": task.id,
                "description": task.description,
                "attempt": task.attempt,
            },
        )

    def _load_plan(self, state: Checkpoint) -> ExecutionPlan | None:
        raw = state.strategy_state.get(_PLAN_KEY)
        return ExecutionPlan.from_dict(raw) if isinstance(raw, dict) else None

    def _store_plan(self, state: Checkpoint, plan: ExecutionPlan) -> None:
        state.strategy_state[_PLAN_KEY] = plan.to_dict()

    def _current_task(self, state: Checkpoint, plan: ExecutionPlan) -> Task | None:
        task_id = state.strategy_state.get(_CURRENT_TASK_KEY)
        return plan.get_task(str(task_id)) if task_id else None

    def _clear_current_task(self, state: Checkpoint) -> None:
        for key in (
            _CURRENT_TASK_KEY,
            _TASK_OUTPUT_KEY,
            _TASK_ERROR_KEY,
            _TASK_COMPLETE_KEY,
            _TASK_TURN_START_KEY,
            _TASK_MESSAGE_START_KEY,
        ):
            state.strategy_state.pop(key, None)
        state.pending_tool_calls = []
        state.next_tool_index = 0
        state.error = None


def _executable_tasks_in_order(plan: ExecutionPlan) -> list[Task]:
    executable = {task.id for task in plan.executable_tasks()}
    return [plan.tasks[task_id] for task_id in plan.execution_order() if task_id in executable]


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
        + "\n\nYou are executing one durable step inside a Plan-and-Execute strategy.\n"
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


def _normalized_description(value: str) -> str:
    return " ".join(value.casefold().split())


def _plan_step_span_id(run_id: str, plan_version: int, task_id: str) -> str:
    raw = f"{run_id}:{plan_version}:{task_id}".encode()
    return f"span_plan_{hashlib.sha256(raw).hexdigest()[:32]}"


def _span_id(span: Any) -> str | None:
    return str(span.span_id) if span is not None else None


def _preview(text: str, max_len: int = 160) -> str:
    value = (text or "").replace("\r\n", "\n").strip()
    return value if len(value) <= max_len else value[: max_len - 3] + "..."
