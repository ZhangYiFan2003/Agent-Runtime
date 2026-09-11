from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from axiom.entrypoints.cli import app
from axiom.evaluation import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunner,
    EvaluationRunResult,
)
from axiom.rl import (
    AgentLightningAdapter,
    AgentLightningAdapterError,
    AgentLightningEncodedStep,
    RewardPipeline,
    RLRolloutRunner,
    RolloutDataset,
)
from axiom.runtime import (
    Checkpoint,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    RunStatus,
    Span,
    SpanStatus,
    SpanType,
    Trace,
)
from axiom.types import Message

NOW = "2026-01-01T00:00:00+00:00"
LATER = "2026-01-01T00:00:01+00:00"


class _PersistingExecutor:
    def __init__(self, checkpoints, observations) -> None:
        self.checkpoints = checkpoints
        self.observations = observations
        self.calls = 0

    async def execute(self, case: EvaluationCase) -> EvaluationRunResult:
        self.calls += 1
        run_id = f"run-{self.calls}"
        trace_id = f"trace-{self.calls}"
        thread_id = f"thread-{self.calls}"
        checkpoint = Checkpoint(
            run_id=run_id,
            thread_id=thread_id,
            turn_id=f"turn-{self.calls}",
            input=case.prompt,
            messages=[
                Message(role="user", content=case.prompt),
                Message(role="assistant", content="verified answer"),
            ],
            status=RunStatus.COMPLETED,
        )
        await self.checkpoints.save(checkpoint)
        trace = Trace(
            trace_id=trace_id,
            run_id=run_id,
            thread_id=thread_id,
            turn_id=checkpoint.turn_id,
            started_at=NOW,
            ended_at=LATER,
            status=RunStatus.COMPLETED.value,
        )
        spans = [
            Span(
                span_id=f"root-{self.calls}",
                trace_id=trace_id,
                span_type=SpanType.AGENT,
                name="run",
                started_at=NOW,
                ended_at=LATER,
                status=SpanStatus.SUCCEEDED,
            ),
            Span(
                span_id=f"step-{self.calls}",
                trace_id=trace_id,
                span_type=SpanType.AGENT,
                name="agent.step",
                started_at=NOW,
                ended_at=LATER,
                status=SpanStatus.SUCCEEDED,
                parent_span_id=f"root-{self.calls}",
                attributes={"step_index": 0},
            ),
            Span(
                span_id=f"llm-{self.calls}",
                trace_id=trace_id,
                span_type=SpanType.LLM,
                name="llm.chat",
                started_at=NOW,
                ended_at=LATER,
                status=SpanStatus.SUCCEEDED,
                parent_span_id=f"step-{self.calls}",
                attributes={
                    "model": "model-a",
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "context.compaction_triggered": False,
                    "context.tool_results_projected": 0,
                },
            ),
        ]
        await self.observations.save_trace(trace)
        for span in spans:
            await self.observations.save_span(span)
        return EvaluationRunResult(
            case_id=case.id,
            run_id=run_id,
            thread_id=thread_id,
            turn_id=checkpoint.turn_id,
            trace_id=trace_id,
            status=RunStatus.COMPLETED.value,
            assistant_output="verified answer",
            duration_ms=1000,
            prompt_tokens=8,
            completion_tokens=2,
            total_tokens=10,
            tool_calls=[],
            step_count=1,
            attribution={
                "runtime_version": "0.1.0",
                "model_configuration_hash": "sha256:model",
                "prompt_version": "sha256:prompt",
                "tool_schema_version": "sha256:tools",
            },
        )


class _Encoder:
    def encode_step(self, trajectory, step) -> AgentLightningEncodedStep:
        assert step.observation.messages
        assert step.action["content"] == "verified answer"
        return AgentLightningEncodedStep(
            prompt_token_ids=(1, 2, 3),
            response_token_ids=(4, 5),
            response_log_probs=(-0.1, -0.2),
        )


async def _collect(*, trials: int = 1, split: str = "train") -> RolloutDataset:
    checkpoints = MemoryCheckpointStore()
    observations = MemoryObservabilityStore()
    executor = _PersistingExecutor(checkpoints, observations)
    evaluator = EvaluationRunner(executor)
    runner = RLRolloutRunner(
        evaluator,
        runtime_store=checkpoints,
        observability_store=observations,
        reward_pipeline=RewardPipeline(),
    )
    dataset = EvaluationDataset(
        name="coding-tasks",
        version="1",
        cases=(EvaluationCase(id="symbol", prompt="find the symbol"),),
    )
    return await runner.collect(dataset, trials=trials, split=split)


def test_repeated_runs_produce_independent_episode_ids() -> None:
    dataset = asyncio.run(_collect(trials=2))
    ids = [item.trajectory_id for item in dataset.trajectories]
    assert len(ids) == 2
    assert len(set(ids)) == 2


