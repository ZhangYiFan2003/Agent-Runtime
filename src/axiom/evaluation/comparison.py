from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from axiom.evaluation.models import EvaluationCaseAggregate, EvaluationSuiteResult


@dataclass(frozen=True, slots=True)
class MetricChange:
    metric: str
    old: float
    new: float
    delta: float
    percent_change: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "old": self.old,
            "new": self.new,
            "delta": self.delta,
            "percent_change": self.percent_change,
        }


@dataclass(frozen=True, slots=True)
class PerformanceWarning:
    metric: str
    message: str
    old: float
    new: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "message": self.message,
            "old": self.old,
            "new": self.new,
        }


@dataclass(frozen=True, slots=True)
class EvaluationComparison:
    old_dataset: str
    new_dataset: str
    metric_changes: tuple[MetricChange, ...]
    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    performance_warnings: tuple[PerformanceWarning, ...]
    stochastic_regressions: tuple[str, ...] = ()
    stochastic_improvements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_dataset": self.old_dataset,
            "new_dataset": self.new_dataset,
            "metric_changes": [change.to_dict() for change in self.metric_changes],
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
            "performance_warnings": [warning.to_dict() for warning in self.performance_warnings],
            "stochastic_regressions": list(self.stochastic_regressions),
            "stochastic_improvements": list(self.stochastic_improvements),
        }


@dataclass(frozen=True, slots=True)
class RegressionThresholds:
    max_success_rate_drop: float = 0.10
    max_token_increase_ratio: float | None = None
    max_latency_increase_ratio: float | None = None
    max_step_increase: float | None = None
    max_cost_per_success_increase_ratio: float | None = None
    fail_new_required_case_failure: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_success_rate_drop",
            "max_token_increase_ratio",
            "max_latency_increase_ratio",
            "max_step_increase",
            "max_cost_per_success_increase_ratio",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")


class RegressionGateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class RegressionGateResult:
    status: RegressionGateStatus
    failures: tuple[str, ...]
    warnings: tuple[str, ...]
    comparison: EvaluationComparison

    @property
    def passed(self) -> bool:
        return self.status == RegressionGateStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "passed": self.passed,
            "failures": list(self.failures),
            "warnings": list(self.warnings),
            "comparison": self.comparison.to_dict(),
        }


def compare_results(
    old: EvaluationSuiteResult,
    new: EvaluationSuiteResult,
    *,
    token_warning_percent: float = 20.0,
    latency_warning_percent: float = 30.0,
    step_warning_delta: float = 2.0,
    cost_warning_percent: float = 20.0,
) -> EvaluationComparison:
    old_cases = _aggregates(old)
    new_cases = _aggregates(new)
    shared = sorted(old_cases.keys() & new_cases.keys())
    regressions = tuple(
        case_id
        for case_id in shared
        if old_cases[case_id].trial_success_rate == 1.0
        and new_cases[case_id].trial_success_rate == 0.0
    )
    improvements = tuple(
        case_id
        for case_id in shared
        if old_cases[case_id].trial_success_rate == 0.0
        and new_cases[case_id].trial_success_rate == 1.0
    )
    stochastic_regressions = tuple(
        case_id
        for case_id in shared
        if old_cases[case_id].trial_success_rate > new_cases[case_id].trial_success_rate
        and case_id not in regressions
    )
    stochastic_improvements = tuple(
        case_id
        for case_id in shared
        if old_cases[case_id].trial_success_rate < new_cases[case_id].trial_success_rate
        and case_id not in improvements
    )
    changes = [
        _change("pass_rate", old.pass_rate, new.pass_rate),
        _change("trial_success_rate", old.trial_success_rate, new.trial_success_rate),
        _change("avg_tokens", old.avg_tokens, new.avg_tokens),
        _change("avg_latency_ms", old.avg_latency_ms, new.avg_latency_ms),
        _change("avg_steps", old.avg_steps, new.avg_steps),
    ]
    old_cost = _optional_metric(old.cost_per_success)
    new_cost = _optional_metric(new.cost_per_success)
    if old_cost is not None and new_cost is not None:
        changes.append(_change("cost_per_success", old_cost, new_cost))
    warnings: list[PerformanceWarning] = []
    if _increase_percent(old.avg_tokens, new.avg_tokens) > token_warning_percent:
        warnings.append(
            PerformanceWarning(
                metric="avg_tokens",
                message=f"average tokens increased by more than {token_warning_percent:g}%",
                old=old.avg_tokens,
                new=new.avg_tokens,
            )
        )
    if _increase_percent(old.avg_latency_ms, new.avg_latency_ms) > latency_warning_percent:
        warnings.append(
            PerformanceWarning(
                metric="avg_latency_ms",
                message=f"average latency increased by more than {latency_warning_percent:g}%",
                old=old.avg_latency_ms,
                new=new.avg_latency_ms,
            )
        )
    if new.avg_steps - old.avg_steps > step_warning_delta:
        warnings.append(
            PerformanceWarning(
                metric="avg_steps",
                message=f"average steps increased by more than {step_warning_delta:g}",
                old=old.avg_steps,
                new=new.avg_steps,
            )
        )
    if (
        old_cost is not None
        and new_cost is not None
        and _increase_percent(old_cost, new_cost) > cost_warning_percent
    ):
        warnings.append(
            PerformanceWarning(
                metric="cost_per_success",
                message=(
                    f"cost per successful trial increased by more than {cost_warning_percent:g}%"
                ),
                old=old_cost,
                new=new_cost,
            )
        )
    return EvaluationComparison(
        old_dataset=old.dataset,
        new_dataset=new.dataset,
        metric_changes=tuple(changes),
        regressions=regressions,
        improvements=improvements,
        performance_warnings=tuple(warnings),
        stochastic_regressions=stochastic_regressions,
        stochastic_improvements=stochastic_improvements,
    )


