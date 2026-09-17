from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from axiom.runtime.models import Checkpoint, ToolExecutionRecord, ToolExecutionStatus
from axiom.runtime.observability import Span, SpanType, TraceBundle


@dataclass(frozen=True, slots=True)
class StepView:
    """Regenerable evaluation view anchored to an observed ``agent.step`` span."""

    run_id: str
    step_index: int
    span_id: str
    kind: str
    model_calls: int
    logical_tool_calls: int
    physical_tool_attempts: int
    input_tokens: int
    output_tokens: int
    latency_ms: float | None
    ttft_ms: float | None
    tool_invocation_ids: tuple[str, ...] = ()
    tool_outcomes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step_index": self.step_index,
            "span_id": self.span_id,
            "kind": self.kind,
            "model_calls": self.model_calls,
            "logical_tool_calls": self.logical_tool_calls,
            "physical_tool_attempts": self.physical_tool_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "ttft_ms": self.ttft_ms,
            "tool_invocation_ids": list(self.tool_invocation_ids),
            "tool_outcomes": list(self.tool_outcomes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepView:
        return cls(
            run_id=str(data.get("run_id") or ""),
            step_index=int(data.get("step_index") or 0),
            span_id=str(data.get("span_id") or ""),
            kind=str(data.get("kind") or "unknown"),
            model_calls=int(data.get("model_calls") or 0),
            logical_tool_calls=int(data.get("logical_tool_calls") or 0),
            physical_tool_attempts=int(data.get("physical_tool_attempts") or 0),
            input_tokens=int(data.get("input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            latency_ms=_optional_float(data.get("latency_ms")),
            ttft_ms=_optional_float(data.get("ttft_ms")),
            tool_invocation_ids=tuple(str(item) for item in data.get("tool_invocation_ids", [])),
            tool_outcomes=tuple(str(item) for item in data.get("tool_outcomes", [])),
        )


@dataclass(frozen=True, slots=True)
class RunEvaluationView:
    run_id: str = ""
    steps: tuple[StepView, ...] = ()
    model_calls: int = 0
    logical_tool_calls: int = 0
    attempted_logical_tool_calls: int = 0
    physical_tool_attempts: int = 0
    retried_logical_tool_calls: int = 0
    timed_out_logical_tool_calls: int = 0
    tool_successes: int = 0
    tool_failures: int = 0
    tool_unknowns: int = 0
    tool_success_rate: float | None = None
    tool_retry_rate: float | None = None
    tool_timeout_rate: float | None = None
    mean_ttft_ms: float | None = None
    mean_step_latency_ms: float | None = None
    completion_status: str = "NOT_APPLICABLE"
    completion_verified: bool | None = None
    terminal_no_progress: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "steps": [step.to_dict() for step in self.steps],
            "model_calls": self.model_calls,
            "logical_tool_calls": self.logical_tool_calls,
            "attempted_logical_tool_calls": self.attempted_logical_tool_calls,
            "physical_tool_attempts": self.physical_tool_attempts,
            "retried_logical_tool_calls": self.retried_logical_tool_calls,
            "timed_out_logical_tool_calls": self.timed_out_logical_tool_calls,
            "tool_successes": self.tool_successes,
            "tool_failures": self.tool_failures,
            "tool_unknowns": self.tool_unknowns,
            "tool_success_rate": self.tool_success_rate,
            "tool_retry_rate": self.tool_retry_rate,
            "tool_timeout_rate": self.tool_timeout_rate,
            "mean_ttft_ms": self.mean_ttft_ms,
            "mean_step_latency_ms": self.mean_step_latency_ms,
            "completion_status": self.completion_status,
            "completion_verified": self.completion_verified,
            "terminal_no_progress": self.terminal_no_progress,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunEvaluationView:
        return cls(
            run_id=str(data.get("run_id") or ""),
            steps=tuple(
                StepView.from_dict(item) for item in data.get("steps", []) if isinstance(item, dict)
            ),
            model_calls=int(data.get("model_calls") or 0),
            logical_tool_calls=int(data.get("logical_tool_calls") or 0),
            attempted_logical_tool_calls=int(data.get("attempted_logical_tool_calls") or 0),
            physical_tool_attempts=int(data.get("physical_tool_attempts") or 0),
            retried_logical_tool_calls=int(data.get("retried_logical_tool_calls") or 0),
            timed_out_logical_tool_calls=int(data.get("timed_out_logical_tool_calls") or 0),
            tool_successes=int(data.get("tool_successes") or 0),
            tool_failures=int(data.get("tool_failures") or 0),
            tool_unknowns=int(data.get("tool_unknowns") or 0),
            tool_success_rate=_optional_float(data.get("tool_success_rate")),
            tool_retry_rate=_optional_float(data.get("tool_retry_rate")),
            tool_timeout_rate=_optional_float(data.get("tool_timeout_rate")),
            mean_ttft_ms=_optional_float(data.get("mean_ttft_ms")),
            mean_step_latency_ms=_optional_float(data.get("mean_step_latency_ms")),
            completion_status=str(data.get("completion_status") or "NOT_APPLICABLE"),
            completion_verified=(
                data.get("completion_verified")
                if isinstance(data.get("completion_verified"), bool)
                else None
            ),
            terminal_no_progress=bool(data.get("terminal_no_progress")),
        )


@dataclass(frozen=True, slots=True)
class EvaluationQualitySummary:
    logical_tool_calls: int = 0
    physical_tool_attempts: int = 0
    tool_success_rate: float | None = None
    tool_retry_rate: float | None = None
    tool_timeout_rate: float | None = None
    tool_unknown_count: int = 0
    completion_applicable_trials: int = 0
    completion_verified_trials: int = 0
    completion_verified_rate: float | None = None
    no_progress_trials: int = 0
    no_progress_rate: float | None = None
    mean_ttft_ms: float | None = None
    mean_step_latency_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationQualitySummary:
        return cls(
            logical_tool_calls=int(data.get("logical_tool_calls") or 0),
            physical_tool_attempts=int(data.get("physical_tool_attempts") or 0),
            tool_success_rate=_optional_float(data.get("tool_success_rate")),
            tool_retry_rate=_optional_float(data.get("tool_retry_rate")),
            tool_timeout_rate=_optional_float(data.get("tool_timeout_rate")),
            tool_unknown_count=int(data.get("tool_unknown_count") or 0),
            completion_applicable_trials=int(data.get("completion_applicable_trials") or 0),
            completion_verified_trials=int(data.get("completion_verified_trials") or 0),
            completion_verified_rate=_optional_float(data.get("completion_verified_rate")),
            no_progress_trials=int(data.get("no_progress_trials") or 0),
            no_progress_rate=_optional_float(data.get("no_progress_rate")),
            mean_ttft_ms=_optional_float(data.get("mean_ttft_ms")),
            mean_step_latency_ms=_optional_float(data.get("mean_step_latency_ms")),
        )


def project_run_evidence(
    state: Checkpoint,
    bundles: Iterable[TraceBundle],
    tool_executions: Iterable[ToolExecutionRecord],
) -> RunEvaluationView:
    bundles = tuple(bundles)
    records = tuple(sorted(tool_executions, key=lambda item: item.invocation_id))
    by_invocation = {record.invocation_id: record for record in records}
    steps = tuple(
        sorted(
            (view for bundle in bundles for view in _project_steps(bundle, by_invocation)),
            key=lambda item: (item.run_id, item.step_index, item.span_id),
        )
    )
    attempted = [record for record in records if record.attempt > 0]
    successes = sum(record.status == ToolExecutionStatus.SUCCEEDED for record in records)
    failures = sum(record.status == ToolExecutionStatus.FAILED for record in records)
    unknowns = sum(record.status == ToolExecutionStatus.UNKNOWN for record in records)
    decided = successes + failures
    ttfts = [value for bundle in bundles for value in _ttfts(bundle.spans)]
    latencies = [step.latency_ms for step in steps if step.latency_ms is not None]
    verification = state.completion_verification
    verified = (
        verification.get("verified") if isinstance(verification.get("verified"), bool) else None
    )
    return RunEvaluationView(
        run_id=state.run_id,
        steps=steps,
        model_calls=sum(
            span.span_type == SpanType.LLM for bundle in bundles for span in bundle.spans
        ),
        logical_tool_calls=len(records),
        attempted_logical_tool_calls=len(attempted),
        physical_tool_attempts=sum(record.attempt for record in records),
        retried_logical_tool_calls=sum(record.attempt > 1 for record in attempted),
        timed_out_logical_tool_calls=sum(
            record.last_failure_category == "timeout" for record in attempted
        ),
        tool_successes=successes,
        tool_failures=failures,
        tool_unknowns=unknowns,
        tool_success_rate=_rate(successes, decided),
        tool_retry_rate=_rate(sum(record.attempt > 1 for record in attempted), len(attempted)),
        tool_timeout_rate=_rate(
            sum(record.last_failure_category == "timeout" for record in attempted),
            len(attempted),
        ),
        mean_ttft_ms=_mean(ttfts),
        mean_step_latency_ms=_mean(latencies),
        completion_status=str(verification.get("status") or "NOT_APPLICABLE"),
        completion_verified=verified,
        terminal_no_progress=bool(state.error and state.error.type == "NO_PROGRESS"),
    )


def aggregate_quality(views: Iterable[RunEvaluationView]) -> EvaluationQualitySummary:
    views = tuple(views)
    successes = sum(view.tool_successes for view in views)
    failures = sum(view.tool_failures for view in views)
    attempted = sum(view.attempted_logical_tool_calls for view in views)
    retried = sum(view.retried_logical_tool_calls for view in views)
    timed_out = sum(view.timed_out_logical_tool_calls for view in views)
    applicable = [view for view in views if view.completion_status != "NOT_APPLICABLE"]
    verified = sum(view.completion_status == "VERIFIED" for view in applicable)
    ttfts = [view.mean_ttft_ms for view in views if view.mean_ttft_ms is not None]
    latencies = [
        step.latency_ms for view in views for step in view.steps if step.latency_ms is not None
    ]
    no_progress = sum(view.terminal_no_progress for view in views)
    return EvaluationQualitySummary(
        logical_tool_calls=sum(view.logical_tool_calls for view in views),
        physical_tool_attempts=sum(view.physical_tool_attempts for view in views),
        tool_success_rate=_rate(successes, successes + failures),
        tool_retry_rate=_rate(retried, attempted),
        tool_timeout_rate=_rate(timed_out, attempted),
        tool_unknown_count=sum(view.tool_unknowns for view in views),
        completion_applicable_trials=len(applicable),
        completion_verified_trials=verified,
        completion_verified_rate=_rate(verified, len(applicable)),
        no_progress_trials=no_progress,
        no_progress_rate=_rate(no_progress, len(views)),
        mean_ttft_ms=_mean(ttfts),
        mean_step_latency_ms=_mean(latencies),
    )


class RecoveryOutcome(StrEnum):
    RECOVERED = "RECOVERED"
    EXPECTED_SAFE_STOP = "EXPECTED_SAFE_STOP"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RecoveryEvaluation:
    executions: int
    recovered: int
    expected_safe_stops: int
    failed: int
    safe_outcome_rate: float | None


def project_recovery_report(report: dict[str, Any]) -> RecoveryEvaluation:
    outcomes = []
    for item in report.get("results", []):
        if not isinstance(item, dict) or not item.get("passed"):
            outcomes.append(RecoveryOutcome.FAILED)
        elif item.get("classification") == "recovered":
            outcomes.append(RecoveryOutcome.RECOVERED)
        elif item.get("classification") == "expected-safe":
            outcomes.append(RecoveryOutcome.EXPECTED_SAFE_STOP)
        else:
            outcomes.append(RecoveryOutcome.FAILED)
    recovered = outcomes.count(RecoveryOutcome.RECOVERED)
    safe = outcomes.count(RecoveryOutcome.EXPECTED_SAFE_STOP)
    failed = outcomes.count(RecoveryOutcome.FAILED)
    return RecoveryEvaluation(
        executions=len(outcomes),
        recovered=recovered,
        expected_safe_stops=safe,
        failed=failed,
        safe_outcome_rate=_rate(recovered + safe, len(outcomes)),
    )


def _project_steps(
    bundle: TraceBundle,
    records: dict[str, ToolExecutionRecord],
) -> list[StepView]:
    result = []
    anchors = [
        span
        for span in bundle.spans
        if span.span_type == SpanType.AGENT and span.name == "agent.step"
    ]
    for step in anchors:
        children = [span for span in bundle.spans if span.parent_span_id == step.span_id]
        models = [span for span in children if span.span_type == SpanType.LLM]
        tools = [span for span in children if span.span_type == SpanType.TOOL]
        invocation_ids = tuple(
            sorted(
                {
                    str(span.attributes["invocation_id"])
                    for span in tools
                    if span.attributes.get("invocation_id")
                }
            )
        )
        outcomes = tuple(
            records[invocation_id].status.value
            for invocation_id in invocation_ids
            if invocation_id in records
        )
        physical_attempts = 0
        for span in tools:
            if span.attributes.get("reused_result"):
                continue
            invocation_id = str(span.attributes.get("invocation_id") or "")
            record = records.get(invocation_id)
            physical_attempts += (
                record.attempt
                if record is not None
                else max(1, int(span.attributes.get("attempt") or 0))
            )
        ttfts = _ttfts(models)
        result.append(
            StepView(
                run_id=bundle.trace.run_id,
                step_index=int(step.attributes.get("step_index") or 0),
                span_id=step.span_id,
                kind=str(step.attributes.get("kind") or "unknown"),
                model_calls=len(models),
                logical_tool_calls=len(invocation_ids),
                physical_tool_attempts=physical_attempts,
                input_tokens=sum(int(span.attributes.get("prompt_tokens") or 0) for span in models),
                output_tokens=sum(
                    int(span.attributes.get("completion_tokens") or 0) for span in models
                ),
                latency_ms=step.latency_ms,
                ttft_ms=_mean(ttfts),
                tool_invocation_ids=invocation_ids,
                tool_outcomes=outcomes,
            )
        )
    return result


def _ttfts(spans: Iterable[Span]) -> list[float]:
    return [
        float(span.attributes["ttft_ms"])
        for span in spans
        if isinstance(span.attributes.get("ttft_ms"), (int, float))
    ]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _mean(values: Iterable[float]) -> float | None:
    values = tuple(values)
    return round(sum(values) / len(values), 3) if values else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
