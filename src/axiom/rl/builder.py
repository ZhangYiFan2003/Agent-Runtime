from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from axiom.runtime.models import Checkpoint, RunStatus, ToolExecutionRecord
from axiom.runtime.observability import RunMetrics, Span, SpanStatus, SpanType, TraceBundle
from axiom.types import Message

from .models import (
    AgentTrajectory,
    TerminationReason,
    TrajectoryObservation,
    TrajectoryOutcome,
    TrajectoryStep,
)

if TYPE_CHECKING:
    from axiom.evaluation.models import EvaluationRunResult


class TrajectoryBuildError(ValueError):
    """Raised when durable evidence cannot produce a trustworthy trajectory."""


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "token",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|password|secret)\s*[:=]\s*[^\s,;]+"),
)
_REDACTED = "[REDACTED]"


class TrajectoryBuilder:
    """Build an RL episode from the Runtime's existing durable evidence."""

    def build(
        self,
        *,
        checkpoint: Checkpoint,
        trace: TraceBundle,
        tool_executions: list[ToolExecutionRecord] | None = None,
        evaluation_result: EvaluationRunResult | None = None,
        provenance: dict[str, Any] | None = None,
        child_run_ids: list[str] | tuple[str, ...] = (),
    ) -> AgentTrajectory:
        if not checkpoint.finished:
            raise TrajectoryBuildError("a trajectory can only be built from a terminal Run")
        if trace.trace.run_id != checkpoint.run_id:
            raise TrajectoryBuildError("checkpoint and trace refer to different Runs")

        records = {item.tool_call_id: item for item in tool_executions or []}
        llm_spans = sorted(
            (
                span
                for span in trace.spans
                if span.span_type == SpanType.LLM and span.status == SpanStatus.SUCCEEDED
            ),
            key=lambda span: (span.started_at, span.span_id),
        )
        assistant_positions = [
            index
            for index, message in enumerate(checkpoint.messages)
            if message.role == "assistant"
        ]
        if len(llm_spans) != len(assistant_positions):
            raise TrajectoryBuildError(
                "successful LLM spans do not match persisted assistant decisions"
            )

        outcome = _outcome(checkpoint, evaluation_result)
        metrics = RunMetrics.from_trace(trace.trace, trace.spans)
        trajectory_id = f"trajectory_{checkpoint.run_id}"
        total_cost = _cost(metrics.cost_usd if metrics.cost_known else None)
        total_step_tokens = sum(
            _span_int(span, "prompt_tokens") + _span_int(span, "completion_tokens")
            for span in llm_spans
        )
        parent_steps = {
            span.span_id: span
            for span in trace.spans
            if span.span_type == SpanType.AGENT and span.name == "agent.step"
        }
        steps: list[TrajectoryStep] = []
        for ordinal, (message_index, llm_span) in enumerate(
            zip(assistant_positions, llm_spans, strict=True)
        ):
            assistant = checkpoint.messages[message_index]
            next_assistant = (
                assistant_positions[ordinal + 1]
                if ordinal + 1 < len(assistant_positions)
                else len(checkpoint.messages)
            )
            tool_messages = [
                message
                for message in checkpoint.messages[message_index + 1 : next_assistant]
                if message.role == "tool"
            ]
            tool_actions = tuple(_tool_action(call, records) for call in assistant.tool_calls)
            tool_observations = tuple(
                _tool_observation(message, records) for message in tool_messages
            )
            known_call_ids = {
                str(action.get("tool_call_id"))
                for action in tool_actions
                if action.get("tool_call_id")
            }
            unexpected = {
                str(observation.get("tool_call_id"))
                for observation in tool_observations
                if observation.get("tool_call_id") not in known_call_ids
            }
            if unexpected:
                raise TrajectoryBuildError(
                    f"tool observations have no matching action: {sorted(unexpected)}"
                )

            observation_messages = tuple(
                sanitize_export_value(_message_dict(item))
                for item in checkpoint.messages[:message_index]
            )
            parent = parent_steps.get(llm_span.parent_span_id or "")
            step_index = (
                int(parent.attributes.get("step_index") or 0) if parent is not None else ordinal
            )
            prompt_tokens = _span_int(llm_span, "prompt_tokens")
            completion_tokens = _span_int(llm_span, "completion_tokens")
            cost = _allocate_cost(
                total_cost,
                prompt_tokens + completion_tokens,
                total_step_tokens,
            )
            is_final = ordinal == len(assistant_positions) - 1
            steps.append(
                TrajectoryStep(
                    trajectory_id=trajectory_id,
                    run_id=checkpoint.run_id,
                    parent_run_id=checkpoint.parent_run_id,
                    step_index=step_index,
                    observation=TrajectoryObservation(
                        messages=observation_messages,
                        fingerprint=_fingerprint(observation_messages),
                        exact=_observation_is_exact(llm_span),
                    ),
                    action=sanitize_export_value(
                        {
                            "type": "tool_call" if assistant.tool_calls else "final_response",
                            "content": assistant.content,
                            "tool_calls": list(assistant.tool_calls),
                        }
                    ),
                    tool_actions=tool_actions,
                    tool_observations=tool_observations,
                    model=_optional_text(llm_span.attributes.get("model")),
                    model_call_id=llm_span.span_id,
                    input_token_count=prompt_tokens,
                    output_token_count=completion_tokens,
                    cost_usd=cost,
                    started_at=llm_span.started_at,
                    latency_ms=llm_span.latency_ms,
                    done=is_final,
                    termination_reason=outcome.termination_reason if is_final else None,
                )
            )

        safe_provenance = sanitize_export_value(
            {
                **(provenance or {}),
                "runtime_evidence": {
                    "checkpoint_schema_version": checkpoint.schema_version,
                    "trace_schema_version": trace.trace.schema_version,
                    "observation_policy": "checkpoint message projection",
                    "objective_fingerprint": _fingerprint(checkpoint.input),
                },
            }
        )
        trajectory = AgentTrajectory(
            trajectory_id=trajectory_id,
            run_id=checkpoint.run_id,
            trace_id=trace.trace.trace_id,
            thread_id=checkpoint.thread_id,
            turn_id=checkpoint.turn_id,
            parent_run_id=checkpoint.parent_run_id,
            parent_step_id=checkpoint.parent_step_id,
            child_run_ids=tuple(sorted(set(child_run_ids))),
            run_kind=checkpoint.run_kind,
            execution_strategy=checkpoint.execution_strategy,
            steps=tuple(steps),
            outcome=outcome,
            metrics=sanitize_export_value(metrics.to_dict()),
            provenance=safe_provenance,
        )
        trajectory.validate()
        validate_export_payload(trajectory.to_dict())
        return trajectory


