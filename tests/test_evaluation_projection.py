from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from axiom.evaluation import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationQualitySummary,
    EvaluationRunResult,
    EvaluationSuiteResult,
    RegressionThresholds,
    RunEvaluationView,
    evaluate_regression_gate,
    project_recovery_report,
    project_run_evidence,
)
from axiom.runtime import (
    Checkpoint,
    RunStatus,
    Span,
    SpanStatus,
    SpanType,
    ToolExecutionRecord,
    ToolExecutionStatus,
    Trace,
    TraceBundle,
)
from axiom.runtime.models import RunError


def _state(run_id: str = "run") -> Checkpoint:
    return Checkpoint.create(thread_id="thread", turn_id="turn", run_id=run_id, input="task")


def _bundle(
    run_id: str = "run",
    *,
    step_index: int = 0,
    ttft_ms: float | None = 12.5,
    tool_attributes: dict | None = None,
) -> TraceBundle:
    started = "2026-01-01T00:00:00+00:00"
    ended = "2026-01-01T00:00:01+00:00"
    step = Span(
        span_id=f"step-{step_index}",
        trace_id=f"trace-{run_id}",
        span_type=SpanType.AGENT,
        name="agent.step",
        started_at=started,
        ended_at=ended,
        status=SpanStatus.SUCCEEDED,
        attributes={"step_index": step_index, "kind": "llm"},
    )
    llm_attributes = {"prompt_tokens": 7, "completion_tokens": 3}
    if ttft_ms is not None:
        llm_attributes["ttft_ms"] = ttft_ms
    spans = [
        step,
        Span(
            span_id=f"llm-{step_index}",
            trace_id=f"trace-{run_id}",
            parent_span_id=step.span_id,
            span_type=SpanType.LLM,
            name="llm.chat",
            started_at=started,
            ended_at=ended,
            status=SpanStatus.SUCCEEDED,
            attributes=llm_attributes,
        ),
    ]
    if tool_attributes is not None:
        spans.append(
            Span(
                span_id=f"tool-{step_index}",
                trace_id=f"trace-{run_id}",
                parent_span_id=step.span_id,
                span_type=SpanType.TOOL,
                name="tool.lookup",
                started_at=started,
                ended_at=ended,
                status=SpanStatus.SUCCEEDED,
                attributes=tool_attributes,
            )
        )
    return TraceBundle(
        trace=Trace(
            trace_id=f"trace-{run_id}",
            run_id=run_id,
            thread_id="thread",
            turn_id="turn",
            started_at=started,
            ended_at=ended,
            status=RunStatus.COMPLETED.value,
        ),
        spans=spans,
    )


def _tool(
    invocation_id: str,
    status: ToolExecutionStatus,
    *,
    attempt: int = 1,
    failure: str | None = None,
) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        invocation_id=invocation_id,
        run_id="run",
        tool_call_id=invocation_id,
        tool_name="lookup",
        arguments_hash="hash",
        status=status,
        attempt=attempt,
        last_failure_category=failure,
    )


def test_step_view_projects_model_tokens_latency_and_ttft() -> None:
    view = project_run_evidence(_state(), [_bundle()], [])

    assert len(view.steps) == 1
    assert view.steps[0].model_calls == 1
    assert (view.steps[0].input_tokens, view.steps[0].output_tokens) == (7, 3)
    assert view.steps[0].latency_ms == 1000
    assert view.mean_ttft_ms == 12.5


def test_missing_ttft_remains_unavailable_and_projection_order_is_deterministic() -> None:
    view = project_run_evidence(_state(), [_bundle(step_index=2, ttft_ms=None), _bundle()], [])

    assert [step.step_index for step in view.steps] == [0, 2]
    assert view.steps[1].ttft_ms is None


def test_tool_metrics_distinguish_logical_attempts_retries_timeouts_and_unknown() -> None:
    records = [
        _tool("ok", ToolExecutionStatus.SUCCEEDED),
        _tool("retried", ToolExecutionStatus.SUCCEEDED, attempt=2),
        _tool("timeout", ToolExecutionStatus.FAILED, failure="timeout"),
        _tool("unknown", ToolExecutionStatus.UNKNOWN),
    ]
    view = project_run_evidence(_state(), [], records)

    assert view.logical_tool_calls == 4
    assert view.physical_tool_attempts == 5
    assert view.tool_success_rate == 0.6667
    assert view.tool_retry_rate == 0.25
    assert view.tool_timeout_rate == 0.25
    assert view.tool_unknowns == 1


def test_reused_tool_result_adds_logical_reference_but_no_new_physical_attempt() -> None:
    record = _tool("reuse", ToolExecutionStatus.SUCCEEDED, attempt=2)
    bundle = _bundle(
        tool_attributes={"invocation_id": "reuse", "reused_result": True, "attempt": 2}
    )
    view = project_run_evidence(_state(), [bundle], [record])

    assert view.steps[0].logical_tool_calls == 1
    assert view.steps[0].physical_tool_attempts == 0
    assert view.physical_tool_attempts == 2  # historical durable attempts remain truthful


