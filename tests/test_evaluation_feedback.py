from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.entrypoints import cli
from axiom.evaluation import (
    BadCaseCollector,
    BadCaseError,
    BadCasePromotionError,
    BadCaseStore,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunner,
    EvaluationRunResult,
    EvaluationSuiteResult,
    RegressionThresholds,
    ReviewStatus,
    ScoreResult,
    ScorerSpec,
    evaluate_regression_gate,
    load_dataset,
    promote_badcase,
    save_result,
)
from axiom.evaluation.attribution import build_evaluation_attribution
from axiom.runtime import (
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    RunStatus,
    Span,
    SpanStatus,
    SpanType,
    Trace,
)
from axiom.runtime.models import (
    Checkpoint,
    RunError,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from axiom.tools import ToolRegistry


def _case(
    case_id: str = "case",
    *,
    scorers: tuple[ScorerSpec, ...] | None = None,
) -> EvaluationCase:
    return EvaluationCase(
        id=case_id,
        prompt="Return the stable answer",
        expected={"answer": "ok"},
        scorers=scorers
        if scorers is not None
        else (ScorerSpec(type="exact_match", config={"expected": "ok"}),),
    )


def _dataset(*cases: EvaluationCase) -> EvaluationDataset:
    selected = cases or (_case(),)
    return EvaluationDataset(name="agent-core", version="2", cases=tuple(selected))


def _run(**overrides) -> EvaluationRunResult:
    values = {
        "case_id": "case",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "trace_id": "trace-1",
        "status": RunStatus.FAILED.value,
        "assistant_output": "wrong",
        "duration_ms": 100.0,
        "prompt_tokens": 8,
        "completion_tokens": 2,
        "total_tokens": 10,
        "tool_calls": [],
        "step_count": 2,
        "error": "run failed",
        "case_definition": _case().to_dict(),
        "scores": [
            ScoreResult(
                scorer="exact_match",
                passed=False,
                score=0.0,
                reason="output did not exactly match",
            )
        ],
        "passed": False,
        "attribution": {
            "runtime_version": "runtime-1",
            "model_provider": "test-provider",
            "model_name": "test-model",
            "model_configuration_hash": "model-config-hash",
            "prompt_version": "prompt-hash",
            "tool_schema_version": "tool-hash",
            "policy_version": "policy-hash",
            "context_policy_version": "context-hash",
            "dataset_version": "2",
        },
    }
    values.update(overrides)
    return EvaluationRunResult(**values)


def _suite(results: list[EvaluationRunResult]) -> EvaluationSuiteResult:
    case_ids = list(dict.fromkeys(result.case_id for result in results))
    dataset = _dataset(*[_case(case_id) for case_id in case_ids])
    return EvaluationSuiteResult.create(
        dataset,
        results,
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
        trials_per_case=max((result.trial_index for result in results), default=1),
        attribution=results[0].attribution if results else {},
    )


def _collect_failed(tmp_path: Path):
    store = BadCaseStore(tmp_path / "badcases.json")
    records = BadCaseCollector(store).collect_suite(_suite([_run()]))
    return store, records[0]


def test_failed_result_collects_pending_badcase_and_pass_is_skipped(tmp_path):
    passed = _run(
        case_id="passing",
        run_id="run-pass",
        passed=True,
        status=RunStatus.COMPLETED.value,
        assistant_output="ok",
        error=None,
        scores=[ScoreResult("exact_match", True, 1.0, "matched")],
        case_definition=_case("passing").to_dict(),
    )
    store = BadCaseStore(tmp_path / "badcases.json")
    records = BadCaseCollector(store).collect_suite(_suite([_run(), passed]))

    assert len(records) == 1
    assert records[0].review_status == ReviewStatus.PENDING
    assert records[0].source_case_id == "case"
    assert store.get(records[0].id) == records[0]


def test_duplicate_collection_is_idempotent_and_restart_safe(tmp_path):
    path = tmp_path / "badcases.json"
    suite = _suite([_run()])
    first = BadCaseCollector(BadCaseStore(path)).collect_suite(suite)[0]
    second = BadCaseCollector(BadCaseStore(path)).collect_suite(suite)[0]

    assert first.id == second.id
    assert len(BadCaseStore(path).list()) == 1


def test_failure_taxonomy_supports_multiple_types_and_bounded_redacted_metadata(tmp_path):
    result = _run(
        error="Tool execution failed: invalid argument",
        error_metadata={
            "type": "ToolArgumentError",
            "message": "x" * 10_000,
            "api_key": "do-not-store",
            "nested": {"access_token": "also-secret"},
        },
    )
    record = BadCaseCollector(BadCaseStore(tmp_path / "badcases.json")).collect_suite(
        _suite([result])
    )[0]

    assert {"run_failed", "wrong_answer", "tool_failure", "tool_argument_failure"} <= set(
        record.failure_types
    )
    assert record.error_metadata["api_key"] == "[REDACTED]"
    assert record.error_metadata["nested"]["access_token"] == "[REDACTED]"
    assert "truncated" in record.error_metadata["message"]


def test_context_budget_and_tool_usage_failures_classify_deterministically(tmp_path):
    context = _run(
        run_id="context-run",
        error="known local hard input limit",
        error_metadata={"type": "CONTEXT_BUDGET_EXCEEDED", "step": "llm"},
    )
    tool_score = ScoreResult(
        scorer="tool_usage",
        passed=False,
        score=0.0,
        reason="wrong tools",
        details={
            "required": ["read_file"],
            "forbidden": ["write_file"],
            "used": ["write_file"],
        },
    )
    tool = _run(run_id="tool-run", scores=[tool_score])
    records = BadCaseCollector(BadCaseStore(tmp_path / "badcases.json")).collect_suite(
        _suite([context, tool])
    )

    assert "context_budget_exceeded" in records[0].failure_types
    assert {"wrong_tool", "forbidden_tool"} <= set(records[1].failure_types)
    assert records[0].failure_evidence


def test_step_and_token_threshold_failures_classify_deterministically(tmp_path):
    score = ScoreResult(
        scorer="metric_threshold",
        passed=False,
        score=0.0,
        reason="limits exceeded",
        details={
            "steps": {"actual": 5, "max": 3},
            "tokens": {"actual": 500, "max": 300},
        },
    )
    record = BadCaseCollector(BadCaseStore(tmp_path / "badcases.json")).collect_suite(
        _suite([_run(scores=[score])])
    )[0]

    assert {"step_budget_exceeded", "token_budget_exceeded"} <= set(record.failure_types)


def test_terminal_run_collection_resolves_checkpoint_trace_and_tool_execution(tmp_path):
    async def scenario():
        runtime = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        state = Checkpoint.create(
            thread_id="thread-runtime",
            turn_id="turn-runtime",
            run_id="run-runtime",
            input="perform the operation",
        )
        state.status = RunStatus.FAILED
        state.error = RunError(type="ToolExecutionError", message="tool failed", step="tool")
        await runtime.save(state)
        trace = Trace(
            trace_id="trace-runtime",
            run_id=state.run_id,
            thread_id=state.thread_id,
            turn_id=state.turn_id,
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:01+00:00",
            status=RunStatus.FAILED.value,
        )
        await observations.save_trace(trace)
        await observations.save_span(
            Span(
                span_id="span-tool",
                trace_id=trace.trace_id,
                span_type=SpanType.TOOL,
                name="tool.lookup",
                started_at=trace.started_at,
                ended_at=trace.ended_at,
                status=SpanStatus.FAILED,
                attributes={"tool_name": "lookup", "invocation_id": "invoke-1"},
            )
        )
        await runtime.save_tool_execution(
            ToolExecutionRecord(
                invocation_id="invoke-1",
                run_id=state.run_id,
                tool_call_id="call-1",
                tool_name="lookup",
                arguments_hash="safe-hash",
                status=ToolExecutionStatus.FAILED,
                result="invalid argument",
                is_error=True,
                error="required input missing",
            )
        )
        record = await BadCaseCollector(
            BadCaseStore(tmp_path / "badcases.json"),
            runtime_store=runtime,
            observability_store=observations,
        ).collect_run(state.run_id)

        assert record.source_type == "runtime"
        assert record.trace_id == trace.trace_id
        assert record.tool_names == ("lookup",)
        assert {"run_failed", "tool_failure", "tool_argument_failure"} <= set(record.failure_types)
        assert record.task == "perform the operation"

    asyncio.run(scenario())


def test_review_transitions_and_invalid_terminal_transition(tmp_path):
    store, record = _collect_failed(tmp_path)
    approved = store.update_review_status(record.id, ReviewStatus.APPROVED, note="real defect")
    ignored = store.update_review_status(approved.id, ReviewStatus.IGNORED, note="provider outage")
    approved_again = store.update_review_status(ignored.id, ReviewStatus.APPROVED)

    assert approved.review_status == ReviewStatus.APPROVED
    assert approved.review_note == "real defect"
    assert ignored.review_status == ReviewStatus.IGNORED
    assert approved_again.review_status == ReviewStatus.APPROVED
    promoted = store.mark_promoted(approved_again.id, dataset="regression.json", case_id="r1")
    with pytest.raises(BadCaseError, match="cannot transition"):
        store.update_review_status(promoted.id, ReviewStatus.IGNORED)


def test_pending_cannot_promote_and_approved_promotion_is_idempotent(tmp_path):
    store, record = _collect_failed(tmp_path)
    dataset_path = tmp_path / "regression.json"
    with pytest.raises(BadCasePromotionError) as pending:
        promote_badcase(store, record.id, dataset_path)
    assert pending.value.code == "BADCASE_NOT_APPROVED"

    store.update_review_status(record.id, ReviewStatus.APPROVED)
    promoted, case = promote_badcase(store, record.id, dataset_path)
    again, same_case = promote_badcase(store, record.id, dataset_path)

    assert promoted.review_status == ReviewStatus.PROMOTED
    assert again.id == promoted.id
    assert same_case == case
    assert case.metadata["source_badcase_id"] == record.id


def test_promotion_preserves_unrelated_cases_and_requires_scorer_evidence(tmp_path):
    store, record = _collect_failed(tmp_path)
    store.update_review_status(record.id, ReviewStatus.APPROVED)
    existing = _case("existing")
    dataset_path = tmp_path / "regression.json"
    from axiom.evaluation import save_dataset

    save_dataset(_dataset(existing), dataset_path)
    _, promoted = promote_badcase(store, record.id, dataset_path)
    loaded = load_dataset(dataset_path)

    assert [case.id for case in loaded.cases] == ["existing", promoted.id]
    assert loaded.cases[1].metadata["source_badcase_id"] == record.id

    insufficient_result = _run(
        run_id="no-evidence",
        scores=[],
        case_definition={"id": "case", "prompt": "a task", "expected": {}, "scorers": []},
    )
    insufficient_store = BadCaseStore(tmp_path / "insufficient.json")
    insufficient = BadCaseCollector(insufficient_store).collect_suite(
        _suite([insufficient_result])
    )[0]
    insufficient_store.update_review_status(insufficient.id, ReviewStatus.APPROVED)
    with pytest.raises(BadCasePromotionError) as error:
        promote_badcase(insufficient_store, insufficient.id, tmp_path / "other.json")
    assert error.value.code == "BADCASE_PROMOTION_INSUFFICIENT_EXPECTATION"


class _TrialExecutor:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls = 0

    async def execute(self, case: EvaluationCase) -> EvaluationRunResult:
        self.calls += 1
        index = self.calls
        output = self.outputs[index - 1]
        return _run(
            case_id=case.id,
            run_id=f"run-{index}",
            thread_id=f"thread-{index}",
            turn_id=f"turn-{index}",
            trace_id=f"trace-{index}",
            status=RunStatus.COMPLETED.value,
            assistant_output=output,
            duration_ms=float(index * 10),
            total_tokens=index * 10,
            prompt_tokens=index * 8,
            completion_tokens=index * 2,
            step_count=index,
            error=None,
            scores=[],
        )


def test_trials_one_preserves_behavior_and_trials_many_are_isolated_and_aggregated():
    async def scenario():
        one = await EvaluationRunner(_TrialExecutor(["ok"])).run(_dataset(), trials=1)
        many = await EvaluationRunner(_TrialExecutor(["ok", "wrong", "ok"])).run(
            _dataset(), trials=3
        )

        assert len(one.results) == 1
        assert one.trials_per_case == 1
        assert one.pass_rate == 1.0
        assert len({item.run_id for item in many.results}) == 3
        assert len({item.thread_id for item in many.results}) == 3
        assert len({item.turn_id for item in many.results}) == 3
        assert len({item.trace_id for item in many.results}) == 3
        assert [item.trial_index for item in many.results] == [1, 2, 3]
        aggregate = many.case_aggregates[0]
        assert (aggregate.trial_count, aggregate.success_count, aggregate.failure_count) == (
            3,
            2,
            1,
        )
        assert aggregate.trial_success_rate == pytest.approx(2 / 3, abs=0.0001)
        assert (aggregate.avg_tokens, aggregate.min_tokens, aggregate.max_tokens) == (20, 10, 30)
        assert (aggregate.avg_steps, aggregate.min_steps, aggregate.max_steps) == (2, 1, 3)
        assert (aggregate.avg_latency_ms, aggregate.min_latency_ms, aggregate.max_latency_ms) == (
            20,
            10,
            30,
        )
        assert not many.results[1].passed
        assert many.results[1].scores[0].reason
        assert many.attribution["dataset_version"] == "2"
        assert many.results[0].attribution["model_configuration_hash"] == "model-config-hash"

    asyncio.run(scenario())


def _trial_suite(
    outcomes: list[bool],
    *,
    tokens: int = 100,
    steps: int = 2,
    latency: float = 100,
    case_id: str = "case",
) -> EvaluationSuiteResult:
    results = [
        _run(
            case_id=case_id,
            run_id=f"{case_id}-{index}",
            trial_index=index,
            passed=passed,
            status=RunStatus.COMPLETED.value,
            total_tokens=tokens,
            step_count=steps,
            duration_ms=latency,
        )
        for index, passed in enumerate(outcomes, start=1)
    ]
    return _suite(results)


def test_regression_gate_functional_stochastic_and_tolerance_rules():
    hard = evaluate_regression_gate(_trial_suite([True]), _trial_suite([False]))
    stochastic = evaluate_regression_gate(
        _trial_suite([True, True, True, True]),
        _trial_suite([True, True, False, False]),
        thresholds=RegressionThresholds(max_success_rate_drop=0.25),
    )
    tolerated = evaluate_regression_gate(
        _trial_suite([True, True, True, False]),
        _trial_suite([True, True, False, False]),
        thresholds=RegressionThresholds(max_success_rate_drop=0.30),
    )

    assert not hard.passed and "hard functional regression" in hard.failures[0]
    assert not stochastic.passed and "trial success-rate regression" in stochastic.failures[0]
    assert tolerated.passed
    assert tolerated.warnings


def test_regression_gate_token_step_and_performance_warning_rules():
    baseline = _trial_suite([True], tokens=100, steps=2, latency=100)
    candidate = _trial_suite([True], tokens=130, steps=5, latency=150)
    warning_only = evaluate_regression_gate(baseline, candidate)
    hard = evaluate_regression_gate(
        baseline,
        candidate,
        thresholds=RegressionThresholds(
            max_token_increase_ratio=0.20,
            max_step_increase=2,
        ),
    )

    assert warning_only.passed
    assert len(warning_only.warnings) == 3
    assert not hard.passed
    assert any("token regression" in failure for failure in hard.failures)
    assert any("step regression" in failure for failure in hard.failures)


def test_new_required_case_failure_and_cli_exit_status(tmp_path):
    baseline = _trial_suite([True], case_id="existing")
    candidate_results = [
        *_trial_suite([True], case_id="existing").results,
        *_trial_suite([False], case_id="new-required").results,
    ]
    candidate = _suite(candidate_results)
    baseline_path = save_result(baseline, tmp_path / "baseline.json")
    candidate_path = save_result(candidate, tmp_path / "candidate.json")
    gate = evaluate_regression_gate(baseline, candidate)
    command = CliRunner().invoke(cli.app, ["eval", "gate", str(baseline_path), str(candidate_path)])

    assert not gate.passed
    assert any("new required case failed" in item for item in gate.failures)
    assert command.exit_code == 1
    assert '"status": "FAIL"' in command.stdout


def test_cli_trials_and_badcase_commands_are_scriptable(tmp_path, monkeypatch):
    captured: dict[str, int] = {}
    suite = _trial_suite([False])

    async def fake_execute(_dataset_path, *, cwd, data_dir, trials):
        del cwd, data_dir
        captured["trials"] = trials
        return suite

    monkeypatch.setattr(cli, "_execute_evaluation_dataset", fake_execute)
    runner = CliRunner()
    result_path = save_result(suite, tmp_path / "result.json")
    run = runner.invoke(cli.app, ["eval", "run", "dataset.json", "--trials", "3"])
    store_path = tmp_path / "badcases.json"
    collect = runner.invoke(
        cli.app,
        [
            "eval",
            "badcase",
            "collect",
            "--result",
            str(result_path),
            "--store",
            str(store_path),
        ],
    )
    record = BadCaseStore(store_path).list()[0]
    approve = runner.invoke(
        cli.app,
        [
            "eval",
            "badcase",
            "approve",
            record.id,
            "--store",
            str(store_path),
            "--note",
            "confirmed",
        ],
    )
    show = runner.invoke(
        cli.app,
        ["eval", "badcase", "show", record.id, "--store", str(store_path)],
    )

    assert run.exit_code == 0
    assert captured["trials"] == 3
    assert collect.exit_code == 0
    assert approve.exit_code == 0
    assert show.exit_code == 0
    assert '"review_status": "APPROVED"' in show.stdout


class _FingerprintLlm:
    provider_name = "safe-provider"
    model_name = "safe-model"
    max_context_window = 32_000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        if False:
            yield {}


def test_attribution_fingerprints_are_complete_and_exclude_secrets(tmp_path):
    config = AxiomConfig()
    config.llm.api_key = "super-secret-api-key"
    config.llm.base_url = "https://contains-private-value.invalid"
    engine = QueryEngine(
        llm_client=_FingerprintLlm(),
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
    )
    attribution = build_evaluation_attribution(engine, runtime_version="build-123")
    encoded = json.dumps(attribution)

    assert {
        "runtime_version",
        "model_provider",
        "model_name",
        "model_configuration_hash",
        "prompt_version",
        "tool_schema_version",
        "policy_version",
        "context_policy_version",
    } <= attribution.keys()
    assert attribution["runtime_version"] == "build-123"
    assert "super-secret" not in encoded
    assert "private-value" not in encoded
    assert "api_key" not in encoded


def test_badcase_inherits_attribution_metadata(tmp_path):
    _, record = _collect_failed(tmp_path)

    assert record.runtime_version == "runtime-1"
    assert record.model_provider == "test-provider"
    assert record.model_name == "test-model"
    assert record.model_configuration_hash == "model-config-hash"
    assert record.prompt_version == "prompt-hash"
    assert record.context_policy_version == "context-hash"
    assert record.dataset_version == "2"


def test_result_v1_loading_remains_compatible():
    result = _suite([_run()]).to_dict()
    result["schema_version"] = 1
    result.pop("case_aggregates")
    result.pop("trial_count")
    result.pop("trial_success_rate")
    result.pop("trials_per_case")
    for trial in result["results"]:
        trial.pop("trial_index")
        trial.pop("case_definition")
        trial.pop("error_metadata")
        trial.pop("attribution")

    loaded = EvaluationSuiteResult.from_dict(result)

    assert loaded.trials_per_case == 1
    assert loaded.trial_count == 1
    assert loaded.case_aggregates[0].trial_count == 1


def test_invalid_trials_rejected():
    with pytest.raises(ValueError, match="at least 1"):
        asyncio.run(EvaluationRunner(_TrialExecutor([])).run(_dataset(), trials=0))


def test_approved_promotion_supports_explicit_override_when_evidence_is_insufficient(tmp_path):
    result = _run(
        scores=[],
        case_definition={"id": "case", "prompt": "say corrected", "scorers": []},
    )
    store = BadCaseStore(tmp_path / "badcases.json")
    record = BadCaseCollector(store).collect_suite(_suite([result]))[0]
    store.update_review_status(record.id, ReviewStatus.APPROVED)

    promoted, case = promote_badcase(
        store,
        record.id,
        tmp_path / "regression.json",
        expected_override={"answer": "corrected"},
    )

    assert promoted.review_status == ReviewStatus.PROMOTED
    assert case.scorers[0].type == "exact_match"
    assert case.scorers[0].config["expected"] == "corrected"
