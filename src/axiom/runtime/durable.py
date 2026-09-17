from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import random
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.config import AxiomConfig
from axiom.context import (
    ContextBudgetExceededError,
    ContextCompactionResult,
    ContextManager,
    apply_compaction_to_strategy_state,
    compaction_count_from_strategy_state,
    context_policy_from_config,
    summary_from_strategy_state,
)
from axiom.execution import ExecutionBackend, create_execution_backend
from axiom.llm.base import LlmClient
from axiom.policy import (
    Capability,
    DefaultPermissionPolicy,
    PermissionAction,
    PermissionDecision,
    PermissionPolicy,
)
from axiom.runtime.budget import (
    BudgetExceededError,
    BudgetManager,
    ModelPricingRegistry,
    RunBudgetPolicy,
    RunBudgetState,
)
from axiom.runtime.checkpoints import (
    CheckpointConflictError,
    RuntimeStore,
    advance_run_state,
    load_run_state,
)
from axiom.runtime.completion import (
    COMPLETION_NOT_VERIFIED,
    CompletionContract,
    CompletionVerificationResult,
    CompletionVerificationStatus,
    CompletionVerifier,
    verification_feedback,
)
from axiom.runtime.dependency import (
    DEPENDENCY_DEADLINE_EXCEEDED,
    DEPENDENCY_RETRY_EXHAUSTED,
    DEPENDENCY_TIMEOUT,
    DependencyFailureCategory,
    OperationDeadline,
    RetryClassifier,
    RetryDecision,
    RetryPolicy,
    RetrySafety,
)
from axiom.runtime.models import (
    Checkpoint,
    Interrupt,
    RunError,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolRetryState,
)
from axiom.runtime.observability import Span, SpanStatus, SpanType, now, tool_span_id
from axiom.runtime.observability_store import RunTracer
from axiom.runtime.ownership import RunOwnership
from axiom.runtime.progress import (
    NoProgressError,
    ProgressDecision,
    ProgressDecisionType,
    ProgressDetector,
    ProgressObservation,
    ProgressPolicy,
    ProgressState,
    action_fingerprint,
    error_fingerprint,
    evidence_fingerprint,
    recovery_message,
)
from axiom.runtime.strategies import RuntimeExecutionStrategy, execution_strategy_from_name
from axiom.runtime.supervisor import ActiveRunSupervisor, ExecutionHandle
from axiom.tools.base import Tool, ToolContext, ToolResult
from axiom.tools.executor import ToolExecutor
from axiom.tools.registry import ToolRegistry
from axiom.types import Message

EventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class _LlmCallResult:
    text: str
    tool_calls: list[dict[str, Any]]
    stop_reason: str
    prompt_tokens: int
    completion_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    first_token_at: str | None
    ttft_ms: float | None


class _LlmStreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cached_input_tokens: int,
        reasoning_tokens: int,
    ) -> None:
        super().__init__(message)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_input_tokens = cached_input_tokens
        self.reasoning_tokens = reasoning_tokens