def test_completion_denominator_excludes_not_applicable_and_no_progress_is_terminal_only() -> None:
    verified = _state("verified")
    verified.completion_verification = {"status": "VERIFIED", "verified": True}
    not_applicable = _state("not-applicable")
    recovery_hint = _state("hint")
    recovery_hint.progress_state = {"recovery_attempts": 1}
    terminal = _state("no-progress")
    terminal.status = RunStatus.FAILED
    terminal.error = RunError(type="NO_PROGRESS", message="bounded recovery exhausted")
    views = [
        project_run_evidence(item, [], [])
        for item in (verified, not_applicable, recovery_hint, terminal)
    ]
    results = [_result(index, quality) for index, quality in enumerate(views)]
    suite = EvaluationSuiteResult.create(
        EvaluationDataset(
            name="quality",
            version="1",
            cases=tuple(EvaluationCase(id=str(index), prompt="task") for index in range(4)),
        ),
        results,
        started_at="2026-01-01T00:00:00+00:00",
    )

    assert suite.quality.completion_applicable_trials == 1
    assert suite.quality.completion_verified_rate == 1.0
    assert suite.quality.no_progress_trials == 1
    assert suite.quality.no_progress_rate == 0.25


def test_quality_projection_round_trips_without_becoming_recovery_state() -> None:
    quality = project_run_evidence(_state(), [_bundle()], [])

    assert RunEvaluationView.from_dict(quality.to_dict()) == quality
    assert "save" not in dir(quality)

    suite = _suite_with_quality("round-trip", completion="VERIFIED", no_progress=False)
    encoded = suite.to_dict()
    encoded["schema_version"] = 3
    encoded.pop("quality")
    for result in encoded["results"]:
        result.pop("quality")
    loaded = EvaluationSuiteResult.from_dict(encoded)
    assert loaded.schema_version == 4
    assert loaded.quality.completion_verified_rate is None


def test_recovery_projection_uses_current_fault_matrix_outcomes() -> None:
    root = Path(__file__).resolve().parents[1]
    report = json.loads(
        (root / "benchmarks/recovery/results/recovery-baseline.json").read_text(encoding="utf-8")
    )
    projected = project_recovery_report(report)

    assert projected.executions == report["matrix"]["total_executions"]
    assert projected.failed == report["summary"]["failures"]
    assert (
        projected.recovered + projected.expected_safe_stops + projected.failed
        == projected.executions
    )


def test_regression_gate_can_enforce_quality_metrics_without_aggressive_defaults() -> None:
    baseline = _suite_with_quality("base", completion="VERIFIED", no_progress=False)
    candidate = _suite_with_quality("candidate", completion="NOT_VERIFIED", no_progress=True)

    default = evaluate_regression_gate(baseline, candidate)
    strict = evaluate_regression_gate(
        baseline,
        candidate,
        thresholds=RegressionThresholds(
            max_completion_verified_rate_drop=0.1,
            max_no_progress_rate_increase=0.1,
        ),
    )

    assert default.passed
    assert not strict.passed
    assert any("completion verified rate" in item for item in strict.failures)
    assert any("NO_PROGRESS rate" in item for item in strict.failures)

    tool_baseline = replace(
        baseline,
        quality=replace(baseline.quality, tool_success_rate=1.0),
    )
    tool_candidate = replace(
        candidate,
        quality=replace(candidate.quality, tool_success_rate=0.5),
    )
    tool_gate = evaluate_regression_gate(
        tool_baseline,
        tool_candidate,
        thresholds=RegressionThresholds(max_tool_success_rate_drop=0.1),
    )
    unavailable = evaluate_regression_gate(
        replace(tool_baseline, quality=EvaluationQualitySummary()),
        tool_candidate,
        thresholds=RegressionThresholds(max_tool_success_rate_drop=0.1),
    )

    assert not tool_gate.passed
    assert unavailable.passed
    assert "evidence is unavailable" in unavailable.warnings[0]


def _result(index: int, quality: RunEvaluationView) -> EvaluationRunResult:
    return EvaluationRunResult(
        case_id=str(index),
        run_id=quality.run_id,
        thread_id="thread",
        turn_id="turn",
        trace_id=None,
        status=RunStatus.COMPLETED.value,
        assistant_output="ok",
        duration_ms=1,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        tool_calls=[],
        step_count=0,
        passed=True,
        quality=quality,
    )


def _suite_with_quality(
    name: str,
    *,
    completion: str,
    no_progress: bool,
) -> EvaluationSuiteResult:
    state = _state(name)
    state.completion_verification = {
        "status": completion,
        "verified": completion == "VERIFIED",
    }
    if no_progress:
        state.error = RunError(type="NO_PROGRESS", message="stalled")
    result = _result(0, project_run_evidence(state, [], []))
    result.case_id = "0"
    dataset = EvaluationDataset(
        name=name,
        version="1",
        cases=(EvaluationCase(id="0", prompt="task"),),
    )
    return EvaluationSuiteResult.create(dataset, [result], started_at="2026-01-01T00:00:00+00:00")