def with_reward(trajectory: AgentTrajectory, reward: Any) -> AgentTrajectory:
    steps = trajectory.steps
    if steps:
        steps = (*steps[:-1], replace(steps[-1], reward_components=dict(reward.components)))
    result = replace(trajectory, steps=steps, reward=reward)
    result.validate()
    return result


def sanitize_export_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.casefold() in _SENSITIVE_KEYS:
        return _REDACTED
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        sanitized = value
        for pattern in _SECRET_PATTERNS:
            sanitized = pattern.sub(_REDACTED, sanitized)
        return sanitized
    if isinstance(value, dict):
        return {
            str(item_key): sanitize_export_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_export_value(item) for item in value]
    return sanitize_export_value(str(value))


def validate_export_payload(value: Any, *, key: str | None = None) -> None:
    if key is not None and key.casefold() in _SENSITIVE_KEYS and value != _REDACTED:
        raise TrajectoryBuildError(f"sensitive export field was not redacted: {key}")
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise TrajectoryBuildError("trajectory contains a secret-like value")
        return
    if isinstance(value, dict):
        for item_key, item in value.items():
            validate_export_payload(item, key=str(item_key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_export_payload(item)


def _outcome(
    checkpoint: Checkpoint,
    evaluation: EvaluationRunResult | None,
) -> TrajectoryOutcome:
    verification = checkpoint.completion_verification
    verified = (
        verification.get("verified")
        if isinstance(verification.get("verified"), bool)
        else None
    )
    verification_status = str(verification.get("status") or "NOT_APPLICABLE")
    reason = _termination_reason(checkpoint, verified)
    evaluation_passed = evaluation.passed if evaluation is not None else None
    if checkpoint.status != RunStatus.COMPLETED:
        success = False
    elif evaluation_passed is not None:
        success = evaluation_passed
    elif checkpoint.completion_contract:
        success = verified is True
    else:
        success = True
    return TrajectoryOutcome(
        status=checkpoint.status.value,
        done=True,
        success=success,
        termination_reason=reason,
        completion_verified=verified,
        verification_status=verification_status,
        evaluation_passed=evaluation_passed,
        error_type=checkpoint.error.type if checkpoint.error else None,
    )


def _termination_reason(checkpoint: Checkpoint, verified: bool | None) -> TerminationReason:
    if checkpoint.status == RunStatus.COMPLETED:
        return (
            TerminationReason.VERIFIED_COMPLETION
            if verified is True
            else TerminationReason.UNVERIFIED_COMPLETION
        )
    if checkpoint.status == RunStatus.CANCELLED:
        return TerminationReason.CANCELLED
    error_type = (checkpoint.error.type if checkpoint.error else "").upper()
    if "NO_PROGRESS" in error_type:
        return TerminationReason.NO_PROGRESS
    if "DEADLINE" in error_type:
        return TerminationReason.DEADLINE_EXCEEDED
    if "BUDGET" in error_type:
        return TerminationReason.BUDGET_EXCEEDED
    if "DEPENDENCY" in error_type or "RETRY_EXHAUSTED" in error_type:
        return TerminationReason.DEPENDENCY_FAILURE
    return TerminationReason.FAILED


def _tool_action(
    call: dict[str, Any],
    records: dict[str, ToolExecutionRecord],
) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    call_id = str(call.get("id") or "")
    raw_arguments = function.get("arguments", {})
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except json.JSONDecodeError:
        arguments = raw_arguments
    record = records.get(call_id)
    return sanitize_export_value(
        {
            "tool_call_id": call_id,
            "tool_name": str(function.get("name") or ""),
            "arguments": arguments,
            "arguments_fingerprint": (
                f"sha256:{record.arguments_hash}" if record else _fingerprint(arguments)
            ),
            "execution_status": record.status.value if record else "UNKNOWN",
            "attempts": record.attempt if record else 0,
        }
    )


def _tool_observation(
    message: Message,
    records: dict[str, ToolExecutionRecord],
) -> dict[str, Any]:
    call_id = str(message.tool_call_id or "")
    record = records.get(call_id)
    return sanitize_export_value(
        {
            "tool_call_id": call_id,
            "tool_name": message.name,
            "content": message.content,
            "is_error": (
                record.is_error if record else bool(message.name and "(error)" in message.name)
            ),
            "execution_status": record.status.value if record else "UNKNOWN",
            "attempts": record.attempt if record else 0,
        }
    )


def _message_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": list(message.tool_calls),
    }


def _observation_is_exact(span: Span) -> bool:
    if not {
        "context.compaction_triggered",
        "context.tool_results_projected",
    } <= span.attributes.keys():
        return False
    return not bool(span.attributes.get("context.compaction_triggered")) and not bool(
        int(span.attributes.get("context.tool_results_projected") or 0)
    )


def _span_int(span: Span, key: str) -> int:
    value = span.attributes.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


def _cost(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _allocate_cost(total: Decimal | None, tokens: int, all_tokens: int) -> str | None:
    if total is None or all_tokens <= 0:
        return None
    return format(total * Decimal(tokens) / Decimal(all_tokens), "f")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None
