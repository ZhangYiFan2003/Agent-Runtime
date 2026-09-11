from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

TRAJECTORY_SCHEMA_VERSION = 1


class TerminationReason(StrEnum):
    VERIFIED_COMPLETION = "VERIFIED_COMPLETION"
    UNVERIFIED_COMPLETION = "UNVERIFIED_COMPLETION"
    FAILED = "FAILED"
    NO_PROGRESS = "NO_PROGRESS"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    CANCELLED = "CANCELLED"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"


@dataclass(frozen=True, slots=True)
class TrajectoryObservation:
    messages: tuple[dict[str, Any], ...]
    fingerprint: str
    source: str = "checkpoint_messages"
    exact: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "messages": [_json_value(item) for item in self.messages],
            "fingerprint": self.fingerprint,
            "source": self.source,
            "exact": self.exact,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryObservation:
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list):
            raise ValueError("trajectory observation messages must be a list")
        return cls(
            messages=tuple(_json_dict(item) for item in raw_messages),
            fingerprint=_required_text(data, "fingerprint"),
            source=str(data.get("source") or "checkpoint_messages"),
            exact=bool(data.get("exact", True)),
        )


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    trajectory_id: str
    run_id: str
    parent_run_id: str | None
    step_index: int
    observation: TrajectoryObservation
    action: dict[str, Any]
    tool_actions: tuple[dict[str, Any], ...] = ()
    tool_observations: tuple[dict[str, Any], ...] = ()
    model: str | None = None
    model_call_id: str | None = None
    input_token_count: int = 0
    output_token_count: int = 0
    cost_usd: str | None = None
    started_at: str | None = None
    latency_ms: float | None = None
    done: bool = False
    termination_reason: TerminationReason | None = None
    reward_components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trajectory_id": self.trajectory_id,
            "run_id": self.run_id,
            "parent_run_id": self.parent_run_id,
            "step_index": self.step_index,
            "observation": self.observation.to_dict(),
            "action": _json_dict(self.action),
            "tool_actions": [_json_dict(item) for item in self.tool_actions],
            "tool_observations": [_json_dict(item) for item in self.tool_observations],
            "model": self.model,
            "model_call_id": self.model_call_id,
            "input_token_count": self.input_token_count,
            "output_token_count": self.output_token_count,
            "cost_usd": self.cost_usd,
            "started_at": self.started_at,
            "latency_ms": self.latency_ms,
            "done": self.done,
            "termination_reason": (
                self.termination_reason.value if self.termination_reason else None
            ),
            "reward_components": dict(self.reward_components),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryStep:
        raw_actions = data.get("tool_actions") or []
        raw_observations = data.get("tool_observations") or []
        reason = data.get("termination_reason")
        return cls(
            trajectory_id=_required_text(data, "trajectory_id"),
            run_id=_required_text(data, "run_id"),
            parent_run_id=_optional_text(data.get("parent_run_id")),
            step_index=int(data.get("step_index") or 0),
            observation=TrajectoryObservation.from_dict(_json_dict(data.get("observation"))),
            action=_json_dict(data.get("action")),
            tool_actions=tuple(_json_dict(item) for item in raw_actions),
            tool_observations=tuple(_json_dict(item) for item in raw_observations),
            model=_optional_text(data.get("model")),
            model_call_id=_optional_text(data.get("model_call_id")),
            input_token_count=int(data.get("input_token_count") or 0),
            output_token_count=int(data.get("output_token_count") or 0),
            cost_usd=_optional_text(data.get("cost_usd")),
            started_at=_optional_text(data.get("started_at")),
            latency_ms=_optional_float(data.get("latency_ms")),
            done=bool(data.get("done")),
            termination_reason=TerminationReason(str(reason)) if reason else None,
            reward_components={
                str(key): float(value)
                for key, value in _json_dict(data.get("reward_components")).items()
            },
        )


@dataclass(frozen=True, slots=True)
class TrajectoryOutcome:
    status: str
    done: bool
    success: bool
    termination_reason: TerminationReason
    completion_verified: bool | None = None
    verification_status: str = "NOT_APPLICABLE"
    evaluation_passed: bool | None = None
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "done": self.done,
            "success": self.success,
            "termination_reason": self.termination_reason.value,
            "completion_verified": self.completion_verified,
            "verification_status": self.verification_status,
            "evaluation_passed": self.evaluation_passed,
            "error_type": self.error_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrajectoryOutcome:
        return cls(
            status=_required_text(data, "status"),
            done=bool(data.get("done")),
            success=bool(data.get("success")),
            termination_reason=TerminationReason(_required_text(data, "termination_reason")),
            completion_verified=(
                data.get("completion_verified")
                if isinstance(data.get("completion_verified"), bool)
                else None
            ),
            verification_status=str(data.get("verification_status") or "NOT_APPLICABLE"),
            evaluation_passed=(
                data.get("evaluation_passed")
                if isinstance(data.get("evaluation_passed"), bool)
                else None
            ),
            error_type=_optional_text(data.get("error_type")),
        )


@dataclass(frozen=True, slots=True)
class RewardResult:
    total_reward: float
    components: dict[str, float]
    configuration_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_reward": self.total_reward,
            "components": dict(self.components),
            "configuration_fingerprint": self.configuration_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RewardResult:
        return cls(
            total_reward=float(data.get("total_reward") or 0.0),
            components={
                str(key): float(value)
                for key, value in _json_dict(data.get("components")).items()
            },
            configuration_fingerprint=_required_text(data, "configuration_fingerprint"),
        )


