from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from axiom.config import AxiomConfig
from axiom.llm.base import LlmClient
from axiom.runtime.checkpoints import RuntimeStore
from axiom.runtime.models import (
    Checkpoint,
    Interrupt,
    RunError,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from axiom.runtime.observability import Span, SpanStatus, SpanType, now, tool_span_id
from axiom.runtime.observability_store import RunTracer
from axiom.tools.base import Tool, ToolContext, ToolResult
from axiom.tools.executor import ToolExecutor
from axiom.tools.registry import ToolRegistry
from axiom.types import Message

EventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]
RetryableError = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    backoff_seconds: float = 0.0
    retryable_error: RetryableError | None = None

    def can_retry(self, error: str, attempt: int) -> bool:
        if attempt >= max(1, self.max_attempts):
            return False
        return self.retryable_error(error) if self.retryable_error else True


@dataclass(frozen=True, slots=True)
class _LlmCallResult:
    text: str
    tool_calls: list[dict[str, Any]]
    stop_reason: str
    prompt_tokens: int
    completion_tokens: int
    first_token_at: str | None
    ttft_ms: float | None


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
        max_turns: int = 20,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.system_prompt = system_prompt
        self.cwd = cwd
        self.config = config
        self.store = store
        self.retry_policy = retry_policy or RetryPolicy()
        self.event_sink = event_sink
        self.tracer = tracer
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
    ) -> Checkpoint:
        state = Checkpoint.create(
            thread_id=thread_id,
            input=input,
            history=history,
            run_id=run_id,
            turn_id=turn_id,
        )
        async with self._run_lock(state.run_id):
            if await self.store.load(state.run_id) is not None:
                raise ValueError(f"run already exists: {state.run_id}")
            if self.tracer is not None:
                await self.tracer.start_run(state)
            await self._save_checkpoint(state, operation="run.start")
            await self._emit(
                "run.started",
                {"run_id": state.run_id, "turn_id": state.turn_id, "status": state.status.value},
            )
            return await self._advance(state)

    async def resume(self, run_id: str, *, decision: str | None = None) -> Checkpoint:
        async with self._run_lock(run_id):
            state = await self._require(run_id)
            if state.status == RunStatus.CANCELLED:
                raise ValueError("cancelled run cannot be resumed")
            if state.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise ValueError(f"{state.status.value.lower()} run cannot be resumed")

            was_recovery = state.status == RunStatus.RUNNING
            if self.tracer is not None:
                await self.tracer.start_run(state, recovered=was_recovery)
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
            await self._save_checkpoint(state, operation="cancel")
            await self._finish_run_trace(state.status)
            await self._emit("run.cancelled", {"run_id": run_id})
            return state

    async def _advance(self, state: Checkpoint) -> Checkpoint:
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

    async def _execute_llm_step(self, state: Checkpoint) -> Checkpoint:
        attempt = 0
        while True:
            attempt += 1
            step_index = state.step_index
            step_span = await self._start_span(
                SpanType.AGENT,
                "agent.step",
                attributes={"step_index": step_index, "kind": "llm", "attempt": attempt},
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
            try:
                llm_result = await self._collect_llm_response(state, call_started=call_started)
            except Exception as exc:
                message = _safe_error(exc)
                latency_ms = round((time.perf_counter() - call_started) * 1000, 3)
                await self._finish_span(
                    llm_span,
                    SpanStatus.FAILED,
                    attributes={
                        "latency_ms": latency_ms,
                        "error": message,
                        "retry_count": attempt - 1,
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
                    },
                )
                state.error = RunError(type=type(exc).__name__, message=message, step="llm")
                await self._save_checkpoint(
                    state,
                    operation="llm.failed",
                    parent_span_id=_span_id(step_span),
                )
                await self._finish_span(
                    step_span,
                    SpanStatus.FAILED,
                    attributes={"error": message},
                )
                await self._emit(
                    "step.failed",
                    {
                        "run_id": state.run_id,
                        "step_index": state.step_index,
                        "kind": "llm",
                        "attempt": attempt,
                        "error": message,
                    },
                )
                await self._emit(
                    "agent.step.failed",
                    {"run_id": state.run_id, "step_index": step_index, "error": message},
                )
                if not self.retry_policy.can_retry(message, attempt):
                    state.status = RunStatus.FAILED
                    await self._save_checkpoint(state, operation="run.failed")
                    await self._finish_run_trace(state.status)
                    await self._emit("run.failed", {"run_id": state.run_id, "error": message})
                    return state
                if self.retry_policy.backoff_seconds > 0:
                    await asyncio.sleep(self.retry_policy.backoff_seconds)
                continue

            latency_ms = round((time.perf_counter() - call_started) * 1000, 3)
            await self._finish_span(
                llm_span,
                SpanStatus.SUCCEEDED,
                attributes={
                    "prompt_tokens": llm_result.prompt_tokens,
                    "completion_tokens": llm_result.completion_tokens,
                    "total_tokens": llm_result.prompt_tokens + llm_result.completion_tokens,
                    "first_token_at": llm_result.first_token_at,
                    "ttft_ms": llm_result.ttft_ms,
                    "latency_ms": latency_ms,
                    "finish_reason": llm_result.stop_reason,
                    "retry_count": attempt - 1,
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
            state.messages.append(
                Message(
                    role="assistant",
                    content=llm_result.text,
                    tool_calls=llm_result.tool_calls,
                )
            )
            state.output_text += llm_result.text
            state.pending_tool_calls = llm_result.tool_calls
            state.next_tool_index = 0
            state.agent_turn += 1
            state.step_index += 1
            state.total_tokens += llm_result.prompt_tokens + llm_result.completion_tokens
            if not llm_result.tool_calls and llm_result.stop_reason != "tool_use":
                state.status = RunStatus.COMPLETED
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
                    "tool_calls": len(llm_result.tool_calls),
                },
            )
            await self._emit(
                "step.completed",
                {
                    "run_id": state.run_id,
                    "step_index": state.step_index - 1,
                    "kind": "llm",
                    "stop_reason": llm_result.stop_reason,
                    "tool_calls": len(llm_result.tool_calls),
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
            return state

    async def _collect_llm_response(
        self,
        state: Checkpoint,
        *,
        call_started: float,
    ) -> _LlmCallResult:
        text = ""
        stop_reason = "end_turn"
        prompt_tokens = 0
        completion_tokens = 0
        first_token_at: str | None = None
        ttft_ms: float | None = None
        tool_states: dict[int, dict[str, Any]] = {}
        async for event in self.llm_client.chat(
            state.messages,
            self.tool_registry.definitions(),
            system_prompt=self.system_prompt,
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
            elif event_type == "error":
                error = event.get("error")
                raise RuntimeError(str(error or "LLM stream failed"))
        return _LlmCallResult(
            text=text,
            tool_calls=_finalize_tool_calls(tool_states),
            stop_reason=stop_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            first_token_at=first_token_at,
            ttft_ms=ttft_ms,
        )

    async def _execute_pending_tool(self, state: Checkpoint) -> Checkpoint:
        step_index = state.step_index
        step_span = await self._start_span(
            SpanType.AGENT,
            "agent.step",
            attributes={"step_index": step_index, "kind": "tool"},
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
        decision = state.decisions.get(invocation_id)
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
                    "approval_required": self._requires_approval(tool),
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

        if existing and existing.status == ToolExecutionStatus.RUNNING:
            retry_is_safe = bool(tool and (tool.is_read_only or tool.idempotency_key_parameter))
            if not retry_is_safe and decision != "approve":
                tool_span = await self._start_tool_span(
                    invocation_id,
                    name,
                    parent_span_id=parent_span_id,
                    attributes={
                        "approval_required": self._requires_approval(tool),
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

        if self._requires_approval(tool) and decision is None:
            return await self._wait_for_approval(
                state,
                invocation_id=invocation_id,
                tool_name=name,
                arguments=payload,
                kind="tool_approval",
                reason=f'Tool "{name}" requires approval.',
                parent_span_id=parent_span_id,
            )

        if decision == "reject":
            record = existing or ToolExecutionRecord(
                invocation_id=invocation_id,
                run_id=state.run_id,
                tool_call_id=tool_call_id,
                tool_name=name,
                arguments_hash=arguments_hash,
                status=ToolExecutionStatus.FAILED,
            )
            record.status = ToolExecutionStatus.FAILED
            record.error = "rejected by approval policy"
            record.is_error = True
            record.completed_at = _now()
            await self.store.save_tool_execution(record)
            tool_span = await self._start_tool_span(
                invocation_id,
                name,
                parent_span_id=parent_span_id,
                attributes={
                    "approval_required": True,
                    "reused_result": False,
                    "retry_count": max(0, record.attempt - 1),
                    "rejected": True,
                },
            )
            await self._finish_span(
                tool_span,
                SpanStatus.FAILED,
                attributes={"error": record.error, "rejected": True},
            )
            result = ToolResult(
                content=f'Tool "{name}" was rejected by approval policy.',
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
            await self.store.save_tool_execution(record)

        tool_span = await self._start_tool_span(
            invocation_id,
            name,
            parent_span_id=parent_span_id,
            attributes={
                "approval_required": self._requires_approval(tool),
                "reused_result": False,
                "ambiguous_execution": bool(
                    existing and existing.status == ToolExecutionStatus.RUNNING
                ),
                "tool_call_id": tool_call_id,
            },
            reopen=bool(existing and existing.status == ToolExecutionStatus.RUNNING),
        )

        while True:
            record.attempt += 1
            record.status = ToolExecutionStatus.RUNNING
            record.started_at = record.started_at or _now()
            record.error = None
            await self.store.save_tool_execution(record)
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
                },
            )

            execution_call = _call_with_payload(call, payload, invocation_id, tool)
            context = ToolContext(
                cwd=self.cwd,
                config=self.config,
                approval_callback=lambda _request: "approve",
                invocation_id=invocation_id,
            )
            result = await ToolExecutor(self.tool_registry).execute_one(execution_call, context)
            if not result.is_error:
                record.status = ToolExecutionStatus.SUCCEEDED
                record.result = result.content
                record.is_error = False
                record.error = None
                record.completed_at = _now()
                await self.store.save_tool_execution(record)
                await self._finish_span(
                    tool_span,
                    SpanStatus.SUCCEEDED,
                    attributes={
                        "attempt": record.attempt,
                        "retry_count": max(0, record.attempt - 1),
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
            await self.store.save_tool_execution(record)
            await self._emit(
                "tool.failed",
                {
                    "run_id": state.run_id,
                    "invocation_id": invocation_id,
                    "tool_name": name,
                    "attempt": record.attempt,
                    "error": result.content,
                },
            )
            retry_is_safe = bool(
                tool and (tool.is_read_only or tool.idempotency_key_parameter is not None)
            )
            retryable = retry_is_safe and self.retry_policy.can_retry(
                result.content, record.attempt
            )
            if not retryable:
                await self._finish_span(
                    tool_span,
                    SpanStatus.FAILED,
                    attributes={
                        "attempt": record.attempt,
                        "retry_count": max(0, record.attempt - 1),
                        "error": result.content,
                    },
                )
                return await self._apply_tool_result(
                    state,
                    invocation_id,
                    name,
                    result,
                    parent_span_id=parent_span_id,
                )
            if self.retry_policy.backoff_seconds > 0:
                await asyncio.sleep(self.retry_policy.backoff_seconds)

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
        state.messages.append(
            Message(
                role="tool",
                content=result.content,
                tool_call_id=result.tool_use_id,
            )
        )
        state.next_tool_index += 1
        state.step_index += 1
        state.interrupt = None
        state.decisions.pop(invocation_id, None)
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
            },
        )
        return state

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
        state.error = RunError(type=type(exc).__name__, message=_safe_error(exc), step=step)
        await self._save_checkpoint(state, operation="run.failed")
        await self._finish_run_trace(state.status)
        await self._emit(
            "run.failed",
            {"run_id": state.run_id, "error": state.error.to_dict()},
        )
        return state

    async def _refresh(self, state: Checkpoint) -> Checkpoint:
        current = await self._require(state.run_id)
        if current.sequence == state.sequence:
            return state
        if current.status != RunStatus.RUNNING:
            return current
        raise RuntimeError(f"run {state.run_id} was concurrently advanced")

    async def _require(self, run_id: str) -> Checkpoint:
        state = await self.store.load(run_id)
        if state is None:
            raise ValueError(f"run not found: {run_id}")
        return state

    def _requires_approval(self, tool: Tool | None) -> bool:
        if tool is None or self.config.policy.hitl_mode == "never":
            return False
        return self.config.policy.hitl_mode == "always" or tool.requires_approval

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
            await self.store.save(state)
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
            await self.store.save(state)
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

    async def _start_span(
        self,
        span_type: SpanType,
        name: str,
        *,
        attributes: dict[str, object] | None = None,
        parent_span_id: str | None = None,
    ) -> Span | None:
        if self.tracer is None:
            return None
        return await self.tracer.start_span(
            span_type,
            name,
            attributes=attributes,
            parent_span_id=parent_span_id,
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