def test_evaluation_case_is_reused_as_rollout_task() -> None:
    dataset = asyncio.run(_collect())
    trajectory = dataset.trajectories[0]
    assert trajectory.provenance["dataset_task_id"] == "symbol"
    assert trajectory.outcome.evaluation_passed is True
    assert trajectory.reward is not None
    assert dataset.summary()["valid_trajectories"] == 1


def test_runtime_model_prompt_tool_and_reward_fingerprints_are_preserved() -> None:
    trajectory = asyncio.run(_collect()).trajectories[0]
    assert trajectory.provenance["runtime_version"] == "0.1.0"
    assert trajectory.provenance["model_configuration_hash"] == "sha256:model"
    assert trajectory.provenance["prompt_version"] == "sha256:prompt"
    assert trajectory.provenance["tool_schema_version"] == "sha256:tools"
    assert trajectory.provenance["reward_configuration"].startswith("sha256:")


def test_held_out_split_metadata_is_preserved() -> None:
    trajectory = asyncio.run(_collect(split="held_out")).trajectories[0]
    assert trajectory.provenance["split"] == "held_out"
    assert trajectory.provenance["held_out"] is True


def test_agent_lightning_adapter_converts_rewarded_trajectory_to_v1_events() -> None:
    trajectory = asyncio.run(_collect()).trajectories[0]
    records = AgentLightningAdapter(_Encoder()).to_event_records(trajectory)
    assert records[0].event_type == "model_request"
    assert records[0].data["prompt_token_ids"] == [1, 2, 3]
    assert records[0].data["response_token_ids"] == [4, 5]
    assert records[0].data["server"]["response_id"] == "llm-1"
    assert records[-1].event_type == "reward"
    assert records[-1].data["value"] == trajectory.reward.total_reward


def test_agent_lightning_adapter_rejects_lossy_trajectory() -> None:
    trajectory = asyncio.run(_collect()).trajectories[0]
    step = trajectory.steps[0]
    lossy = replace(
        trajectory,
        steps=(replace(step, observation=replace(step.observation, exact=False)),),
    )
    with pytest.raises(AgentLightningAdapterError, match="lossy observation"):
        AgentLightningAdapter(_Encoder()).to_event_records(lossy)


def test_agent_lightning_adapter_rejects_retokenized_policy_data() -> None:
    trajectory = asyncio.run(_collect()).trajectories[0]

    class RetokenizingEncoder(_Encoder):
        def encode_step(self, trajectory, step) -> AgentLightningEncodedStep:
            return replace(super().encode_step(trajectory, step), exact=False)

    with pytest.raises(AgentLightningAdapterError, match="retokenized"):
        AgentLightningAdapter(RetokenizingEncoder()).to_event_records(trajectory)


def test_agent_lightning_trajectory_aggregation_masks_tool_observation_tokens() -> None:
    trajectory = asyncio.run(_collect()).trajectories[0]
    original = trajectory.steps[0]
    trajectory = replace(
        trajectory,
        steps=(
            replace(original, done=False, termination_reason=None),
            replace(original, step_index=1, model_call_id="llm-2"),
        ),
    )

    class MultiTurnEncoder:
        def encode_step(self, trajectory, step) -> AgentLightningEncodedStep:
            if step.step_index == 0:
                return AgentLightningEncodedStep((1, 2), (3,))
            # Token 9 is the tool/environment observation inserted between calls.
            return AgentLightningEncodedStep((1, 2, 3, 9), (4,))

    audit = AgentLightningAdapter(MultiTurnEncoder()).audit_trajectory_aggregation(
        trajectory
    )
    assert audit.prompt_token_ids == (1, 2)
    assert audit.response_token_ids == (3, 9, 4)
    assert audit.response_mask == (1, 0, 1)
    assert audit.policy_token_count == 2
    assert audit.observation_token_count == 1


def test_agent_lightning_trajectory_aggregation_rejects_prefix_mismatch() -> None:
    trajectory = asyncio.run(_collect()).trajectories[0]
    original = trajectory.steps[0]
    trajectory = replace(
        trajectory,
        steps=(
            replace(original, done=False, termination_reason=None),
            replace(original, step_index=1, model_call_id="llm-2"),
        ),
    )

    class MismatchedEncoder:
        def encode_step(self, trajectory, step) -> AgentLightningEncodedStep:
            if step.step_index == 0:
                return AgentLightningEncodedStep((1, 2), (3,))
            return AgentLightningEncodedStep((7, 8), (4,))

    with pytest.raises(AgentLightningAdapterError, match="prefix extension"):
        AgentLightningAdapter(MismatchedEncoder()).audit_trajectory_aggregation(
            trajectory
        )


def test_rl_export_cli_is_discoverable() -> None:
    result = CliRunner().invoke(app, ["rl", "export", "--help"])
    assert result.exit_code == 0
    assert "Trajectory JSONL output" in result.output
