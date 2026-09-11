from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .models import AgentTrajectory, TrajectoryStep


class AgentLightningAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AgentLightningEncodedStep:
    """Token IDs captured by the server that actually sampled the policy action."""

    prompt_token_ids: tuple[int, ...]
    response_token_ids: tuple[int, ...]
    response_log_probs: tuple[float, ...] | None = None
    exact: bool = True


class AgentLightningTokenEncoder(Protocol):
    """Lookup boundary for exact, server-captured token data.

    Implementations should normally resolve ``step.model_call_id`` against the
    inference server's request log. Retokenizing Axiom messages is not exact and
    must return ``exact=False``.
    """

    def encode_step(
        self,
        trajectory: AgentTrajectory,
        step: TrajectoryStep,
    ) -> AgentLightningEncodedStep: ...


@dataclass(frozen=True, slots=True)
class AgentLightningEventRecord:
    event_type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"event_type": self.event_type, "data": dict(self.data)}


@dataclass(frozen=True, slots=True)
class AgentLightningAggregationAudit:
    prompt_token_ids: tuple[int, ...]
    response_token_ids: tuple[int, ...]
    response_mask: tuple[int, ...]
    policy_token_count: int
    observation_token_count: int


class AgentLightningAdapter:
    """Translate Axiom rollouts to Agent Lightning 1.0 event-server inputs.

    Agent Lightning 1.0.1 has no public ``Triplet`` construction API. Its veRL
    rollout manager reads ``model_request`` and ``reward`` events, constructs
    internal triplets, and performs trajectory-level aggregation. This adapter
    therefore emits the public ``EventCreate`` boundary and refuses retokenized
    on-policy data by default.
    """

    def __init__(
        self,
        encoder: AgentLightningTokenEncoder,
        *,
        require_exact_observations: bool = True,
        require_exact_token_ids: bool = True,
    ) -> None:
        self.encoder = encoder
        self.require_exact_observations = require_exact_observations
        self.require_exact_token_ids = require_exact_token_ids

    def _encode(self, trajectory: AgentTrajectory) -> tuple[AgentLightningEncodedStep, ...]:
        trajectory.validate()
        if trajectory.execution_strategy != "react":
            raise AgentLightningAdapterError(
                "Agent Lightning v1 adapter currently supports ReAct trajectories only"
            )
        if not trajectory.steps:
            raise AgentLightningAdapterError("trajectory has no policy action to train")
        if trajectory.reward is None:
            raise AgentLightningAdapterError("trajectory must be rewarded before adaptation")
        missing_context = {
            "prompt_version",
            "tool_schema_version",
        } - trajectory.provenance.keys()
        if missing_context:
            raise AgentLightningAdapterError(
                "trainer context fingerprints are missing: "
                + ", ".join(sorted(missing_context))
            )
        if self.require_exact_observations and any(
            not step.observation.exact for step in trajectory.steps
        ):
            raise AgentLightningAdapterError(
                "lossy observation projection cannot be adapted for on-policy training"
            )

        encoded = tuple(self.encoder.encode_step(trajectory, step) for step in trajectory.steps)
        for step, item in zip(trajectory.steps, encoded, strict=True):
            if not item.prompt_token_ids or not item.response_token_ids:
                raise AgentLightningAdapterError(
                    f"token encoder returned an empty sequence for step {step.step_index}"
                )
            if self.require_exact_token_ids and not item.exact:
                raise AgentLightningAdapterError(
                    "retokenized or reconstructed token IDs cannot be used for on-policy training"
                )
            if item.response_log_probs is not None and len(item.response_log_probs) != len(
                item.response_token_ids
            ):
                raise AgentLightningAdapterError(
                    f"response log-probability length mismatch for step {step.step_index}"
                )
        return encoded

    def audit_trajectory_aggregation(
        self,
        trajectory: AgentTrajectory,
    ) -> AgentLightningAggregationAudit:
        """Mirror Agent Lightning 1.0.1 trajectory aggregation invariants."""

        encoded = self._encode(trajectory)
        first = encoded[0]
        prompt_ids = list(first.prompt_token_ids)
        response_ids = list(first.response_token_ids)
        response_mask = [1] * len(response_ids)
        context = prompt_ids + response_ids

        for index, item in enumerate(encoded[1:], start=1):
            next_prompt = list(item.prompt_token_ids)
            if next_prompt[: len(context)] != context:
                raise AgentLightningAdapterError(
                    "Agent Lightning trajectory aggregation would split the rollout at "
                    f"step {trajectory.steps[index].step_index}: next prompt is not an exact "
                    "prefix extension of the prior prompt plus policy response"
                )
            observation_ids = next_prompt[len(context) :]
            response_ids.extend(observation_ids)
            response_mask.extend([0] * len(observation_ids))
            response_ids.extend(item.response_token_ids)
            response_mask.extend([1] * len(item.response_token_ids))
            context = next_prompt + list(item.response_token_ids)

        return AgentLightningAggregationAudit(
            prompt_token_ids=tuple(prompt_ids),
            response_token_ids=tuple(response_ids),
            response_mask=tuple(response_mask),
            policy_token_count=sum(response_mask),
            observation_token_count=len(response_mask) - sum(response_mask),
        )

    def to_event_records(
        self,
        trajectory: AgentTrajectory,
    ) -> tuple[AgentLightningEventRecord, ...]:
        encoded = self._encode(trajectory)
        # Reject a rollout that Agent Lightning would silently split into more
        # than one training row in its recommended trajectory aggregation mode.
        self.audit_trajectory_aggregation(trajectory)

        records: list[AgentLightningEventRecord] = []
        for step, item in zip(trajectory.steps, encoded, strict=True):
            records.append(
                AgentLightningEventRecord(
                    event_type="model_request",
                    data={
                        "status": "ok",
                        "http_status": 200,
                        "prompt_token_ids": list(item.prompt_token_ids),
                        "response_token_ids": list(item.response_token_ids),
                        "response_log_probs": (
                            list(item.response_log_probs)
                            if item.response_log_probs is not None
                            else None
                        ),
                        "server": {
                            "response_id": step.model_call_id,
                            "agent_name": "axiom-react",
                            "trajectory_id": trajectory.trajectory_id,
                            "run_id": trajectory.run_id,
                            "trace_id": trajectory.trace_id,
                            "step_index": step.step_index,
                        },
                    },
                )
            )
        assert trajectory.reward is not None
        records.append(
            AgentLightningEventRecord(
                event_type="reward",
                data={
                    "value": trajectory.reward.total_reward,
                    "source": "axiom",
                    "reason": trajectory.outcome.termination_reason.value,
                },
            )
        )
        return tuple(records)

    def to_native_events(self, trajectory: AgentTrajectory) -> tuple[Any, ...]:
        """Create Agent Lightning 1.x ``EventCreate`` objects when installed."""

        try:
            from agentlightning.schemas import EventCreate
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Agent Lightning is optional; install Axiom with the 'rl' extra"
            ) from exc
        return tuple(
            EventCreate(**record.to_dict()) for record in self.to_event_records(trajectory)
        )
