from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from axiom.evaluation.models import EvaluationRunResult

from .builder import with_reward
from .models import AgentTrajectory, RewardResult, TerminationReason


@dataclass(frozen=True, slots=True)
class RewardConfig:
    """Configurable reward weights; zero defaults keep v1 outcome-first."""

    verified_outcome_reward: float = 1.0
    evaluation_pass_reward: float = 1.0
    completed_outcome_reward: float = 0.0
    required_tool_reward: float = 0.0
    successful_tool_reward: float = 0.0
    max_tool_reward: float = 0.0
    step_penalty: float = 0.0
    token_penalty_per_1k: float = 0.0
    cost_penalty_per_usd: float = 0.0
    retry_penalty: float = 0.0
    unverified_completion_penalty: float = 0.0
    failed_penalty: float = 0.0
    no_progress_penalty: float = 0.0
    budget_exceeded_penalty: float = 0.0
    deadline_exceeded_penalty: float = 0.0
    cancelled_penalty: float = 0.0
    dependency_failure_penalty: float = 0.0
    evaluation_failure_penalty: float = 0.0

    def __post_init__(self) -> None:
        values = asdict(self)
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("reward weights must be finite")
        non_negative = {
            key: value
            for key, value in values.items()
            if key.endswith("_penalty") or key == "max_tool_reward"
        }
        if any(value < 0 for value in non_negative.values()):
            raise ValueError("penalty magnitudes and max_tool_reward must be non-negative")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class RewardPipeline:
    """Compose deterministic outcome, tool, efficiency, and failure signals."""

    def __init__(self, config: RewardConfig | None = None) -> None:
        self.config = config or RewardConfig()

    def score(
        self,
        trajectory: AgentTrajectory,
        *,
        evaluation_result: EvaluationRunResult | None = None,
    ) -> RewardResult:
        trajectory.validate()
        components = {
            "outcome_reward": self._outcome_reward(trajectory, evaluation_result),
            "tool_reward": self._tool_reward(trajectory, evaluation_result),
            "step_penalty": -self.config.step_penalty * len(trajectory.steps),
            "token_penalty": (
                -self.config.token_penalty_per_1k
                * (_metric_number(trajectory, "total_tokens") / 1000.0)
            ),
            "cost_penalty": (
                -self.config.cost_penalty_per_usd * _metric_decimal(trajectory, "cost_usd")
            ),
            "retry_penalty": (
                -self.config.retry_penalty * _metric_number(trajectory, "retry_count")
            ),
            "failure_penalty": -(
                self._failure_penalty(trajectory.outcome.termination_reason)
                + (
                    self.config.evaluation_failure_penalty
                    if evaluation_result is not None and not evaluation_result.passed
                    else 0.0
                )
            ),
        }
        result = RewardResult(
            total_reward=math.fsum(components.values()),
            components=components,
            configuration_fingerprint=self.config.fingerprint,
        )
        if not math.isfinite(result.total_reward):
            raise ValueError("reward total must be finite")
        return result

    def apply(
        self,
        trajectory: AgentTrajectory,
        *,
        evaluation_result: EvaluationRunResult | None = None,
    ) -> AgentTrajectory:
        return with_reward(
            trajectory,
            self.score(trajectory, evaluation_result=evaluation_result),
        )

    def _outcome_reward(
        self,
        trajectory: AgentTrajectory,
        evaluation: EvaluationRunResult | None,
    ) -> float:
        if evaluation is not None and not evaluation.passed:
            return 0.0
        if trajectory.outcome.completion_verified is True:
            return self.config.verified_outcome_reward
        if evaluation is not None and evaluation.passed:
            return self.config.evaluation_pass_reward
        if trajectory.outcome.success:
            return self.config.completed_outcome_reward
        return 0.0

    def _tool_reward(
        self,
        trajectory: AgentTrajectory,
        evaluation: EvaluationRunResult | None,
    ) -> float:
        # Reward unique successful tools, not calls, and cap the whole process signal.
        successful_tools = {
            str(action.get("tool_name"))
            for step in trajectory.steps
            for action in step.tool_actions
            if action.get("execution_status") == "SUCCEEDED" and action.get("tool_name")
        }
        reward = len(successful_tools) * self.config.successful_tool_reward
        if evaluation is not None:
            tool_usage_passed = any(
                score.scorer == "tool_usage" and score.passed for score in evaluation.scores
            )
            if tool_usage_passed:
                reward += self.config.required_tool_reward
        return min(reward, self.config.max_tool_reward)

    def _failure_penalty(self, reason: TerminationReason) -> float:
        return {
            TerminationReason.VERIFIED_COMPLETION: 0.0,
            TerminationReason.UNVERIFIED_COMPLETION: (
                self.config.unverified_completion_penalty
            ),
            TerminationReason.FAILED: self.config.failed_penalty,
            TerminationReason.NO_PROGRESS: self.config.no_progress_penalty,
            TerminationReason.BUDGET_EXCEEDED: self.config.budget_exceeded_penalty,
            TerminationReason.DEADLINE_EXCEEDED: self.config.deadline_exceeded_penalty,
            TerminationReason.CANCELLED: self.config.cancelled_penalty,
            TerminationReason.DEPENDENCY_FAILURE: self.config.dependency_failure_penalty,
        }[reason]


def _metric_number(trajectory: AgentTrajectory, key: str) -> float:
    value = trajectory.metrics.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _metric_decimal(trajectory: AgentTrajectory, key: str) -> float:
    value = trajectory.metrics.get(key)
    if value is None:
        return 0.0
    try:
        return float(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return 0.0