def evaluate_regression_gate(
    baseline: EvaluationSuiteResult,
    candidate: EvaluationSuiteResult,
    *,
    thresholds: RegressionThresholds | None = None,
) -> RegressionGateResult:
    policy = thresholds or RegressionThresholds()
    comparison = compare_results(baseline, candidate)
    failures = [f"hard functional regression: {case_id}" for case_id in comparison.regressions]
    old_cases = _aggregates(baseline)
    new_cases = _aggregates(candidate)
    if policy.fail_new_required_case_failure:
        for case_id in sorted(new_cases.keys() - old_cases.keys()):
            if new_cases[case_id].failure_count:
                failures.append(f"new required case failed: {case_id}")
    for case_id in sorted(old_cases.keys() & new_cases.keys()):
        drop = old_cases[case_id].trial_success_rate - new_cases[case_id].trial_success_rate
        if drop > policy.max_success_rate_drop and case_id not in comparison.regressions:
            failures.append(
                f"trial success-rate regression: {case_id} dropped by {drop:.4f} "
                f"(limit {policy.max_success_rate_drop:.4f})"
            )
    if (
        policy.max_token_increase_ratio is not None
        and _increase_ratio(baseline.avg_tokens, candidate.avg_tokens)
        > policy.max_token_increase_ratio
    ):
        failures.append(
            "token regression: average tokens increased beyond "
            f"{policy.max_token_increase_ratio:.4f}"
        )
    baseline_cost = _optional_metric(baseline.cost_per_success)
    candidate_cost = _optional_metric(candidate.cost_per_success)
    if (
        policy.max_cost_per_success_increase_ratio is not None
        and baseline_cost is not None
        and candidate_cost is not None
        and _increase_ratio(baseline_cost, candidate_cost)
        > policy.max_cost_per_success_increase_ratio
    ):
        failures.append(
            "cost-per-success regression: cost increased beyond "
            f"{policy.max_cost_per_success_increase_ratio:.4f}"
        )
    if policy.max_step_increase is not None and (
        candidate.avg_steps - baseline.avg_steps > policy.max_step_increase
    ):
        failures.append(
            f"step regression: average steps increased beyond {policy.max_step_increase:g}"
        )
    if (
        policy.max_latency_increase_ratio is not None
        and _increase_ratio(baseline.avg_latency_ms, candidate.avg_latency_ms)
        > policy.max_latency_increase_ratio
    ):
        failures.append(
            "latency regression: average latency increased beyond "
            f"{policy.max_latency_increase_ratio:.4f}"
        )
    warnings = [warning.message for warning in comparison.performance_warnings]
    warnings.extend(
        f"stochastic quality change: {case_id} had a lower trial success rate"
        for case_id in comparison.stochastic_regressions
        if not any(case_id in failure for failure in failures)
    )
    return RegressionGateResult(
        status=RegressionGateStatus.FAIL if failures else RegressionGateStatus.PASS,
        failures=tuple(failures),
        warnings=tuple(warnings),
        comparison=comparison,
    )


def _change(metric: str, old: float, new: float) -> MetricChange:
    delta = round(new - old, 4)
    percent = round(delta / old * 100, 2) if old else None
    return MetricChange(
        metric=metric,
        old=old,
        new=new,
        delta=delta,
        percent_change=percent,
    )


def _increase_percent(old: float, new: float) -> float:
    if old <= 0:
        return 0.0
    return (new - old) / old * 100


def _increase_ratio(old: float, new: float) -> float:
    if old <= 0:
        return 0.0 if new <= 0 else float("inf")
    return (new - old) / old


def _optional_metric(value: str | None) -> float | None:
    return float(value) if value is not None else None


def _aggregates(suite: EvaluationSuiteResult) -> dict[str, EvaluationCaseAggregate]:
    if suite.case_aggregates:
        return {item.case_id: item for item in suite.case_aggregates}
    case_ids = list(dict.fromkeys(result.case_id for result in suite.results))
    return {
        case_id: EvaluationCaseAggregate.create(
            case_id, [result for result in suite.results if result.case_id == case_id]
        )
        for case_id in case_ids
    }
