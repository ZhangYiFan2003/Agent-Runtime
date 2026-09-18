from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def load_scenarios(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or not isinstance(raw.get("scenarios"), list):
        raise ValueError("invalid recovery scenario schema")
    ids = [item.get("id") for item in raw["scenarios"]]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("recovery scenario ids must be present and unique")
    for item in raw["scenarios"]:
        if item.get("expected_outcome") not in {"recovered", "expected-safe"}:
            raise ValueError(f"invalid recovery outcome: {item.get('id')}")
        if not Path(str(item.get("test", "")).split("::", 1)[0]).is_file():
            raise ValueError(f"recovery test target does not exist: {item.get('id')}")
    return raw


def classify_execution(scenario: dict[str, Any], *, passed: bool) -> dict[str, Any]:
    assertions = set(scenario["assertions"])
    return {
        "passed": passed,
        "classification": scenario["expected_outcome"] if passed else "failure",
        "duplicate_tool_executions": 0 if passed and "no_duplicate_tool" in assertions else None,
        "duplicate_child_runs": 0 if passed and "no_duplicate_child" in assertions else None,
        "lost_completed_results": 0 if passed and "result_preserved" in assertions else None,
        "incorrect_terminal_states": 0
        if passed and "terminal_state_correct" in assertions
        else None,
    }


def serialize_report(report: dict[str, Any]) -> str:
    forbidden = {"api_key", "authorization", "credential", "secret"}
    stack = [report]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if any(any(part in str(key).casefold() for part in forbidden) for key in value):
                raise ValueError("secret-like field is not allowed in recovery artifact")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenarios", type=Path, default=Path("benchmarks/recovery/scenarios.json")
    )
    parser.add_argument("--repetitions", type=int)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmarks/recovery/results/recovery-baseline.json")
    )
    args = parser.parse_args()
    matrix = load_scenarios(args.scenarios)
    if any(
        item.get("category") == "distributed-redelivery"
        for item in matrix["scenarios"]
    ) and not os.environ.get("AXIOM_TEST_POSTGRES_DSN"):
        raise RuntimeError(
            "distributed-redelivery scenarios require AXIOM_TEST_POSTGRES_DSN"
        )
    repetitions = args.repetitions or int(matrix["repetitions"])
    results = []
    for repetition in range(1, repetitions + 1):
        for scenario in matrix["scenarios"]:
            started = time.perf_counter()
            run = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", scenario["test"], "-p", "no:cacheprovider"],
                check=False,
                capture_output=True,
                text=True,
            )
            output = run.stdout + run.stderr
            passed = run.returncode == 0 and not (
                scenario["category"] == "distributed-redelivery"
                and "skipped" in output.casefold()
            )
            classified = classify_execution(scenario, passed=passed)
            results.append(
                {
                    "scenario_id": scenario["id"],
                    "category": scenario["category"],
                    "repetition": repetition,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    **classified,
                    "failure_summary": "" if passed else output[-2000:],
                }
            )
    passed = [item for item in results if item["passed"]]
    report = {
        "schema_version": 1,
        "benchmark": matrix["name"],
        "benchmark_type": "offline deterministic fault injection",
        "environment": {
            "git_commit": _git_commit(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "matrix": {
            "scenario_count": len(matrix["scenarios"]),
            "repetitions": repetitions,
            "total_executions": len(results),
            "categories": dict(
                sorted(Counter(item["category"] for item in matrix["scenarios"]).items())
            ),
        },
        "summary": {
            "successful_recovery": sum(item["classification"] == "recovered" for item in passed),
            "expected_safe_outcomes": sum(
                item["classification"] == "expected-safe" for item in passed
            ),
            "failures": len(results) - len(passed),
            "duplicate_tool_executions": _known_sum(passed, "duplicate_tool_executions"),
            "duplicate_child_runs": _known_sum(passed, "duplicate_child_runs"),
            "lost_completed_results": _known_sum(passed, "lost_completed_results"),
            "incorrect_terminal_states": _known_sum(passed, "incorrect_terminal_states"),
            "scenario_recovery_rate": round(len(passed) / len(results), 4) if results else 0.0,
        },
        "results": results,
        "limitations": [
            "Faults are deterministic test hooks, not random process kills.",
            "Zero duplicate/loss counts mean the mapped test explicitly asserted that property.",
            (
                "Ambiguous external side effects count as expected-safe when Runtime waits for "
                "explicit recovery."
            ),
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialize_report(report), encoding="utf-8")
    args.output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(_markdown(report))
    if report["summary"]["failures"]:
        raise SystemExit(1)


def _known_sum(items: list[dict[str, Any]], key: str) -> int:
    return sum(item[key] for item in items if item[key] is not None)


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={Path.cwd().as_posix()}", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "unknown"


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Durable Recovery Fault-Injection Baseline",
        "",
        "> Deterministic offline recovery evidence.",
        "",
        f"- Scenarios: {report['matrix']['scenario_count']}",
        f"- Repetitions: {report['matrix']['repetitions']}",
        f"- Total executions: {report['matrix']['total_executions']}",
        f"- Recovery rate: {summary['scenario_recovery_rate']:.4f}",
        f"- Expected-safe outcomes: {summary['expected_safe_outcomes']}",
        f"- Failures: {summary['failures']}",
        f"- Duplicate Tool executions: {summary['duplicate_tool_executions']}",
        f"- Duplicate Child Runs: {summary['duplicate_child_runs']}",
        f"- Lost completed results: {summary['lost_completed_results']}",
        f"- Incorrect terminal states: {summary['incorrect_terminal_states']}",
        "",
        "| Scenario | Category | Repetition | Classification | Passed |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for item in report["results"]:
        passed = "yes" if item["passed"] else "no"
        lines.append(
            f"| {item['scenario_id']} | {item['category']} | {item['repetition']} | "
            f"{item['classification']} | {passed} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
