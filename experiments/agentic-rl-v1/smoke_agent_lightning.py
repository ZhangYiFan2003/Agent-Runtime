from __future__ import annotations

import json
from pathlib import Path

import agentlightning

from axiom.rl import (
    AgentLightningAdapter,
    AgentLightningEncodedStep,
    AgentTrajectory,
    RewardResult,
    TerminationReason,
    TrajectoryObservation,
    TrajectoryOutcome,
    TrajectoryStep,
)


class ExactCapture:
    """Stand-in for exact IDs captured by the policy-serving boundary."""

    def encode_step(self, trajectory, step):
        del trajectory
        if step.step_index == 0:
            return AgentLightningEncodedStep(
                prompt_token_ids=(10, 11),
                response_token_ids=(20,),
                response_log_probs=(-0.25,),
            )
        # 30 and 31 are the tool/environment observation appended between calls.
        return AgentLightningEncodedStep(
            prompt_token_ids=(10, 11, 20, 30, 31),
            response_token_ids=(21,),
            response_log_probs=(-0.5,),
        )


def _trajectory() -> AgentTrajectory:
    observation = TrajectoryObservation(
        messages=({"role": "user", "content": "find Symbol"},),
        fingerprint="sha256:exact-observation",
        source="serving_capture",
        exact=True,
    )
    first = TrajectoryStep(
        trajectory_id="trajectory-smoke",
        run_id="run-smoke",
        parent_run_id=None,
        step_index=0,
        observation=observation,
        action={"tool_call": {"name": "search_repository"}},
        model_call_id="request-1",
    )
    second = TrajectoryStep(
        trajectory_id="trajectory-smoke",
        run_id="run-smoke",
        parent_run_id=None,
        step_index=1,
        observation=observation,
        action={"content": "answer"},
        model_call_id="request-2",
        done=True,
        termination_reason=TerminationReason.VERIFIED_COMPLETION,
    )
    return AgentTrajectory(
        trajectory_id="trajectory-smoke",
        run_id="run-smoke",
        trace_id="trace-smoke",
        thread_id="thread-smoke",
        turn_id="turn-smoke",
        parent_run_id=None,
        parent_step_id=None,
        run_kind="ROOT",
        execution_strategy="react",
        steps=(first, second),
        outcome=TrajectoryOutcome(
            status="COMPLETED",
            done=True,
            success=True,
            termination_reason=TerminationReason.VERIFIED_COMPLETION,
            completion_verified=True,
            verification_status="VERIFIED",
        ),
        metrics={},
        provenance={
            "prompt_version": "sha256:prompt",
            "tool_schema_version": "sha256:tools",
        },
        reward=RewardResult(
            total_reward=1.0,
            components={"outcome_reward": 1.0},
            configuration_fingerprint="sha256:reward",
        ),
    )


def main() -> None:
    adapter = AgentLightningAdapter(ExactCapture())
    trajectory = _trajectory()
    events = adapter.to_native_events(trajectory)
    audit = adapter.audit_trajectory_aggregation(trajectory)
    assert [event.event_type for event in events] == [
        "model_request",
        "model_request",
        "reward",
    ]
    assert audit.response_token_ids == (20, 30, 31, 21)
    assert audit.response_mask == (1, 0, 0, 1)
    assert events[-1].data["value"] == 1.0
    triplet_public = hasattr(agentlightning, "Triplet")
    assert triplet_public is False
    result = {
        "agent_lightning": agentlightning.__version__,
        "native_event_class": type(events[0]).__name__,
        "event_types": [event.event_type for event in events],
        "public_triplet": triplet_public,
        "aggregated_response_token_ids": list(audit.response_token_ids),
        "aggregated_response_mask": list(audit.response_mask),
        "policy_token_count": audit.policy_token_count,
        "observation_token_count": audit.observation_token_count,
        "reward": events[-1].data["value"],
        "passed": True,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    output = Path(__file__).resolve().parent / "results" / "agent_lightning_compatibility.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")


if __name__ == "__main__":
    main()