class DurableAgentRuntime:
    """Checkpointed ReAct execution for a single-machine runtime.

    LLM clients, tool functions, locks, and callbacks are deliberately runtime-only.
    Only JSON-compatible messages and execution metadata enter checkpoints.
    """

    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        system_prompt: str,
        cwd: str,
        config: AxiomConfig,
        store: RuntimeStore,
        retry_policy: RetryPolicy | None = None,
        event_sink: EventSink | None = None,
        tracer: RunTracer | None = None,
        permission_policy: PermissionPolicy | None = None,
        execution_backend: ExecutionBackend | None = None,
        execution_strategy: RuntimeExecutionStrategy | str = "react",
        active_run_supervisor: ActiveRunSupervisor | None = None,
        context_manager: ContextManager | None = None,
        budget_manager: BudgetManager | None = None,
        progress_detector: ProgressDetector | None = None,
        completion_verifier: CompletionVerifier | None = None,
        retry_random: Callable[[], float] | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
        ownership: RunOwnership | None = None,
        max_turns: int = 20,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.config = config
        self.store = store
        self.retry_policy = retry_policy or RetryPolicy.from_config(config.dependency)
        self.retry_classifier = RetryClassifier()
        self.retry_random = retry_random or random.random
        self.retry_sleep = retry_sleep or asyncio.sleep
        self.ownership = ownership
        self.event_sink = event_sink
        self.tracer = tracer
        self.permission_policy = permission_policy or DefaultPermissionPolicy(
            cwd,
            hitl_mode=config.policy.hitl_mode,
        )
        self.execution_backend = execution_backend or create_execution_backend(config, cwd)
        self.execution_strategy = (
            execution_strategy_from_name(execution_strategy, llm_client=llm_client)
            if isinstance(execution_strategy, str)
            else execution_strategy
        )
        self.active_run_supervisor = active_run_supervisor or ActiveRunSupervisor()
        self.context_manager = context_manager or ContextManager(
            context_policy_from_config(config, llm_client)
        )
        self.budget_manager = budget_manager or BudgetManager(
            store,
            policy=RunBudgetPolicy.from_config(config.run_budget),
            provider=llm_client.provider_name,
            model=llm_client.model_name,
            pricing_registry=ModelPricingRegistry.from_config(config.run_budget.model_pricing),
            observability_store=tracer.store if tracer is not None else None,
            ownership=ownership,
        )
        if budget_manager is not None and ownership is not None:
            self.budget_manager.ownership = ownership
        self.progress_detector = progress_detector or ProgressDetector(
            ProgressPolicy.from_config(config.progress)
        )
        self.completion_verifier = completion_verifier or CompletionVerifier()
        self.max_turns = max_turns
        self._locks: dict[str, asyncio.Lock] = {}

    async def start(
        self,
        *,
        thread_id: str,
        input: str,
        history: list[Message] | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        parent_run_id: str | None = None,
        parent_step_id: str | None = None,
        run_kind: str = "agent",
        budget_owner_run_id: str | None = None,
        completion_contract: CompletionContract | None = None,
    ) -> Checkpoint:
        if run_kind == "agent" and self.execution_strategy.name == "multi_agent":
            run_kind = "orchestrator"
        resolved_budget_owner = budget_owner_run_id
        if resolved_budget_owner is None and parent_run_id is not None:
            parent = await load_run_state(self.store, parent_run_id)
            resolved_budget_owner = (
                parent.budget_owner_run_id or parent.run_id if parent is not None else parent_run_id
            )
        state = Checkpoint.create(
            thread_id=thread_id,
            input=input,
            history=history,
            run_id=run_id,
            turn_id=turn_id,
            execution_strategy=self.execution_strategy.name,
            parent_run_id=parent_run_id,
            parent_step_id=parent_step_id,
            run_kind=run_kind,
            budget_owner_run_id=resolved_budget_owner,
            budget_policy=self.budget_manager.policy.to_dict(),
            progress_policy=self.progress_detector.policy.to_dict(),
            progress_state=ProgressState().to_dict(),
            completion_contract=(
                completion_contract.to_dict() if completion_contract is not None else None
            ),
        )

        async with self._run_lock(state.run_id):
            if await load_run_state(self.store, state.run_id) is not None:
                raise ValueError(f"run already exists: {state.run_id}")
            if self.tracer is not None:
                await self.tracer.start_run(state)
            await self.budget_manager.initialize(state)
            await self._save_checkpoint(state, operation="run.start")
            await self._emit(
                "run.started",
                {
                    "run_id": state.run_id,
                    "turn_id": state.turn_id,
                    "status": state.status.value,
                },
            )

        async def execute() -> Checkpoint:
            current = await self.reconcile_run_control_state(state.run_id)
            if current.status == RunStatus.CANCELLED:
                return current
            return await self._advance(current)

        return await self._supervise(state, execute)

    async def budget_snapshot(self, state: Checkpoint) -> RunBudgetState:
        return await self.budget_manager.snapshot(state)

    async def resume(self, run_id: str, *, decision: str | None = None) -> Checkpoint:
        state = await self._require(run_id)

        async def execute() -> Checkpoint:
            reconciled = await self.reconcile_run_control_state(run_id)
            if reconciled.status == RunStatus.CANCELLED and state.status != RunStatus.CANCELLED:
                return reconciled
            return await self._resume_inner(run_id, decision=decision)

        return await self._supervise(state, execute)

    async def _resume_inner(self, run_id: str, *, decision: str | None) -> Checkpoint:
        async with self._run_lock(run_id):
            state = await self._require(run_id)
            if state.execution_strategy != self.execution_strategy.name:
                raise ValueError(
                    "runtime execution strategy does not match persisted checkpoint: "
                    f"{self.execution_strategy.name} != {state.execution_strategy}"
                )
            if state.status == RunStatus.CANCELLED:
                raise ValueError("cancelled run cannot be resumed")
            if state.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise ValueError(f"{state.status.value.lower()} run cannot be resumed")

            was_recovery = state.status == RunStatus.RUNNING
            if self.tracer is not None:
                await self.tracer.start_run(state, recovered=was_recovery)
            await self.budget_manager.initialize(state)
            resume_span = await self._start_span(
                SpanType.AGENT,
                "resume",
                attributes={"recovered": was_recovery, "decision": decision},
            )
            await self._emit(
                "resume.started",
                {"run_id": run_id, "recovered": was_recovery, "decision": decision},
            )

            if state.status == RunStatus.WAITING_APPROVAL:
                normalized = _normalize_decision(decision)
                if normalized is None:
                    raise ValueError("approval decision must be approve or reject")
                if state.interrupt and state.interrupt.invocation_id:
                    state.decisions[state.interrupt.invocation_id] = normalized
            elif state.status == RunStatus.INTERRUPTED and decision is not None:
                normalized = _normalize_decision(decision)
                if normalized == "reject":
                    state.status = RunStatus.CANCELLED
                    state.interrupt = None
                    await self._save_checkpoint(state, operation="resume.reject")
                    await self._emit("run.cancelled", {"run_id": run_id})
                    await self._finish_span(resume_span, SpanStatus.CANCELLED)
                    await self._finish_run_trace(state.status)
                    await self._emit(
                        "resume.completed",
                        {"run_id": run_id, "status": state.status.value},
                    )
                    return state

            # WAITING_CHILD is a durable scheduling state, not an approval state.
            # Resuming it simply asks the parent strategy to observe the existing child.

            if was_recovery:
                # A no-op checkpoint is an optimistic claim. Two recovering workers
                # cannot both advance from the same sequence.
                await self._save_checkpoint(state, operation="resume.claim")
            else:
                state.status = RunStatus.RUNNING
                state.interrupt = None
                state.error = None
                await self._save_checkpoint(state, operation="resume")
            await self._update_run_trace(state.status)
            await self._emit(
                "run.resumed",
                {"run_id": run_id, "recovered": was_recovery},
            )
        try:
            state = await self._advance(state)
        except Exception as exc:
            await self._finish_span(
                resume_span,
                SpanStatus.FAILED,
                attributes={"error": _safe_error(exc)},
            )
            await self._emit(
                "resume.completed",
                {"run_id": run_id, "status": "FAILED", "error": _safe_error(exc)},
            )
            raise
        resume_status = {
            RunStatus.COMPLETED: SpanStatus.SUCCEEDED,
            RunStatus.FAILED: SpanStatus.FAILED,
            RunStatus.CANCELLED: SpanStatus.CANCELLED,
        }.get(state.status, SpanStatus.INTERRUPTED)
        await self._finish_span(
            resume_span,
            resume_status,
            attributes={"result_status": state.status.value},
        )
        if state.finished:
            await self._finish_run_trace(state.status)
        await self._emit(
            "resume.completed",
            {"run_id": run_id, "status": state.status.value},
        )
        return state

    async def interrupt(self, run_id: str, *, reason: str = "manual interrupt") -> Checkpoint:
        async with self._run_lock(run_id):
            state = await self._require(run_id)
            if state.status != RunStatus.RUNNING:
                raise ValueError(f"run in {state.status.value} cannot be interrupted")
            if self.tracer is not None:
                await self.tracer.start_run(state)
            state.status = RunStatus.INTERRUPTED
            state.interrupt = Interrupt(kind="manual", reason=reason)
            await self._save_checkpoint(state, operation="interrupt.manual")
            await self._record_interrupt(state, kind="manual", reason=reason)
            await self._update_run_trace(state.status)
            await self._emit("run.interrupted", {"run_id": run_id, "reason": reason})
            return state

    async def cancel(self, run_id: str) -> Checkpoint:
        return await self._cancel_durable(run_id, signal_owner=True)

    async def reconcile_run_control_state(self, run_id: str) -> Checkpoint:
        """Conservatively reconcile one Run against durable ancestor cancellation."""
        state = await self._require(run_id)
        if state.finished or state.parent_run_id is None:
            return state
        ancestor_id = state.parent_run_id
        visited = {state.run_id}
        cancelled_ancestor_id: str | None = None
        while ancestor_id is not None:
            if ancestor_id in visited:
                raise ValueError(f"run lineage cycle detected at {ancestor_id}")
            visited.add(ancestor_id)
            ancestor = await load_run_state(self.store, ancestor_id)
            if ancestor is None:
                break
            if ancestor.status == RunStatus.CANCELLED:
                cancelled_ancestor_id = ancestor.run_id
                break
            ancestor_id = ancestor.parent_run_id
        if cancelled_ancestor_id is None:
            return state
        reconciled = await self._cancel_durable(run_id)
        await self._emit(
            "run.control_reconciled",
            {
                "run_id": run_id,
                "status": reconciled.status.value,
                "reason": "ancestor_cancelled",
                "cancelled_ancestor_run_id": cancelled_ancestor_id,
            },
        )
        return reconciled

    async def _cancel_durable(self, run_id: str, *, signal_owner: bool = False) -> Checkpoint:
        async with self._run_lock(run_id):
            state = await self._require(run_id)
            if state.status == RunStatus.CANCELLED:
                return state
            if state.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise ValueError(f"run in {state.status.value} cannot be cancelled")
            if self.tracer is not None:
                await self.tracer.start_run(state)
            state.status = RunStatus.CANCELLED
            state.interrupt = None
            await self.execution_strategy.on_cancel(self, state)
            await self._save_checkpoint(state, operation="cancel")
            await self.execution_strategy.after_cancel(self, state)
            await self._finish_run_trace(state.status)
            await self._emit("run.cancelled", {"run_id": run_id})
            if signal_owner:
                self.active_run_supervisor.request_cancel(run_id)
            return state

    async def _supervise(
        self,
        state: Checkpoint,
        execute: Callable[[], Awaitable[Checkpoint]],
    ) -> Checkpoint:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("durable execution must run inside an asyncio Task")
        handle = self.active_run_supervisor.register(
            ExecutionHandle(
                run_id=state.run_id,
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                parent_run_id=state.parent_run_id,
                run_kind=state.run_kind,
                event_loop=loop,
                asyncio_task=task,
                owner_thread_id=threading.get_ident(),
                metadata={"execution_strategy": state.execution_strategy},
            )
        )
        try:
            return await execute()
        except asyncio.CancelledError:
            if not handle.cancellation_requested:
                raise
            return await self._converge_cancelled(state.run_id)
        except CheckpointConflictError:
            current = await load_run_state(self.store, state.run_id)
            if current is not None and current.status == RunStatus.CANCELLED:
                return current
            raise
        finally:
            self.active_run_supervisor.unregister(state.run_id, expected=handle)

    async def _converge_cancelled(self, run_id: str) -> Checkpoint:
        current = await self._require(run_id)
        if current.finished:
            return current
        return await self._cancel_durable(run_id)

    async def _advance(self, state: Checkpoint) -> Checkpoint:
        reconciled = await self.reconcile_run_control_state(state.run_id)
        if reconciled.status == RunStatus.CANCELLED:
            return reconciled
        state = reconciled
        if state.execution_strategy != self.execution_strategy.name:
            raise ValueError(
                "runtime execution strategy does not match persisted checkpoint: "
                f"{self.execution_strategy.name} != {state.execution_strategy}"
            )
        return await self.execution_strategy.advance(self, state)

    async def _advance_react(self, state: Checkpoint) -> Checkpoint:
        while state.status == RunStatus.RUNNING:
            state = await self._refresh(state)
            if state.status != RunStatus.RUNNING:
                return state

            if state.pending_tool_calls and state.next_tool_index < len(state.pending_tool_calls):
                state = await self._execute_pending_tool(state)
                continue

            if state.agent_turn >= self.max_turns:
                return await self._fail(
                    state,
                    RuntimeError(f"agent exceeded max_turns={self.max_turns}"),
                    step="llm",
                )

            state.pending_tool_calls = []
            state.next_tool_index = 0
            state = await self._execute_llm_step(state)
        return state

    async def _execute_llm_step(
        self,
        state: Checkpoint,
        *,
        system_prompt: str | None = None,
        parent_span_id: str | None = None,
        complete_run: bool = True,
        fail_run: bool = True,
        output_state_key: str | None = None,
        tool_call_scope: str | None = None,
    ) -> Checkpoint:
        try:
            await self.budget_manager.consume_step(
                state,
                f"step:{state.run_id}:{state.step_index}:llm",
            )
        except BudgetExceededError as exc:
            return await self._fail(state, exc, step="llm")
        attempt = 0
        persisted_retry = (
            state.error.metadata
            if state.error is not None
            and state.error.step == "llm"
            and state.error.metadata.get("dependency_type") == "model"
            else {}
        )
        if persisted_retry:
            attempt = int(persisted_retry.get("attempt_count") or 0)
            if (
                persisted_retry.get("retry_in_progress")
                and attempt >= self.retry_policy.max_attempts
            ):
                state.error = RunError(
                    type=DEPENDENCY_RETRY_EXHAUSTED,
                    message="LLM retry allowance exhausted before process recovery",
                    step="llm",
                    metadata={
                        **persisted_retry,
                        "retryable": False,
                        "retry_exhausted": True,
                    },
                )
                if fail_run:
                    state.status = RunStatus.FAILED
                    await self._save_checkpoint(state, operation="run.failed")
                    await self._finish_run_trace(state.status)
                else:
                    state.strategy_state["current_task_error"] = state.error.message
                    await self._save_checkpoint(state, operation="plan.step.llm_failed")
                return state
            if not persisted_retry.get("retryable"):
                if fail_run:
                    state.status = RunStatus.FAILED
                    await self._save_checkpoint(state, operation="run.failed")
                    await self._finish_run_trace(state.status)
                else:
                    state.strategy_state["current_task_error"] = state.error.message
                    await self._save_checkpoint(state, operation="plan.step.llm_failed")
                return state
            next_retry_at = persisted_retry.get("next_retry_at")
            if isinstance(next_retry_at, str):
                pending_delay = _seconds_until(next_retry_at)
                remaining = await self.budget_manager.ensure_wall_time(state)
                if remaining is not None and pending_delay >= remaining:
                    state.error = RunError(
                        type=DEPENDENCY_DEADLINE_EXCEEDED,
                        message="LLM retry backoff cannot fit inside the remaining Run deadline",
                        step="llm",
                        metadata={
                            **persisted_retry,
                            "retryable": False,
                            "retry_blocked_by_deadline": True,
                        },
                    )
                    if fail_run:
                        state.status = RunStatus.FAILED
                        await self._save_checkpoint(state, operation="run.failed")
                        await self._finish_run_trace(state.status)
                    else:
                        state.strategy_state["current_task_error"] = state.error.message
                        await self._save_checkpoint(state, operation="plan.step.llm_failed")
                    return state
                if pending_delay > 0:
                    await self.retry_sleep(pending_delay)
        while True:
            attempt += 1
            if attempt > 1 and state.error is not None:
                state.error.metadata.update(
                    {
                        "attempt_count": attempt,
                        "retry_in_progress": True,
                    }
                )
                state.error.metadata.pop("next_retry_at", None)
                await self._save_checkpoint(state, operation="llm.retry.started")
            step_index = state.step_index
            step_span = await self._start_span(
                SpanType.AGENT,
                "agent.step",
                attributes={"step_index": step_index, "kind": "llm", "attempt": attempt},
                parent_span_id=parent_span_id,
            )
            await self._emit(
                "step.started",
                {
                    "run_id": state.run_id,
                    "step_index": step_index,
                    "kind": "llm",
                    "attempt": attempt,
                },
            )
            await self._emit(
                "agent.step.started",
                {"run_id": state.run_id, "step_index": step_index, "kind": "llm"},
            )
            llm_span = await self._start_span(
                SpanType.LLM,
                "llm.chat",
                parent_span_id=_span_id(step_span),
                attributes={
                    "provider": self.llm_client.provider_name,
                    "model": self.llm_client.model_name,
                    "temperature": self.config.llm.temperature,
                    "retry_count": attempt - 1,
                },
            )
            call_started = time.perf_counter()
            await self._emit(
                "llm.started",
                {
                    "run_id": state.run_id,
                    "span_id": _span_id(llm_span),
                    "provider": self.llm_client.provider_name,
                    "model": self.llm_client.model_name,
                    "attempt": attempt,
                },
            )
            projection: ContextCompactionResult | None = None
            model_operation_id: str | None = None
            model_reserved = False
            model_accounted = False
            partial_usage = (0, 0, 0, 0)
            try:
                effective_system_prompt = system_prompt or self.system_prompt
                progress_state = ProgressState.from_dict(state.progress_state)
                model_messages = list(state.messages)
                if progress_state.recovery_signal_pending:
                    model_messages.append(
                        Message(role="user", content=recovery_message(progress_state))
                    )
                projection = await self.context_manager.prepare(
                    model_messages,
                    system_prompt=effective_system_prompt,
                    tools=self.tool_registry.definitions(),
                    objective=state.input,
                    previous_summary=summary_from_strategy_state(state.strategy_state),
                )
                compaction_count = apply_compaction_to_strategy_state(
                    state.strategy_state,
                    projection,
                )
                context_attributes = projection.observability_attributes(
                    compaction_count=compaction_count
                )
                if self.tracer is not None and llm_span is not None:
                    await self.tracer.annotate_span(llm_span.span_id, **context_attributes)
                if projection.compacted or projection.tool_results_projected:
                    await self._emit(
                        "context.compacted",
                        {"run_id": state.run_id, **context_attributes},
                    )
                model_operation_id = (
                    f"model:{state.run_id}:{_span_id(llm_span) or time.perf_counter_ns()}"
                )
                await self.budget_manager.reserve_model_call(
                    state,
                    model_operation_id,
                    estimated_input_tokens=projection.estimated_tokens_after,
                )
                model_reserved = True
                try:
                    remaining = await self.budget_manager.ensure_wall_time(state)
                    deadline = OperationDeadline(self.config.llm.timeout, remaining)
                    if deadline.exhausted:
                        raise TimeoutError("LLM operation deadline exhausted")
                    if self.tracer is not None and llm_span is not None:
                        await self.tracer.annotate_span(
                            llm_span.span_id,
                            **{
                                "dependency.timeout_ms": round(
                                    deadline.effective_timeout_seconds * 1000, 3
                                )
                            },
                        )
                    llm_result = await asyncio.wait_for(
                        self._collect_llm_response(
                            state,
                            call_started=call_started,
                            system_prompt=effective_system_prompt,
                            messages=projection.messages,
                        ),
                        timeout=deadline.effective_timeout_seconds,
                    )
                except _LlmStreamError as stream_error:
                    partial_usage = (
                        stream_error.prompt_tokens,
                        stream_error.completion_tokens,
                        stream_error.cached_input_tokens,
                        stream_error.reasoning_tokens,
                    )
                    await self.budget_manager.complete_model_call(
                        state,
                        model_operation_id,
                        input_tokens=stream_error.prompt_tokens,
                        output_tokens=stream_error.completion_tokens,
                        cached_input_tokens=stream_error.cached_input_tokens,
                        reasoning_tokens=stream_error.reasoning_tokens,
                    )
                    model_accounted = True
                    raise
                partial_usage = (
                    llm_result.prompt_tokens,
                    llm_result.completion_tokens,
                    llm_result.cached_input_tokens,
                    llm_result.reasoning_tokens,
                )
                await self.budget_manager.complete_model_call(
                    state,
                    model_operation_id,
                    input_tokens=llm_result.prompt_tokens,
                    output_tokens=llm_result.completion_tokens,
                    cached_input_tokens=llm_result.cached_input_tokens,
                    reasoning_tokens=llm_result.reasoning_tokens,
                )
                model_accounted = True
                if progress_state.recovery_signal_pending:
                    self.progress_detector.mark_recovery_delivered(progress_state)
                    state.progress_state = progress_state.to_dict()
            except Exception as exc:
                if model_operation_id is not None and model_reserved and not model_accounted:
                    with suppress(BudgetExceededError):
                        await self.budget_manager.complete_model_call(
                            state,
                            model_operation_id,
                            input_tokens=partial_usage[0],
                            output_tokens=partial_usage[1],
                            cached_input_tokens=partial_usage[2],
                            reasoning_tokens=partial_usage[3],
                        )
                message = _safe_error(exc)
                latency_ms = round((time.perf_counter() - call_started) * 1000, 3)
                context_attributes = (
                    projection.observability_attributes(
                        compaction_count=compaction_count_from_strategy_state(state.strategy_state)
                    )
                    if projection is not None
                    else {}
                )
                if isinstance(exc, ContextBudgetExceededError):
                    context_attributes.update(
                        {
                            "context.estimated_tokens_before": exc.estimated_tokens,
                            "context.estimated_tokens_after": exc.estimated_tokens,
                            "context.compaction_triggered": False,
                            "context.compaction_count": compaction_count_from_strategy_state(
                                state.strategy_state
                            ),
                            "context.compression_ratio": 1.0,
                            "context.evicted_messages": 0,
                            "context.preserved_messages": len(state.messages),
                            "context.tool_results_projected": 0,
                            "context.trigger_reason": "hard_input_limit",
                        }
                    )
                await self._finish_span(
                    llm_span,
                    SpanStatus.FAILED,
                    attributes={
                        "latency_ms": latency_ms,
                        "error": message,
                        "retry_count": attempt - 1,
                        "prompt_tokens": partial_usage[0],
                        "completion_tokens": partial_usage[1],
                        "total_tokens": partial_usage[0] + partial_usage[1],
                        **context_attributes,
                    },
                )
                await self._emit(
                    "llm.failed",
                    {
                        "run_id": state.run_id,
                        "span_id": _span_id(llm_span),
                        "latency_ms": latency_ms,
                        "error": message,
                        "attempt": attempt,
                        **(
                            {"code": exc.code}
                            if isinstance(exc, (ContextBudgetExceededError, BudgetExceededError))
                            else {}
                        ),
                    },
                )
                if isinstance(exc, BudgetExceededError):
                    await self.budget_manager.record_hard_limit(state, exc)
                if isinstance(exc, (ContextBudgetExceededError, BudgetExceededError)):
                    decision = RetryDecision(
                        retry=False,
                        category=DependencyFailureCategory.PERMANENT_ERROR,
                        reason="outer Runtime control is authoritative",
                    )
                else:
                    category = self.retry_classifier.classify(exc)
                    try:
                        remaining = await self.budget_manager.ensure_wall_time(state)
                    except BudgetExceededError as deadline_error:
                        await self.budget_manager.record_hard_limit(state, deadline_error)
                        exc = deadline_error
                        message = _safe_error(deadline_error)
                        decision = RetryDecision(
                            retry=False,
                            category=category,
                            reason="outer Runtime wall-time budget is exhausted",
                        )
                    else:
                        decision = self.retry_policy.decide(
                            category=category,
                            attempt=attempt,
                            safety=RetrySafety.SAFE,
                            error=message,
                            random_source=self.retry_random,
                            remaining_seconds=remaining,
                            retry_after_seconds=self.retry_classifier.retry_after_seconds(exc),
                        )
                error_type = self._dependency_error_code(exc, decision)
                retry_metadata = self._retry_metadata("model", attempt, decision)
                retry_metadata["retry_in_progress"] = False
                if decision.retry:
                    retry_metadata["next_retry_at"] = _after_seconds(decision.delay_seconds)
                state.error = RunError(
                    type=error_type,
                    message=message,
                    step="llm",
                    metadata={
                        **(exc.metadata() if isinstance(exc, BudgetExceededError) else {}),
                        **retry_metadata,
                    },
                )
                await self._save_checkpoint(
                    state,
                    operation="llm.failed",
                    parent_span_id=_span_id(step_span),
                )
                if self.tracer is not None and llm_span is not None:
                    await self.tracer.annotate_span(
                        llm_span.span_id,
                        **self._retry_span_attributes(attempt, decision),
                    )
                await self._finish_span(
                    step_span,
                    SpanStatus.FAILED,
                    attributes={"error": message, **self._retry_span_attributes(attempt, decision)},
                )
                await self._emit(
                    "step.failed",
                    {
                        "run_id": state.run_id,
                        "step_index": state.step_index,
                        "kind": "llm",
                        "attempt": attempt,
                        "error": message,
                        **self._retry_metadata("model", attempt, decision),
                    },
                )
                await self._emit(
                    "agent.step.failed",
                    {"run_id": state.run_id, "step_index": step_index, "error": message},
                )
                if not decision.retry:
                    if fail_run:
                        state.status = RunStatus.FAILED
                        await self._save_checkpoint(state, operation="run.failed")
                        await self._finish_run_trace(state.status)
                        await self._emit("run.failed", {"run_id": state.run_id, "error": message})
                    else:
                        state.strategy_state["current_task_error"] = message
                        await self._save_checkpoint(state, operation="plan.step.llm_failed")
                    return state
                if decision.delay_seconds > 0:
                    await self.retry_sleep(decision.delay_seconds)
                continue

            latency_ms = round((time.perf_counter() - call_started) * 1000, 3)
            await self._finish_span(
                llm_span,
                SpanStatus.SUCCEEDED,
                attributes={
                    "prompt_tokens": llm_result.prompt_tokens,
                    "completion_tokens": llm_result.completion_tokens,
                    "cached_input_tokens": llm_result.cached_input_tokens,
                    "reasoning_tokens": llm_result.reasoning_tokens,
                    "total_tokens": llm_result.prompt_tokens + llm_result.completion_tokens,
                    "first_token_at": llm_result.first_token_at,
                    "ttft_ms": llm_result.ttft_ms,
                    "latency_ms": latency_ms,
                    "finish_reason": llm_result.stop_reason,
                    "retry_count": attempt - 1,
                    "dependency.retry_attempt": attempt,
                    "dependency.retry_count": attempt - 1,
                    **projection.observability_attributes(
                        compaction_count=compaction_count_from_strategy_state(state.strategy_state)
                    ),
                },
            )
            await self._emit(
                "llm.completed",
                {
                    "run_id": state.run_id,
                    "span_id": _span_id(llm_span),
                    "prompt_tokens": llm_result.prompt_tokens,
                    "completion_tokens": llm_result.completion_tokens,
                    "ttft_ms": llm_result.ttft_ms,
                    "latency_ms": latency_ms,
                    "finish_reason": llm_result.stop_reason,
                },
            )

            state.error = None
            scoped_tool_calls = _scope_tool_call_ids(llm_result.tool_calls, tool_call_scope)
            state.messages.append(
                Message(
                    role="assistant",
                    content=llm_result.text,
                    tool_calls=scoped_tool_calls,
                )
            )
            if output_state_key is None:
                state.output_text += llm_result.text
            else:
                current = str(state.strategy_state.get(output_state_key) or "")
                state.strategy_state[output_state_key] = current + llm_result.text
            state.pending_tool_calls = scoped_tool_calls
            state.next_tool_index = 0
            state.agent_turn += 1
            state.step_index += 1
            state.total_tokens += llm_result.prompt_tokens + llm_result.completion_tokens
            candidate_completion = not scoped_tool_calls and llm_result.stop_reason != "tool_use"
            if candidate_completion:
                if complete_run:
                    await self._apply_completion_verification(
                        state,
                        allow_correction=True,
                        parent_span_id=_span_id(step_span),
                    )
                else:
                    state.strategy_state["current_task_complete"] = True
            await self._save_checkpoint(
                state,
                operation="llm.completed",
                parent_span_id=_span_id(step_span),
            )
            await self._finish_span(
                step_span,
                SpanStatus.SUCCEEDED,
                attributes={
                    "finish_reason": llm_result.stop_reason,
                    "tool_calls": len(scoped_tool_calls),
                },
            )
            await self._emit(
                "step.completed",
                {
                    "run_id": state.run_id,
                    "step_index": state.step_index - 1,
                    "kind": "llm",
                    "stop_reason": llm_result.stop_reason,
                    "tool_calls": len(scoped_tool_calls),
                },
            )
            await self._emit(
                "agent.step.completed",
                {"run_id": state.run_id, "step_index": step_index, "kind": "llm"},
            )
            if state.status == RunStatus.COMPLETED:
                await self._finish_run_trace(state.status)
                await self._emit(
                    "run.completed",
                    {"run_id": state.run_id, "total_tokens": state.total_tokens},
                )
            elif state.status == RunStatus.FAILED:
                await self._finish_run_trace(state.status)
                await self._emit(
                    "run.failed",
                    {
                        "run_id": state.run_id,
                        "error": state.error.to_dict() if state.error else None,
                    },
                )
            return state

    async def _apply_completion_verification(
        self,
        state: Checkpoint,
        *,
        allow_correction: bool,
        parent_span_id: str | None = None,
    ) -> CompletionVerificationResult | None:
        if not state.completion_contract:
            state.status = RunStatus.COMPLETED
            return None
        contract = CompletionContract.from_dict(state.completion_contract)
        state.completion_verification_attempts += 1
        attempt = state.completion_verification_attempts
        span = await self._start_span(
            SpanType.VERIFICATION,
            "completion.verify",
            parent_span_id=parent_span_id,
            attributes={"verification.attempt": attempt},
        )
        result = await self.completion_verifier.verify(
            state,
            contract,
            store=self.store,
            cwd=self.cwd,
            attempt=attempt,
        )
        state.completion_verification = result.to_dict()
        attributes: dict[str, object] = {
            "verification.status": result.status.value,
            "verification.verified": result.verified,
            "verification.attempt": result.attempt,
            "verification.check_count": len(result.checks),
            "verification.failed_check_ids": list(result.failed_check_ids),
        }
        if self.tracer is not None:
            await self.tracer.annotate_span(self.tracer.root_span_id, **attributes)
        await self._finish_span(
            span,
            (
                SpanStatus.SUCCEEDED
                if result.status
                in {
                    CompletionVerificationStatus.VERIFIED,
                    CompletionVerificationStatus.NOT_APPLICABLE,
                }
                else SpanStatus.FAILED
            ),
            attributes=attributes,
        )
        await self._emit(
            "completion.verification.completed",
            {
                "run_id": state.run_id,
                "status": result.status.value,
                "verified": result.verified,
                "attempt": result.attempt,
                "failed_check_ids": list(result.failed_check_ids),
            },
        )
        if result.status in {
            CompletionVerificationStatus.VERIFIED,
            CompletionVerificationStatus.NOT_APPLICABLE,
        }:
            state.status = RunStatus.COMPLETED
            state.error = None
            return result
        if (
            result.status == CompletionVerificationStatus.NOT_VERIFIED
            and allow_correction
            and attempt <= contract.max_correction_attempts
        ):
            state.status = RunStatus.RUNNING
            state.messages.append(Message(role="user", content=verification_feedback(result)))
            return result
        state.status = RunStatus.FAILED
        state.error = RunError(
            type=COMPLETION_NOT_VERIFIED,
            message=(
                "completion verification did not pass"
                if result.status == CompletionVerificationStatus.NOT_VERIFIED
                else "completion verification errored"
            ),
            step="completion_verification",
            metadata={
                "verification_status": result.status.value,
                "attempt": result.attempt,
                "failed_check_ids": list(result.failed_check_ids),
            },
        )
        return result

    async def _collect_llm_response(
        self,
        state: Checkpoint,
        *,
        call_started: float,
        system_prompt: str | None = None,
        messages: list[Message] | None = None,
    ) -> _LlmCallResult:
        text = ""
        stop_reason = "end_turn"
        prompt_tokens = 0
        completion_tokens = 0
        cached_input_tokens = 0
        reasoning_tokens = 0
        first_token_at: str | None = None
        ttft_ms: float | None = None
        tool_states: dict[int, dict[str, Any]] = {}
        try:
            async for event in self.llm_client.chat(
                messages if messages is not None else state.messages,
                self.tool_registry.definitions(),
                system_prompt=system_prompt or self.system_prompt,
            ):
                event_type = event.get("type")
                if (
                    event_type in {"text_delta", "thinking_delta", "tool_call_delta"}
                    and first_token_at is None
                ):
                    first_token_at = now()
                    ttft_ms = round((time.perf_counter() - call_started) * 1000, 3)
                if event_type == "text_delta":
                    text += str(event.get("text") or "")
                elif event_type == "tool_call_delta" and isinstance(event.get("tool_call"), dict):
                    _merge_tool_delta(tool_states, event["tool_call"], state.agent_turn)
                elif event_type == "message_end":
                    stop_reason = str(event.get("stop_reason") or "end_turn")
                elif event_type == "usage":
                    usage = event.get("usage") or {}
                    if isinstance(usage, dict):
                        prompt_tokens += int(usage.get("input_tokens") or 0)
                        completion_tokens += int(usage.get("output_tokens") or 0)
                        cached_input_tokens += int(usage.get("cached_input_tokens") or 0)
                        reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
                elif event_type == "error":
                    error = event.get("error")
                    if isinstance(error, BaseException):
                        raise error
                    raise RuntimeError(str(error or "LLM stream failed"))
        except Exception as exc:
            raise _LlmStreamError(
                _safe_error(exc),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_input_tokens=cached_input_tokens,
                reasoning_tokens=reasoning_tokens,
            ) from exc
        return _LlmCallResult(
            text=text,
            tool_calls=_finalize_tool_calls(tool_states),
            stop_reason=stop_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_input_tokens=cached_input_tokens,
            reasoning_tokens=reasoning_tokens,
            first_token_at=first_token_at,
            ttft_ms=ttft_ms,
        )

    async def _execute_pending_tool(
        self,
        state: Checkpoint,
        *,
        parent_span_id: str | None = None,
    ) -> Checkpoint:
        try:
            await self.budget_manager.consume_step(
                state,
                f"step:{state.run_id}:{state.step_index}:tool",
            )
        except BudgetExceededError as exc:
            return await self._fail(state, exc, step="tool")
        step_index = state.step_index
        step_span = await self._start_span(
            SpanType.AGENT,
            "agent.step",
            attributes={"step_index": step_index, "kind": "tool"},
            parent_span_id=parent_span_id,
        )
        await self._emit(
            "agent.step.started",
            {"run_id": state.run_id, "step_index": step_index, "kind": "tool"},
        )
        try:
            result = await self._execute_pending_tool_inner(
                state,
                parent_span_id=_span_id(step_span),
            )
        except Exception as exc:
            await self._finish_span(
                step_span,
                SpanStatus.FAILED,
                attributes={"error": _safe_error(exc)},
            )
            await self._emit(
                "agent.step.failed",
                {"run_id": state.run_id, "step_index": step_index, "error": _safe_error(exc)},
            )
            raise
        step_status = {
            RunStatus.FAILED: SpanStatus.FAILED,
            RunStatus.CANCELLED: SpanStatus.CANCELLED,
            RunStatus.INTERRUPTED: SpanStatus.INTERRUPTED,
            RunStatus.WAITING_APPROVAL: SpanStatus.INTERRUPTED,
        }.get(result.status, SpanStatus.SUCCEEDED)
        await self._finish_span(
            step_span,
            step_status,
            attributes={"result_status": result.status.value},
        )
        await self._emit(
            "agent.step.completed",
            {
                "run_id": state.run_id,
                "step_index": step_index,
                "kind": "tool",
                "status": step_status.value,
            },
        )
        return result

    async def _execute_pending_tool_inner(
        self,
        state: Checkpoint,
        *,
        parent_span_id: str | None,
    ) -> Checkpoint:
        call = state.pending_tool_calls[state.next_tool_index]
        tool_call_id = str(call.get("id") or f"call_{state.agent_turn}_{state.next_tool_index}")
        call["id"] = tool_call_id
        name = _tool_call_name(call)
        payload = _tool_call_arguments(call)
        arguments_hash = _arguments_hash(payload)
        invocation_id = f"{state.run_id}:{tool_call_id}"
        tool = self.tool_registry.get(name)
        approval_decision = state.decisions.get(invocation_id)
        existing = await self.store.load_tool_execution(invocation_id)

        if existing and existing.arguments_hash != arguments_hash:
            return await self._fail(
                state,
                RuntimeError("tool invocation arguments changed after checkpoint"),
                step="tool",
            )

        if existing and existing.status == ToolExecutionStatus.SUCCEEDED:
            tool_span = await self._start_tool_span(
                invocation_id,
                name,
                parent_span_id=parent_span_id,
                attributes={
                    "approval_required": self._may_require_approval(tool),
                    "reused_result": True,
                    "retry_count": max(0, existing.attempt - 1),
                },
            )
            if tool_span is not None and tool_span.status == SpanStatus.RUNNING:
                await self._finish_span(
                    tool_span,
                    SpanStatus.SUCCEEDED,
                    attributes={"reused_result": True},
                )
            elif tool_span is not None and self.tracer is not None:
                await self.tracer.annotate_span(tool_span.span_id, reused_result=True)
            result = ToolResult(
                content=existing.result or "",
                is_error=False,
                tool_use_id=tool_call_id,
            )
            return await self._apply_tool_result(
                state,
                invocation_id,
                name,
                result,
                reused=True,
                parent_span_id=parent_span_id,
            )

        retry_safety = self._tool_retry_safety(tool)

        if existing and existing.status in {
            ToolExecutionStatus.FAILED,
            ToolExecutionStatus.UNKNOWN,
        } and (
            existing.retry_state
            in {ToolRetryState.RETRY_SUPPRESSED, ToolRetryState.RETRY_EXHAUSTED}
            or existing.attempt >= self.retry_policy.max_attempts
        ):
            tool_span = await self._start_tool_span(
                invocation_id,
                name,
                parent_span_id=parent_span_id,
                attributes={
                    "reused_result": True,
                    "retry_count": max(0, existing.attempt - 1),
                    "dependency.failure_category": existing.last_failure_category,
                    "dependency.retry_exhausted": existing.retry_exhausted,
                    "dependency.unsafe_retry_suppressed": (
                        existing.retry_suppressed_reason == "unsafe"
                    ),
                },
            )
            await self._finish_span(tool_span, SpanStatus.FAILED)
            return await self._apply_tool_result(
                state,
                invocation_id,
                name,
                ToolResult(
                    content=existing.result or existing.error or "tool dependency failed",
                    is_error=True,
                    tool_use_id=tool_call_id,
                    metadata={
                        "failure_category": existing.last_failure_category,
                        "error_code": existing.last_error_code,
                        "retry_exhausted": existing.retry_exhausted,
                        "unsafe_retry_suppressed": (
                            existing.retry_suppressed_reason == "unsafe"
                        ),
                    },
                ),
                reused=True,
                parent_span_id=parent_span_id,
            )

        if existing and existing.status == ToolExecutionStatus.RUNNING:
            if existing.attempt >= self.retry_policy.max_attempts:
                existing.status = ToolExecutionStatus.FAILED
                existing.is_error = True
                existing.result = existing.error or "tool result remained unknown after restart"
                existing.last_failure_category = DependencyFailureCategory.UNKNOWN.value
                existing.last_error_code = DEPENDENCY_RETRY_EXHAUSTED
                existing.retry_state = ToolRetryState.RETRY_EXHAUSTED
                existing.completed_at = _now()
                await self._save_tool_execution(existing)
                return await self._execute_pending_tool_inner(
                    state,
                    parent_span_id=parent_span_id,
                )
            retry_is_safe = retry_safety != RetrySafety.UNSAFE
            if not retry_is_safe and approval_decision != "approve":
                tool_span = await self._start_tool_span(
                    invocation_id,
                    name,
                    parent_span_id=parent_span_id,
                    attributes={
                        "approval_required": self._may_require_approval(tool),
                        "ambiguous_execution": True,
                        "retry_count": max(0, existing.attempt - 1),
                    },
                )
                if tool_span is not None and self.tracer is not None:
                    await self.tracer.annotate_span(
                        tool_span.span_id,
                        ambiguous_execution=True,
                    )
                return await self._wait_for_approval(
                    state,
                    invocation_id=invocation_id,
                    tool_name=name,
                    arguments=payload,
                    kind="ambiguous_tool_execution",
                    reason=(
                        "The process stopped after tool execution started but before success was "
                        "persisted. Retrying may duplicate an external side effect."
                    ),
                    parent_span_id=parent_span_id,
                )

        permission = await self._permission_decision(
            state,
            tool=tool,
            arguments=payload,
            invocation_id=invocation_id,
            parent_span_id=parent_span_id,
        )
        if permission.action == PermissionAction.REQUIRE_APPROVAL:
            return await self._wait_for_approval(
                state,
                invocation_id=invocation_id,
                tool_name=name,
                arguments=payload,
                kind="tool_approval",
                reason=permission.reason,
                parent_span_id=parent_span_id,
            )

        if permission.action == PermissionAction.DENY:
            record = existing or ToolExecutionRecord(
                invocation_id=invocation_id,
                run_id=state.run_id,
                tool_call_id=tool_call_id,
                tool_name=name,
                arguments_hash=arguments_hash,
                status=ToolExecutionStatus.FAILED,
            )
            record.status = ToolExecutionStatus.FAILED
            record.error = f"denied by permission policy: {permission.reason}"
            record.is_error = True
            record.completed_at = _now()
            await self._save_tool_execution(record)
            tool_span = await self._start_tool_span(
                invocation_id,
                name,
                parent_span_id=parent_span_id,
                attributes={
                    "approval_required": True,
                    "reused_result": False,
                    "retry_count": max(0, record.attempt - 1),
                    "rejected": True,
                    "permission_action": permission.action.value,
                    "permission_reason": permission.reason,
                    "permission_rule": permission.matched_rule,
                },
            )
            await self._finish_span(
                tool_span,
                SpanStatus.FAILED,
                attributes={
                    "error": record.error,
                    "rejected": True,
                    "permission_action": permission.action.value,
                    "permission_reason": permission.reason,
                    "permission_rule": permission.matched_rule,
                },
            )
            result = ToolResult(
                content=f'Tool "{name}" execution denied by permission policy: {permission.reason}',
                is_error=True,
                tool_use_id=tool_call_id,
            )
            return await self._apply_tool_result(
                state,
                invocation_id,
                name,
                result,
                parent_span_id=parent_span_id,
            )

        record = existing or ToolExecutionRecord(
            invocation_id=invocation_id,
            run_id=state.run_id,
            tool_call_id=tool_call_id,
            tool_name=name,
            arguments_hash=arguments_hash,
            status=ToolExecutionStatus.PENDING,
        )
        if existing is None:
            await self._save_tool_execution(record)

        tool_span = await self._start_tool_span(
            invocation_id,
            name,
            parent_span_id=parent_span_id,
            attributes={
                "approval_required": self._may_require_approval(tool),
                "permission_action": permission.action.value,
                "permission_reason": permission.reason,
                "permission_rule": permission.matched_rule,
                "reused_result": False,
                "ambiguous_execution": bool(
                    existing and existing.status == ToolExecutionStatus.RUNNING
                ),
                "tool_call_id": tool_call_id,
                **self._execution_start_attributes(tool, payload),
            },
            reopen=bool(existing and existing.status == ToolExecutionStatus.RUNNING),
        )

        while True:
            if record.next_retry_at:
                pending_delay = _seconds_until(record.next_retry_at)
                remaining = await self.budget_manager.ensure_wall_time(state)
                if remaining is not None and pending_delay >= remaining:
                    record.retry_state = ToolRetryState.RETRY_SUPPRESSED
                    record.retry_suppressed_reason = "deadline"
                    record.last_error_code = DEPENDENCY_DEADLINE_EXCEEDED
                    record.next_retry_at = None
                    await self._save_tool_execution(record)
                    await self._finish_span(
                        tool_span,
                        SpanStatus.FAILED,
                        attributes={
                            "dependency.retry_blocked_by_deadline": True,
                            "dependency.backoff_ms": round(pending_delay * 1000, 3),
                        },
                    )
                    result = ToolResult(
                        content=record.result or record.error or "tool retry missed Run deadline",
                        is_error=True,
                        tool_use_id=tool_call_id,
                        metadata={
                            "error_code": DEPENDENCY_DEADLINE_EXCEEDED,
                            "failure_category": record.last_failure_category,
                            "retry_blocked_by_deadline": True,
                        },
                    )
                    return await self._apply_tool_result(
                        state,
                        invocation_id,
                        name,
                        result,
                        parent_span_id=parent_span_id,
                    )
                if pending_delay > 0:
                    await self.retry_sleep(pending_delay)
                record.next_retry_at = None
                record.retry_state = ToolRetryState.NONE
                await self._save_tool_execution(record)
            next_attempt = record.attempt + 1
            try:
                await self.budget_manager.consume_tool_call(
                    state,
                    f"tool:{invocation_id}:attempt:{next_attempt}",
                )
            except BudgetExceededError as exc:
                return await self._fail(state, exc, step="tool")
            record.attempt += 1
            record.status = ToolExecutionStatus.RUNNING
            record.started_at = record.started_at or _now()
            record.error = None
            await self._save_tool_execution(record)
            if tool_span is not None and self.tracer is not None:
                await self.tracer.annotate_span(
                    tool_span.span_id,
                    attempt=record.attempt,
                    retry_count=max(0, record.attempt - 1),
                )
            await self._emit(
                "tool.started",
                {
                    "run_id": state.run_id,
                    "invocation_id": invocation_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": name,
                    "arguments": payload,
                    "attempt": record.attempt,
                    **self._execution_start_attributes(tool, payload),
                },
            )

            execution_call = _call_with_payload(call, payload, invocation_id, tool)
            context = ToolContext(
                cwd=self.cwd,
                config=self.config,
                approval_callback=lambda _request: "approve",
                invocation_id=invocation_id,
                run_id=state.run_id,
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                workspace=self.cwd,
                permission_policy=self.permission_policy,
                preauthorized_invocation_id=invocation_id,
                execution_backend=self.execution_backend,
            )
            try:
                remaining = await self.budget_manager.ensure_wall_time(state)
                configured_timeout = min(
                    tool.timeout if tool is not None else self.config.tools.timeout,
                    self.config.tools.timeout,
                )
                deadline = OperationDeadline(configured_timeout, remaining)
                if deadline.exhausted:
                    raise TimeoutError("Tool operation deadline exhausted")
                context.operation_timeout_seconds = deadline.effective_timeout_seconds
                if tool_span is not None and self.tracer is not None:
                    await self.tracer.annotate_span(
                        tool_span.span_id,
                        **{
                            "dependency.timeout_ms": round(
                                deadline.effective_timeout_seconds * 1000, 3
                            )
                        },
                    )
                result = await ToolExecutor(
                    self.tool_registry,
                    execution_backend=self.execution_backend,
                ).execute_one(execution_call, context)
            except asyncio.CancelledError:
                unsafe_outcome = retry_safety == RetrySafety.UNSAFE
                record.status = (
                    ToolExecutionStatus.UNKNOWN
                    if unsafe_outcome
                    else ToolExecutionStatus.FAILED
                )
                record.is_error = True
                record.error = "tool execution cancelled"
                record.last_failure_category = DependencyFailureCategory.UNKNOWN.value
                record.last_error_code = "CancelledError"
                record.retry_state = ToolRetryState.RETRY_SUPPRESSED
                record.retry_suppressed_reason = "unsafe" if unsafe_outcome else "cancelled"
                record.next_retry_at = None
                record.completed_at = _now()
                await self._save_tool_execution(record)
                cancelled = {
                    **self._execution_start_attributes(tool, payload),
                    "cancelled": True,
                }
                await self._finish_span(
                    tool_span,
                    SpanStatus.CANCELLED,
                    attributes={"error": record.error, **cancelled},
                )
                await self._emit(
                    "tool.failed",
                    {
                        "run_id": state.run_id,
                        "invocation_id": invocation_id,
                        "tool_name": name,
                        "attempt": record.attempt,
                        "error": record.error,
                        "execution": cancelled,
                    },
                )
                raise
            if not result.is_error:
                record.status = ToolExecutionStatus.SUCCEEDED
                record.result = result.content
                record.is_error = False
                record.error = None
                record.completed_at = _now()
                record.last_failure_category = None
                record.last_error_code = None
                record.retry_state = ToolRetryState.NONE
                record.retry_suppressed_reason = None
                record.next_retry_at = None
                await self._save_tool_execution(record)
                await self._finish_span(
                    tool_span,
                    SpanStatus.SUCCEEDED,
                    attributes={
                        "attempt": record.attempt,
                        "retry_count": max(0, record.attempt - 1),
                        "dependency.retry_attempt": record.attempt,
                        "dependency.retry_count": max(0, record.attempt - 1),
                        "dependency.backoff_ms": round(
                            record.retry_backoff_seconds * 1000, 3
                        ),
                        **result.metadata,
                    },
                )
                return await self._apply_tool_result(
                    state,
                    invocation_id,
                    name,
                    result,
                    parent_span_id=parent_span_id,
                )

            record.status = ToolExecutionStatus.FAILED
            record.result = result.content
            record.is_error = True
            record.error = result.content
            record.completed_at = _now()
            category = self.retry_classifier.classify(result.metadata or result.content)
            remaining = await self.budget_manager.remaining_wall_time(state)
            decision = self.retry_policy.decide(
                category=category,
                attempt=record.attempt,
                safety=retry_safety,
                error=result.content,
                random_source=self.retry_random,
                remaining_seconds=remaining,
                retry_after_seconds=self.retry_classifier.retry_after_seconds(result.metadata),
            )
            record.last_failure_category = category.value
            record.last_error_code = self._dependency_error_code(
                RuntimeError(result.content),
                decision,
            )
            record.retry_suppressed_reason = (
                None
                if decision.exhausted
                else "unsafe"
                if decision.unsafe_suppressed
                else "deadline"
                if decision.blocked_by_deadline
                else "not_retryable"
                if not decision.retry
                else None
            )
            record.status = (
                ToolExecutionStatus.UNKNOWN
                if decision.unsafe_suppressed
                else ToolExecutionStatus.FAILED
            )
            result.metadata.update(
                {
                    "error_code": record.last_error_code,
                    **self._retry_metadata("tool", record.attempt, decision),
                }
            )
            if decision.retry:
                record.retry_state = ToolRetryState.RETRY_PENDING
                record.retry_backoff_seconds += decision.delay_seconds
                record.next_retry_at = _after_seconds(decision.delay_seconds)
            else:
                record.retry_state = (
                    ToolRetryState.RETRY_EXHAUSTED
                    if decision.exhausted
                    else ToolRetryState.RETRY_SUPPRESSED
                )
                record.next_retry_at = None
            await self._save_tool_execution(record)
            await self._emit(
                "tool.failed",
                {
                    "run_id": state.run_id,
                    "invocation_id": invocation_id,
                    "tool_name": name,
                    "attempt": record.attempt,
                    "error": result.content,
                    "execution": result.metadata or None,
                },
            )
            if tool_span is not None and self.tracer is not None:
                await self.tracer.annotate_span(
                    tool_span.span_id,
                    **self._retry_span_attributes(record.attempt, decision),
                )
            if not decision.retry:
                await self._finish_span(
                    tool_span,
                    SpanStatus.FAILED,
                    attributes={
                        "attempt": record.attempt,
                        "retry_count": max(0, record.attempt - 1),
                        "error": result.content,
                        "dependency.backoff_ms": round(
                            record.retry_backoff_seconds * 1000, 3
                        ),
                        **result.metadata,
                    },
                )
                return await self._apply_tool_result(
                    state,
                    invocation_id,
                    name,
                    result,
                    parent_span_id=parent_span_id,
                )

    async def _apply_tool_result(
        self,
        state: Checkpoint,
        invocation_id: str,
        tool_name: str,
        result: ToolResult,
        *,
        reused: bool = False,
        parent_span_id: str | None = None,
    ) -> Checkpoint:
        call = state.pending_tool_calls[state.next_tool_index]
        arguments = _tool_call_arguments(call)
        state.messages.append(
            Message(
                role="tool",
                content=result.content,
                name=f"{tool_name}{' (error)' if result.is_error else ''}",
                tool_call_id=result.tool_use_id,
            )
        )
        state.next_tool_index += 1
        state.step_index += 1
        state.interrupt = None
        state.decisions.pop(invocation_id, None)
        if not reused:
            progress_fingerprint = (
                evidence_fingerprint(result.content) if not result.is_error else None
            )
            decision = await self.observe_progress(
                state,
                ProgressObservation(
                    operation_id=f"tool:{invocation_id}",
                    step=state.step_index,
                    action_fingerprint=action_fingerprint(tool_name, arguments),
                    error_fingerprint=(
                        error_fingerprint(
                            str(result.metadata.get("error_type") or "tool_error"),
                            result.content,
                            tool_name=tool_name,
                            category=str(result.metadata.get("category") or ""),
                        )
                        if result.is_error
                        else None
                    ),
                    state_fingerprint=progress_fingerprint,
                ),
                parent_span_id=parent_span_id,
            )
            if decision.decision == ProgressDecisionType.TERMINATE:
                return state
        await self._save_checkpoint(
            state,
            operation="tool.completed",
            parent_span_id=parent_span_id,
        )
        await self._emit(
            "tool.completed",
            {
                "run_id": state.run_id,
                "invocation_id": invocation_id,
                "tool_name": tool_name,
                "is_error": result.is_error,
                "reused": reused,
                "tool_call_id": result.tool_use_id,
                "result": result.content,
                "execution": result.metadata or None,
            },
        )
        return state

    async def observe_progress(
        self,
        state: Checkpoint,
        observation: ProgressObservation,
        *,
        parent_span_id: str | None = None,
    ) -> ProgressDecision:
        """Apply one restart-safe observation and persist its bounded projection."""

        detector = ProgressDetector(
            ProgressPolicy.from_dict(state.progress_policy)
            if state.progress_policy
            else self.progress_detector.policy
        )
        progress_state = ProgressState.from_dict(state.progress_state)
        decision = detector.observe(progress_state, observation)
        state.progress_state = progress_state.to_dict()
        attributes = progress_state.observability_attributes()
        if self.tracer is not None:
            await self.tracer.annotate_span(self.tracer.root_span_id, **attributes)
        if decision.detected:
            span = await self._start_span(
                SpanType.AGENT,
                "progress.detect",
                parent_span_id=parent_span_id,
                attributes={
                    **attributes,
                    "progress.repetition_count": decision.repetition_count,
                },
            )
            await self._finish_span(
                span,
                SpanStatus.FAILED
                if decision.decision == ProgressDecisionType.TERMINATE
                else SpanStatus.INTERRUPTED,
            )
            await self._emit(
                "progress.detected",
                {
                    "run_id": state.run_id,
                    "step": observation.step,
                    "decision": decision.decision.value,
                    **attributes,
                },
            )
        if decision.decision == ProgressDecisionType.RECOVER:
            await self._emit(
                "progress.recovery",
                {
                    "run_id": state.run_id,
                    "step": observation.step,
                    "recovery_attempts": progress_state.recovery_attempts,
                    "detector_type": progress_state.detector_type,
                },
            )
        elif decision.decision == ProgressDecisionType.TERMINATE:
            await self._fail(
                state,
                NoProgressError(
                    run_id=state.run_id,
                    step=observation.step,
                    state=progress_state,
                ),
                step="progress",
            )
        return decision

    async def _wait_for_approval(
        self,
        state: Checkpoint,
        *,
        invocation_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        kind: str,
        reason: str,
        parent_span_id: str | None = None,
    ) -> Checkpoint:
        state.status = RunStatus.WAITING_APPROVAL
        state.interrupt = Interrupt(
            kind=kind,
            reason=reason,
            invocation_id=invocation_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        await self._save_checkpoint(
            state,
            operation="interrupt",
            parent_span_id=parent_span_id,
        )
        await self._record_interrupt(
            state,
            kind=kind,
            reason=reason,
            parent_span_id=parent_span_id,
        )
        await self._update_run_trace(state.status)
        await self._emit(
            "interrupt.created",
            {
                "run_id": state.run_id,
                "kind": kind,
                "reason": reason,
                "invocation_id": invocation_id,
            },
        )
        await self._emit(
            "run.interrupted",
            {
                "run_id": state.run_id,
                "status": state.status.value,
                "interrupt": state.interrupt.to_dict(),
            },
        )
        return state

    async def _fail(self, state: Checkpoint, exc: Exception, *, step: str) -> Checkpoint:
        state.status = RunStatus.FAILED
        state.error = RunError(
            type=exc.code
            if isinstance(exc, (BudgetExceededError, NoProgressError))
            else type(exc).__name__,
            message=_safe_error(exc),
            step=step,
            metadata=exc.metadata()
            if isinstance(exc, (BudgetExceededError, NoProgressError))
            else {},
        )
        if isinstance(exc, BudgetExceededError):
            await self.budget_manager.record_hard_limit(state, exc)
        if isinstance(exc, BudgetExceededError) and self.tracer is not None:
            await self.tracer.annotate_span(
                self.tracer.root_span_id,
                **{
                    "budget.hard_limit_reached": True,
                    "budget.exceeded_dimension": exc.dimension,
                    "budget.exceeded_limit": str(exc.limit),
                    "budget.exceeded_used": str(exc.used),
                },
            )
        await self._save_checkpoint(state, operation="run.failed")
        await self._finish_run_trace(state.status)
        await self._emit(
            "run.failed",
            {"run_id": state.run_id, "error": state.error.to_dict()},
        )
        return state

    async def _refresh(self, state: Checkpoint) -> Checkpoint:
        if self.ownership is not None:
            await self.store.validate_ownership(self.ownership)
        current = await self._require(state.run_id)
        if current.sequence == state.sequence:
            return state
        if current.status != RunStatus.RUNNING:
            return current
        raise RuntimeError(f"run {state.run_id} was concurrently advanced")

    async def _require(self, run_id: str) -> Checkpoint:
        state = await load_run_state(self.store, run_id)
        if state is None:
            raise ValueError(f"run not found: {run_id}")
        return state

    async def _permission_decision(
        self,
        state: Checkpoint,
        *,
        tool: Tool | None,
        arguments: dict[str, Any],
        invocation_id: str,
        parent_span_id: str | None,
    ) -> PermissionDecision:
        if tool is None:
            return PermissionDecision(
                PermissionAction.ALLOW,
                "unknown tool will be rejected by the executor",
                "tool.not_found",
            )
        request = tool.permission_request(
            arguments,
            ToolContext(
                cwd=self.cwd,
                config=self.config,
                invocation_id=invocation_id,
                run_id=state.run_id,
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                workspace=self.cwd,
            ),
            invocation_id=invocation_id,
        )
        policy_decision = await self.permission_policy.evaluate(request)
        approved = state.decisions.get(invocation_id)
        if approved == "reject":
            decision = PermissionDecision(
                PermissionAction.DENY,
                "this invocation was explicitly rejected",
                "invocation.rejected",
            )
        elif approved == "approve" and policy_decision.action != PermissionAction.DENY:
            decision = PermissionDecision(
                PermissionAction.ALLOW,
                "this invocation was explicitly approved",
                "invocation.approved",
            )
        else:
            decision = policy_decision
        payload = {
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "turn_id": state.turn_id,
            "invocation_id": invocation_id,
            "tool_name": tool.name,
            "capabilities": list(request.capabilities),
            "decision": decision.action.value,
            "reason": decision.reason,
            "matched_rule": decision.matched_rule,
        }
        await self._emit("policy.decision", payload)
        span_status = {
            PermissionAction.ALLOW: SpanStatus.SUCCEEDED,
            PermissionAction.DENY: SpanStatus.FAILED,
            PermissionAction.REQUIRE_APPROVAL: SpanStatus.INTERRUPTED,
        }[decision.action]
        span = await self._start_span(
            SpanType.POLICY,
            f"policy.{tool.name}",
            attributes=payload,
            parent_span_id=parent_span_id,
        )
        await self._finish_span(span, span_status)
        return decision

    def _may_require_approval(self, tool: Tool | None) -> bool:
        if tool is None or self.config.policy.hitl_mode == "never":
            return False
        if self.config.policy.hitl_mode == "always" or tool.requires_approval:
            return True
        sensitive = {
            Capability.FILESYSTEM_WRITE.value,
            Capability.SHELL_EXECUTE.value,
            Capability.NETWORK_WRITE.value,
            Capability.EXTERNAL_SIDE_EFFECT.value,
        }
        return bool(set(tool.capabilities) & sensitive)

    def _execution_start_attributes(
        self,
        tool: Tool | None,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        if tool is None or Capability.SHELL_EXECUTE.value not in tool.capabilities:
            return {}
        return {
            "execution_backend": self.execution_backend.name,
            "workspace": self.cwd,
            "timeout_seconds": float(arguments.get("timeout") or self.config.tools.timeout),
        }

    @staticmethod
    def _dependency_error_code(exc: Exception, decision: RetryDecision) -> str:
        if isinstance(exc, (ContextBudgetExceededError, BudgetExceededError)):
            return exc.code
        if decision.blocked_by_deadline:
            return DEPENDENCY_DEADLINE_EXCEEDED
        if decision.exhausted:
            return DEPENDENCY_RETRY_EXHAUSTED
        if decision.category == DependencyFailureCategory.TIMEOUT:
            return DEPENDENCY_TIMEOUT
        return type(exc).__name__

    @staticmethod
    def _retry_metadata(
        dependency_type: str,
        attempt: int,
        decision: RetryDecision,
    ) -> dict[str, Any]:
        return {
            "dependency_type": dependency_type,
            "failure_category": decision.category.value,
            "attempt_count": attempt,
            "backoff_ms": round(decision.delay_seconds * 1000, 3),
            "retryable": decision.retry,
            "retry_exhausted": decision.exhausted,
            "retry_blocked_by_deadline": decision.blocked_by_deadline,
            "unsafe_retry_suppressed": decision.unsafe_suppressed,
        }

    @staticmethod
    def _retry_span_attributes(
        attempt: int,
        decision: RetryDecision,
    ) -> dict[str, Any]:
        return {
            "dependency.retry_attempt": attempt,
            "dependency.retry_count": max(0, attempt - 1),
            "dependency.failure_category": decision.category.value,
            "dependency.backoff_ms": round(decision.delay_seconds * 1000, 3),
            "dependency.retryable": decision.retry,
            "dependency.retry_exhausted": decision.exhausted,
            "dependency.retry_blocked_by_deadline": decision.blocked_by_deadline,
            "dependency.unsafe_retry_suppressed": decision.unsafe_suppressed,
        }

    @staticmethod
    def _tool_retry_safety(tool: Tool | None) -> RetrySafety:
        if tool is None:
            return RetrySafety.UNSAFE
        if tool.retry_safety is not None:
            return RetrySafety(str(tool.retry_safety))
        if tool.is_read_only:
            return RetrySafety.SAFE
        key_name = tool.idempotency_key_parameter
        if key_name:
            return RetrySafety.IDEMPOTENT
        return RetrySafety.UNSAFE

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    async def _save_checkpoint(
        self,
        state: Checkpoint,
        *,
        operation: str,
        parent_span_id: str | None = None,
    ) -> None:
        if self.tracer is None:
            await advance_run_state(self.store, state, ownership=self.ownership)
            return
        span = await self.tracer.start_span(
            SpanType.CHECKPOINT,
            "checkpoint.save",
            parent_span_id=parent_span_id,
            attributes={
                "operation": operation,
                "sequence_before": state.sequence,
                "run_status": state.status.value,
            },
        )
        try:
            await advance_run_state(self.store, state, ownership=self.ownership)
        except Exception as exc:
            await self.tracer.finish_span(
                span,
                SpanStatus.FAILED,
                attributes={"error": _safe_error(exc)},
            )
            raise
        await self.tracer.finish_span(
            span,
            SpanStatus.SUCCEEDED,
            attributes={"sequence": state.sequence},
        )

    async def _save_tool_execution(self, record: ToolExecutionRecord) -> None:
        if self.ownership is None:
            await self.store.save_tool_execution(record)
        else:
            await self.store.save_tool_execution(record, ownership=self.ownership)

    async def _start_span(
        self,
        span_type: SpanType,
        name: str,
        *,
        attributes: dict[str, object] | None = None,
        parent_span_id: str | None = None,
        span_id: str | None = None,
        reopen: bool = False,
    ) -> Span | None:
        if self.tracer is None:
            return None
        return await self.tracer.start_span(
            span_type,
            name,
            attributes=attributes,
            parent_span_id=parent_span_id,
            span_id=span_id,
            reopen=reopen,
        )

    async def _finish_span(
        self,
        span: Span | None,
        status: SpanStatus,
        *,
        attributes: dict[str, object] | None = None,
    ) -> None:
        if self.tracer is None or span is None:
            return
        await self.tracer.finish_span(span, status, attributes=attributes)

    async def _start_tool_span(
        self,
        invocation_id: str,
        tool_name: str,
        *,
        parent_span_id: str | None,
        attributes: dict[str, object] | None = None,
        reopen: bool = False,
    ) -> Span | None:
        if self.tracer is None:
            return None
        return await self.tracer.start_span(
            SpanType.TOOL,
            f"tool.{tool_name}",
            span_id=tool_span_id(invocation_id),
            parent_span_id=parent_span_id,
            reopen=reopen,
            attributes={
                "tool_name": tool_name,
                "invocation_id": invocation_id,
                **dict(attributes or {}),
            },
        )

    async def _record_interrupt(
        self,
        state: Checkpoint,
        *,
        kind: str,
        reason: str,
        parent_span_id: str | None = None,
    ) -> None:
        span = await self._start_span(
            SpanType.INTERRUPT,
            "interrupt",
            parent_span_id=parent_span_id,
            attributes={
                "kind": kind,
                "reason": reason,
                "invocation_id": state.interrupt.invocation_id if state.interrupt else None,
                "tool_name": state.interrupt.tool_name if state.interrupt else None,
            },
        )
        await self._finish_span(span, SpanStatus.INTERRUPTED)

    async def _update_run_trace(self, status: RunStatus) -> None:
        if self.tracer is not None:
            await self.tracer.update_run(status)

    async def _finish_run_trace(self, status: RunStatus) -> None:
        if self.tracer is not None:
            await self.tracer.update_run(status, terminal=True)

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event_type, payload)
        if inspect.isawaitable(result):
            await result


def _span_id(span: Span | None) -> str | None:
    return span.span_id if span is not None else None


def _merge_tool_delta(
    states: dict[int, dict[str, Any]], delta: dict[str, Any], agent_turn: int
) -> None:
    index = int(delta.get("index") or 0)
    state = states.setdefault(
        index,
        {
            "id": delta.get("id") or f"call_{agent_turn + 1}_{index}",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if delta.get("id"):
        state["id"] = str(delta["id"])
    function = delta.get("function") if isinstance(delta.get("function"), dict) else {}
    if function.get("name"):
        state["function"]["name"] = str(function["name"])
    if function.get("arguments"):
        state["function"]["arguments"] += str(function["arguments"])


def _finalize_tool_calls(states: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [states[index] for index in sorted(states) if states[index]["function"]["name"]]


def _scope_tool_call_ids(
    calls: list[dict[str, Any]],
    scope: str | None,
) -> list[dict[str, Any]]:
    if scope is None:
        return calls
    scoped: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        item = dict(call)
        original = str(item.get("id") or f"call_{index}")
        item["id"] = f"{scope}:{original}"
        scoped.append(item)
    return scoped


def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(function.get("name") or call.get("name") or "")


def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = function.get("arguments", call.get("arguments", {}))
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {"raw": raw}
        return decoded if isinstance(decoded, dict) else {"value": decoded}
    return raw if isinstance(raw, dict) else {}


def _call_with_payload(
    call: dict[str, Any],
    payload: dict[str, Any],
    invocation_id: str,
    tool: Tool | None,
) -> dict[str, Any]:
    data = dict(payload)
    if tool and tool.idempotency_key_parameter:
        data.setdefault(tool.idempotency_key_parameter, invocation_id)
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {"name": _tool_call_name(call), "arguments": json.dumps(data)},
    }


def _arguments_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_decision(decision: str | None) -> str | None:
    if decision is None:
        return None
    normalized = decision.lower().strip()
    if normalized in {"approve", "approved", "allow", "retry"}:
        return "approve"
    if normalized in {"reject", "rejected", "deny", "denied", "skip"}:
        return "reject"
    return None


def _safe_error(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    return text[:4000]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _after_seconds(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=max(0.0, seconds))).isoformat()


def _seconds_until(value: str) -> float:
    try:
        target = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0.0, (target - datetime.now(UTC)).total_seconds())
