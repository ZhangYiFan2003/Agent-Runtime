from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.entrypoints import cli
from axiom.evaluation import (
    ContainsScorer,
    DurableEvaluationExecutor,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunner,
    EvaluationRunResult,
    EvaluationSuiteResult,
    ExactMatchScorer,
    MetricThresholdScorer,
    RunStatusScorer,
    ScorerSpec,
    ToolUsageScorer,
    compare_results,
    load_dataset,
    load_result,
    required_scores_passed,
    save_result,
    score_case,
)
from axiom.runtime import (
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RetryPolicy,
    RunStatus,
)
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolResult, object_schema


def _result(**overrides) -> EvaluationRunResult:
    values = {
        "case_id": "case",
        "run_id": "run",
        "thread_id": "thread",
        "turn_id": "turn",
        "trace_id": "trace",
        "status": RunStatus.COMPLETED.value,
        "assistant_output": "CheckpointStore is implemented in checkpoints.py",
        "duration_ms": 100.0,
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
        "tool_calls": ["grep", "read_file"],
        "step_count": 3,
    }
    values.update(overrides)
    return EvaluationRunResult(**values)


def _case(*scorers: ScorerSpec) -> EvaluationCase:
    return EvaluationCase(id="case", prompt="test", scorers=tuple(scorers))


def _dataset(case: EvaluationCase) -> EvaluationDataset:
    return EvaluationDataset(name="test-suite", version="1", cases=(case,))


