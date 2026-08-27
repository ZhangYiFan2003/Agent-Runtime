from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.rag.models import CodeSearchResult
from benchmarks.agent_runtime.run_agent_benchmark import validate_dataset
from benchmarks.recovery.run_fault_injection import (
    classify_execution,
    load_scenarios,
)
from benchmarks.recovery.run_fault_injection import (
    serialize_report as serialize_recovery_report,
)
from benchmarks.retrieval.evaluation import (
    RelevantTarget,
    RetrievalCase,
    evaluate_rankings,
    load_retrieval_dataset,
    serialize_report,
)

ROOT = Path(__file__).resolve().parents[1]


def _case(*targets: RelevantTarget) -> RetrievalCase:
    return RetrievalCase("case", "query", targets, "test", "lexical-oriented", "test")


def _result(path: str, symbol: str | None = None) -> CodeSearchResult:
    return CodeSearchResult(path=path, line=1, snippet="", symbol_name=symbol)


def test_retrieval_dataset_schema_and_distribution() -> None:
    dataset = load_retrieval_dataset(
        ROOT / "benchmarks/retrieval/dataset.json", repository_root=ROOT
    )
    assert len(dataset.cases) == 80
    assert len({case.id for case in dataset.cases}) == 80
    assert {case.query_type for case in dataset.cases} == {
        "lexical-oriented",
        "semantic/paraphrase-oriented",
        "symbol-oriented",
        "graph/context-oriented",
    }


def test_retrieval_dataset_rejects_duplicate_id_and_invalid_ground_truth(tmp_path: Path) -> None:
    source = json.loads((ROOT / "benchmarks/retrieval/dataset.json").read_text(encoding="utf-8"))
    source["cases"] = [source["cases"][0], source["cases"][0]]
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="ids must be unique"):
        load_retrieval_dataset(duplicate, repository_root=ROOT)

    source["cases"] = [dict(source["cases"][0])]
    source["cases"][0]["relevant"] = [{"path": "src/axiom/missing.py"}]
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ValueError, match="does not exist"):
        load_retrieval_dataset(invalid, repository_root=ROOT)


def test_recall_mrr_and_multi_relevant_coverage_are_correct() -> None:
    case = _case(RelevantTarget("a.py", "A"), RelevantTarget("b.py", "B"))
    rankings = {"case": [_result("x.py"), _result("a.py", "A"), _result("b.py", "B")]}
    metrics = evaluate_rankings([case], rankings)
    assert metrics == {
        "recall_at_1": 0.0,
        "recall_at_3": 1.0,
        "recall_at_5": 1.0,
        "mrr_at_5": 0.5,
        "top_1_accuracy": 0.0,
        "relevant_coverage_at_5": 1.0,
    }
    assert evaluate_rankings([], {})["mrr_at_5"] == 0.0


def test_result_serialization_is_deterministic_and_rejects_secrets() -> None:
    report = {"z": 1, "a": {"value": 2}}
    assert serialize_report(report) == serialize_report(report)
    assert serialize_recovery_report(report) == serialize_recovery_report(report)
    with pytest.raises(ValueError, match="secret-like"):
        serialize_report({"api_key": "never"})
    with pytest.raises(ValueError, match="secret-like"):
        serialize_recovery_report({"authorization": "never"})


def test_agent_dataset_and_recovery_matrix_validate() -> None:
    agent = validate_dataset(ROOT / "benchmarks/agent-runtime/dataset.json")
    recovery = load_scenarios(ROOT / "benchmarks/recovery/scenarios.json")
    assert agent["case_count"] == 40
    assert len(recovery["scenarios"]) == 21


def test_recovery_result_classification() -> None:
    scenario = {
        "expected_outcome": "expected-safe",
        "assertions": ["no_duplicate_tool", "terminal_state_correct"],
    }
    passed = classify_execution(scenario, passed=True)
    failed = classify_execution(scenario, passed=False)
    assert passed["classification"] == "expected-safe"
    assert passed["duplicate_tool_executions"] == 0
    assert passed["incorrect_terminal_states"] == 0
    assert failed["classification"] == "failure"
