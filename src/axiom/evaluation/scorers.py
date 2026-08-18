from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from axiom.evaluation.models import (
    EvaluationCase,
    EvaluationRunResult,
    ScoreResult,
    ScorerSpec,
)
from axiom.runtime.models import RunStatus


class Scorer(Protocol):
    name: str
    required: bool

    async def score(
        self,
        case: EvaluationCase,
        result: EvaluationRunResult,
    ) -> ScoreResult: ...


@dataclass(frozen=True, slots=True)
class ContainsScorer:
    expected: tuple[str, ...]
    case_sensitive: bool = False
    match_all: bool = True
    required: bool = True
    name: str = "contains"

    async def score(self, case: EvaluationCase, result: EvaluationRunResult) -> ScoreResult:
        del case
        output = (
            result.assistant_output if self.case_sensitive else result.assistant_output.casefold()
        )
        targets = (
            self.expected
            if self.case_sensitive
            else tuple(item.casefold() for item in self.expected)
        )
        matches = [target in output for target in targets]
        passed = all(matches) if self.match_all else any(matches)
        return _score(
            self.name,
            passed,
            self.required,
            "output contains expected text" if passed else "expected text was not found",
            {"expected": list(self.expected), "matched": sum(matches), "match_all": self.match_all},
        )


@dataclass(frozen=True, slots=True)
class ExactMatchScorer:
    expected: str
    case_sensitive: bool = True
    strip: bool = True
    required: bool = True
    name: str = "exact_match"

    async def score(self, case: EvaluationCase, result: EvaluationRunResult) -> ScoreResult:
        del case
        actual = result.assistant_output.strip() if self.strip else result.assistant_output
        expected = self.expected.strip() if self.strip else self.expected
        if not self.case_sensitive:
            actual = actual.casefold()
            expected = expected.casefold()
        passed = actual == expected
        return _score(
            self.name,
            passed,
            self.required,
            "output exactly matched" if passed else "output did not exactly match",
            {"expected": self.expected},
        )


@dataclass(frozen=True, slots=True)
class ToolUsageScorer:
    required_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    required: bool = True
    name: str = "tool_usage"

    async def score(self, case: EvaluationCase, result: EvaluationRunResult) -> ScoreResult:
        del case
        used = set(result.tool_calls)
        missing = sorted(set(self.required_tools) - used)
        forbidden = sorted(set(self.forbidden_tools) & used)
        passed = not missing and not forbidden
        reason_parts = []
        if missing:
            reason_parts.append(f"missing required tools: {', '.join(missing)}")
        if forbidden:
            reason_parts.append(f"used forbidden tools: {', '.join(forbidden)}")
        return _score(
            self.name,
            passed,
            self.required,
            "; ".join(reason_parts) or "tool usage constraints satisfied",
            {
                "required": list(self.required_tools),
                "forbidden": list(self.forbidden_tools),
                "used": list(result.tool_calls),
            },
        )


@dataclass(frozen=True, slots=True)
class RunStatusScorer:
    accepted: tuple[str, ...] = (RunStatus.COMPLETED.value,)
    required: bool = True
    name: str = "run_status"

    async def score(self, case: EvaluationCase, result: EvaluationRunResult) -> ScoreResult:
        del case
        passed = result.status in self.accepted
        return _score(
            self.name,
            passed,
            self.required,
            f"status {result.status} is accepted"
            if passed
            else f"status {result.status} is not accepted",
            {"accepted": list(self.accepted), "actual": result.status},
        )


@dataclass(frozen=True, slots=True)
class MetricThresholdScorer:
    max_steps: int | None = None
    max_tokens: int | None = None
    max_latency_ms: float | None = None
    max_tool_calls: int | None = None
    required: bool = True
    name: str = "metric_threshold"

    async def score(self, case: EvaluationCase, result: EvaluationRunResult) -> ScoreResult:
        del case
        checks: dict[str, dict[str, int | float | None]] = {}
        failures: list[str] = []
        for name, actual, limit in (
            ("steps", result.step_count, self.max_steps),
            ("tokens", result.total_tokens, self.max_tokens),
            ("latency_ms", result.duration_ms, self.max_latency_ms),
            ("tool_calls", result.tool_call_count, self.max_tool_calls),
        ):
            if limit is None:
                continue
            checks[name] = {"actual": actual, "max": limit}
            if actual is None or actual > limit:
                failures.append(f"{name}={actual} exceeds {limit}")
        return _score(
            self.name,
            not failures,
            self.required,
            "; ".join(failures) or "metric thresholds satisfied",
            checks,
        )


def scorer_from_spec(spec: ScorerSpec) -> Scorer:
    config = spec.config
    if spec.type == "contains":
        return ContainsScorer(
            expected=_strings(
                config.get("expected"), field="contains.expected", require_non_empty=True
            ),
            case_sensitive=bool(config.get("case_sensitive", False)),
            match_all=bool(config.get("match_all", True)),
            required=spec.required,
        )
    if spec.type == "exact_match":
        if "expected" not in config:
            raise ValueError("exact_match.expected is required")
        return ExactMatchScorer(
            expected=str(config["expected"]),
            case_sensitive=bool(config.get("case_sensitive", True)),
            strip=bool(config.get("strip", True)),
            required=spec.required,
        )
    if spec.type == "tool_usage":
        required_tools = _strings(config.get("required_tools", []), field="required_tools")
        forbidden_tools = _strings(config.get("forbidden_tools", []), field="forbidden_tools")
        if not required_tools and not forbidden_tools:
            raise ValueError("tool_usage requires required_tools or forbidden_tools")
        return ToolUsageScorer(
            required_tools=required_tools,
            forbidden_tools=forbidden_tools,
            required=spec.required,
        )
    if spec.type == "run_status":
        return RunStatusScorer(
            accepted=_strings(
                config.get("accepted", config.get("expected", RunStatus.COMPLETED.value)),
                field="run_status.accepted",
            ),
            required=spec.required,
        )
    if spec.type == "metric_threshold":
        thresholds = {
            "max_steps": _optional_int(config.get("max_steps")),
            "max_tokens": _optional_int(config.get("max_tokens")),
            "max_latency_ms": _optional_float(config.get("max_latency_ms")),
            "max_tool_calls": _optional_int(config.get("max_tool_calls")),
        }
        if all(value is None for value in thresholds.values()):
            raise ValueError("metric_threshold requires at least one maximum")
        return MetricThresholdScorer(
            **thresholds,
            required=spec.required,
        )
    raise ValueError(f"unknown evaluation scorer: {spec.type}")


async def score_case(
    case: EvaluationCase,
    result: EvaluationRunResult,
    scorers: list[Scorer],
) -> list[ScoreResult]:
    return [await scorer.score(case, result) for scorer in scorers]


def required_scores_passed(scores: list[ScoreResult]) -> bool:
    return all(score.passed for score in scores if score.required)


def _score(
    name: str,
    passed: bool,
    required: bool,
    reason: str,
    details: dict[str, Any],
) -> ScoreResult:
    return ScoreResult(
        scorer=name,
        passed=passed,
        score=1.0 if passed else 0.0,
        reason=reason,
        required=required,
        details=details,
    )


def _strings(
    value: Any,
    *,
    field: str,
    require_non_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, str):
        result = (value,)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        result = tuple(value)
    else:
        raise ValueError(f"{field} must be a string or list of strings")
    if require_non_empty and (not result or any(not item for item in result)):
        raise ValueError(f"{field} must not be empty")
    return result


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