def test_dataset_parsing_and_validation(tmp_path):
    path = tmp_path / "dataset.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "agent-core",
                "version": "1.0",
                "metadata": {"owner": "runtime"},
                "cases": [
                    {
                        "id": "locate_store",
                        "prompt": "Locate the store",
                        "tags": ["runtime"],
                        "timeout": 5,
                        "scorers": [{"type": "contains", "expected": "checkpoints.py"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    dataset = load_dataset(path)

    assert dataset.name == "agent-core"
    assert dataset.cases[0].timeout_seconds == 5
    assert dataset.cases[0].scorers[0].config["expected"] == "checkpoints.py"


def test_maintained_agent_core_dataset_is_valid():
    root = Path(__file__).resolve().parents[1]

    dataset = load_dataset(root / "benchmarks" / "datasets" / "agent-core.json")

    assert dataset.name == "agent-core"
    assert len(dataset.cases) == 12
    assert {"basic", "tool-selection", "multi-step", "failure-handling"} <= {
        tag for case in dataset.cases for tag in case.tags
    }


def test_contains_and_exact_match_scorers():
    async def scenario():
        result = _result(assistant_output="  AXIOM_EVAL_OK  ")
        contains = await ContainsScorer(("axiom_eval",)).score(_case(), result)
        exact = await ExactMatchScorer("AXIOM_EVAL_OK").score(_case(), result)
        mismatch = await ExactMatchScorer("different").score(_case(), result)

        assert contains.passed
        assert exact.passed
        assert not mismatch.passed

    asyncio.run(scenario())


def test_tool_usage_scorer_checks_required_and_forbidden_without_order():
    async def scenario():
        result = _result(tool_calls=["read_file", "grep", "read_file"])
        passing = await ToolUsageScorer(
            required_tools=("grep", "read_file"),
            forbidden_tools=("write_file",),
        ).score(_case(), result)
        failing = await ToolUsageScorer(
            required_tools=("list_dir",),
            forbidden_tools=("grep",),
        ).score(_case(), result)

        assert passing.passed
        assert not failing.passed
        assert failing.details["used"] == ["read_file", "grep", "read_file"]

    asyncio.run(scenario())


def test_run_status_and_metric_threshold_scorers():
    async def scenario():
        result = _result()
        status = await RunStatusScorer().score(_case(), result)
        metrics = await MetricThresholdScorer(
            max_steps=3,
            max_tokens=10,
            max_latency_ms=100,
            max_tool_calls=2,
        ).score(_case(), result)
        too_many_steps = await MetricThresholdScorer(max_steps=2).score(_case(), result)

        assert status.passed
        assert metrics.passed
        assert not too_many_steps.passed

    asyncio.run(scenario())


def test_composite_scoring_requires_all_required_scorers():
    async def scenario():
        result = _result()
        scorers = [
            RunStatusScorer(),
            ContainsScorer(("checkpoints.py",)),
            ExactMatchScorer("not exact", required=False),
        ]
        scores = await score_case(_case(), result, scorers)

        assert [score.passed for score in scores] == [True, True, False]
        assert required_scores_passed(scores)

    asyncio.run(scenario())


class ToolLlm:
    provider_name = "evaluation-test"
    model_name = "evaluation-model"
    max_context_window = 10000

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        if not any(message.role == "tool" for message in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_lookup",
                    "function": {"name": "lookup", "arguments": json.dumps({"value": "x"})},
                },
            }
            yield {"type": "usage", "usage": {"input_tokens": 4, "output_tokens": 1}}
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "found checkpoint"}
        yield {"type": "usage", "usage": {"input_tokens": 5, "output_tokens": 2}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class FailingLlm:
    provider_name = "evaluation-test"
    model_name = "failing-model"
    max_context_window = 10000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        yield {"type": "error", "error": "model unavailable"}


def _engine(llm, registry: ToolRegistry, tmp_path) -> QueryEngine:
    return QueryEngine(
        llm_client=llm,
        tool_registry=registry,
        config=AxiomConfig(),
        cwd=str(tmp_path),
    )


def test_runner_uses_real_durable_runtime_and_captures_metrics(tmp_path):
    async def scenario():
        async def lookup(_payload, _context):
            return ToolResult(content="checkpoint")

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="lookup",
                description="lookup",
                parameters=object_schema({"value": {"type": "string"}}, required=["value"]),
                handler=lookup,
                is_read_only=True,
            )
        )
        checkpoints = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        case = EvaluationCase(
            id="runtime_case",
            prompt="find it",
            scorers=(
                ScorerSpec(type="run_status"),
                ScorerSpec(type="contains", config={"expected": "checkpoint"}),
                ScorerSpec(type="tool_usage", config={"required_tools": ["lookup"]}),
                ScorerSpec(type="metric_threshold", config={"max_steps": 4}),
            ),
        )
        executor = DurableEvaluationExecutor(
            engine_factory=lambda _case: _engine(ToolLlm(), registry, tmp_path),
            checkpoint_store=checkpoints,
            observability_store=observations,
        )

        suite = await EvaluationRunner(executor).run(_dataset(case))
        result = suite.results[0]
        checkpoint = await checkpoints.load(result.run_id)
        trace = await ObservabilityService(observations).trace(result.run_id)

        assert result.passed
        assert result.status == RunStatus.COMPLETED
        assert result.assistant_output == "found checkpoint"
        assert result.tool_calls == ["lookup"]
        assert result.total_tokens == 12
        assert result.step_count == 3
        assert checkpoint is not None
        assert checkpoint.thread_id == result.thread_id
        assert checkpoint.turn_id == result.turn_id
        assert trace is not None
        assert result.trace_id == trace.trace.trace_id
        assert result.quality is not None
        assert result.quality.logical_tool_calls == 1
        assert result.quality.physical_tool_attempts == 1
        assert result.quality.tool_success_rate == 1.0
        assert len(result.quality.steps) == 3
        assert suite.quality.logical_tool_calls == 1

    asyncio.run(scenario())


def test_failed_run_is_scored_and_does_not_abort_suite(tmp_path):
    async def scenario():
        case = _case(ScorerSpec(type="run_status"))
        executor = DurableEvaluationExecutor(
            engine_factory=lambda _case: _engine(FailingLlm(), ToolRegistry(), tmp_path),
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=MemoryObservabilityStore(),
            retry_policy=RetryPolicy(max_attempts=1),
        )

        suite = await EvaluationRunner(executor).run(_dataset(case))
        result = suite.results[0]

        assert result.status == RunStatus.FAILED
        assert not result.passed
        assert result.scores[0].scorer == "run_status"
        assert "model unavailable" in str(result.error)
        assert suite.cases_failed == 1

    asyncio.run(scenario())


def test_json_report_round_trip(tmp_path):
    case = _case()
    result = _result(passed=True)
    suite = EvaluationSuiteResult.create(
        _dataset(case),
        [result],
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:01+00:00",
    )

    path = save_result(suite, tmp_path / "result.json")
    loaded = load_result(path)

    assert loaded.to_dict() == suite.to_dict()
    assert json.loads(path.read_text(encoding="utf-8"))["results"][0]["run_id"] == "run"


def test_comparison_detects_regression_improvement_and_performance_warnings():
    dataset = EvaluationDataset(
        name="agent-core",
        version="1",
        cases=(EvaluationCase(id="regressed", prompt="x"), EvaluationCase(id="better", prompt="y")),
    )
    old = EvaluationSuiteResult.create(
        dataset,
        [
            _result(
                case_id="regressed", passed=True, total_tokens=100, duration_ms=100, step_count=2
            ),
            _result(
                case_id="better", passed=False, total_tokens=100, duration_ms=100, step_count=2
            ),
        ],
        started_at="start",
    )
    new = EvaluationSuiteResult.create(
        dataset,
        [
            _result(
                case_id="regressed", passed=False, total_tokens=150, duration_ms=150, step_count=5
            ),
            _result(case_id="better", passed=True, total_tokens=150, duration_ms=150, step_count=5),
        ],
        started_at="start",
    )

    comparison = compare_results(old, new)

    assert comparison.regressions == ("regressed",)
    assert comparison.improvements == ("better",)
    assert {warning.metric for warning in comparison.performance_warnings} == {
        "avg_tokens",
        "avg_latency_ms",
        "avg_steps",
    }


def test_eval_cli_run_and_compare_smoke(tmp_path, monkeypatch):
    case = _case()
    old = EvaluationSuiteResult.create(
        _dataset(case),
        [_result(passed=True)],
        started_at="start",
    )
    new = EvaluationSuiteResult.create(
        _dataset(case),
        [_result(passed=False)],
        started_at="start",
    )

    async def fake_execute(_dataset_path, *, cwd, data_dir):
        del cwd, data_dir
        return old

    monkeypatch.setattr(cli, "_execute_evaluation_dataset", fake_execute)
    runner = CliRunner()
    output = tmp_path / "eval-result.json"
    run = runner.invoke(
        cli.app,
        ["eval", "run", "dataset.json", "--output", str(output), "--verbose"],
    )
    old_path = save_result(old, tmp_path / "old.json")
    new_path = save_result(new, tmp_path / "new.json")
    compare = runner.invoke(cli.app, ["eval", "compare", str(old_path), str(new_path)])

    assert run.exit_code == 0
    assert "Dataset: test-suite" in run.stdout
    assert "PASS" not in run.stdout  # no configured scorers in this synthetic result
    assert output.exists()
    assert compare.exit_code == 0
    assert "case PASS → FAIL" in compare.stdout
