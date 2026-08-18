from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from axiom.evaluation.models import EvaluationSuiteResult


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_dataset": self.old_dataset,
            "new_dataset": self.new_dataset,
            "metric_changes": [change.to_dict() for change in self.metric_changes],
            "regressions": list(self.regressions),
            "improvements": list(self.improvements),
            "performance_warnings": [warning.to_dict() for warning in self.performance_warnings],
        }


def compare_results(
    old: EvaluationSuiteResult,
    new: EvaluationSuiteResult,
    *,
    token_warning_percent: float = 20.0,
    latency_warning_percent: float = 30.0,
    step_warning_delta: float = 2.0,
) -> EvaluationComparison:
    old_cases = {result.case_id: result for result in old.results}
    new_cases = {result.case_id: result for result in new.results}
    shared = sorted(old_cases.keys() & new_cases.keys())
    regressions = tuple(
        case_id for case_id in shared if old_cases[case_id].passed and not new_cases[case_id].passed
    )
    improvements = tuple(
        case_id for case_id in shared if not old_cases[case_id].passed and new_cases[case_id].passed
    )
    changes = (
        _change("pass_rate", old.pass_rate, new.pass_rate),
        _change("avg_tokens", old.avg_tokens, new.avg_tokens),
        _change("avg_latency_ms", old.avg_latency_ms, new.avg_latency_ms),
        _change("avg_steps", old.avg_steps, new.avg_steps),
    )
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
    return EvaluationComparison(
        old_dataset=old.dataset,
        new_dataset=new.dataset,
        metric_changes=changes,
        regressions=regressions,
        improvements=improvements,
        performance_warnings=tuple(warnings),
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