@dataclass(frozen=True, slots=True)
class AgentTrajectory:
    trajectory_id: str
    run_id: str
    trace_id: str
    thread_id: str
    turn_id: str
    parent_run_id: str | None
    parent_step_id: str | None
    run_kind: str
    execution_strategy: str
    steps: tuple[TrajectoryStep, ...]
    outcome: TrajectoryOutcome
    metrics: dict[str, Any]
    provenance: dict[str, Any]
    reward: RewardResult | None = None
    child_run_ids: tuple[str, ...] = ()
    schema_version: int = TRAJECTORY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != TRAJECTORY_SCHEMA_VERSION:
            raise ValueError(f"unsupported trajectory schema version: {self.schema_version}")
        if not self.trajectory_id or not self.run_id or not self.trace_id:
            raise ValueError("trajectory, run, and trace identifiers are required")
        indexes = [step.step_index for step in self.steps]
        if indexes != sorted(indexes) or len(indexes) != len(set(indexes)):
            raise ValueError("trajectory step indexes must be strictly ordered and unique")
        if any(step.trajectory_id != self.trajectory_id for step in self.steps):
            raise ValueError("trajectory step identifier does not match its episode")
        if any(step.run_id != self.run_id for step in self.steps):
            raise ValueError("trajectory step run identifier does not match its episode")
        if not self.outcome.done:
            raise ValueError("exported trajectories must have a terminal outcome")
        done_steps = [step for step in self.steps if step.done]
        if self.steps and (len(done_steps) != 1 or done_steps[0] is not self.steps[-1]):
            raise ValueError("only the final trajectory step may be terminal")
        if self.steps and self.steps[-1].termination_reason != self.outcome.termination_reason:
            raise ValueError("step and episode termination reasons differ")
        call_ids = {
            str(action.get("tool_call_id"))
            for step in self.steps
            for action in step.tool_actions
            if action.get("tool_call_id")
        }
        observation_ids = {
            str(observation.get("tool_call_id"))
            for step in self.steps
            for observation in step.tool_observations
            if observation.get("tool_call_id")
        }
        if not observation_ids <= call_ids:
            raise ValueError("tool observations must reference a trajectory tool action")
        if self.reward is not None:
            values = [self.reward.total_reward, *self.reward.components.values()]
            if not all(math.isfinite(value) for value in values):
                raise ValueError("trajectory rewards must be finite")
            if not math.isclose(
                math.fsum(self.reward.components.values()),
                self.reward.total_reward,
                abs_tol=1e-9,
            ):
                raise ValueError("trajectory reward components do not sum to total reward")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "trajectory_id": self.trajectory_id,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "parent_run_id": self.parent_run_id,
            "parent_step_id": self.parent_step_id,
            "child_run_ids": list(self.child_run_ids),
            "run_kind": self.run_kind,
            "execution_strategy": self.execution_strategy,
            "steps": [step.to_dict() for step in self.steps],
            "outcome": self.outcome.to_dict(),
            "metrics": _json_dict(self.metrics),
            "provenance": _json_dict(self.provenance),
            "reward": self.reward.to_dict() if self.reward else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTrajectory:
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("trajectory steps must be a list")
        raw_children = data.get("child_run_ids") or []
        trajectory = cls(
            schema_version=int(data.get("schema_version") or 0),
            trajectory_id=_required_text(data, "trajectory_id"),
            run_id=_required_text(data, "run_id"),
            trace_id=_required_text(data, "trace_id"),
            thread_id=_required_text(data, "thread_id"),
            turn_id=_required_text(data, "turn_id"),
            parent_run_id=_optional_text(data.get("parent_run_id")),
            parent_step_id=_optional_text(data.get("parent_step_id")),
            child_run_ids=tuple(str(item) for item in raw_children),
            run_kind=str(data.get("run_kind") or "agent"),
            execution_strategy=str(data.get("execution_strategy") or "react"),
            steps=tuple(TrajectoryStep.from_dict(item) for item in raw_steps),
            outcome=TrajectoryOutcome.from_dict(_json_dict(data.get("outcome"))),
            metrics=_json_dict(data.get("metrics")),
            provenance=_json_dict(data.get("provenance")),
            reward=(
                RewardResult.from_dict(data["reward"])
                if isinstance(data.get("reward"), dict)
                else None
            ),
        )
        trajectory.validate()
        return trajectory


def write_trajectories_jsonl(path: str | Path, trajectories: list[AgentTrajectory]) -> None:
    from .builder import validate_export_payload

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for item in trajectories:
        payload = item.to_dict()
        validate_export_payload(payload)
        lines.append(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        )
    target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def read_trajectories_jsonl(path: str | Path) -> tuple[AgentTrajectory, ...]:
    trajectories: list[AgentTrajectory] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid trajectory JSONL at line {line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"trajectory JSONL line {line_number} must be an object")
        trajectories.append(AgentTrajectory.from_dict(value))
    return tuple(trajectories)


def _required_text(data: dict[str, Any], key: str) -> str:
    value = str(data.get(key) or "").strip()
    if not value:
        raise ValueError(f"trajectory field {key} is required")
    return value


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _json_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)
