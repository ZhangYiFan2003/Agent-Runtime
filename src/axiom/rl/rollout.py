from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from axiom.evaluation.models import EvaluationDataset
from axiom.evaluation.runner import EvaluationRunner
from axiom.runtime.checkpoints import RuntimeStore
from axiom.runtime.observability_store import ObservabilityService, ObservabilityStore

from .builder import TrajectoryBuilder, TrajectoryBuildError
from .models import AgentTrajectory, write_trajectories_jsonl
from .rewards import RewardPipeline


@dataclass(frozen=True, slots=True)
class RolloutDataset:
    name: str
    version: str
    split: str
    trajectories: tuple[AgentTrajectory, ...]
    source_evaluation: str
    source_evaluation_version: str

    @property
    def rollout_count(self) -> int:
        return len(self.trajectories)

    @property
    def mean_reward(self) -> float | None:
        rewards = [
            item.reward.total_reward for item in self.trajectories if item.reward is not None
        ]
        return sum(rewards) / len(rewards) if rewards else None

    @property
    def verified_completion_rate(self) -> float:
        if not self.trajectories:
            return 0.0
        return sum(
            item.outcome.completion_verified is True for item in self.trajectories
        ) / len(self.trajectories)

    @property
    def mean_episode_length(self) -> float:
        if not self.trajectories:
            return 0.0
        return sum(len(item.steps) for item in self.trajectories) / len(self.trajectories)

    def export_jsonl(self, path: str | Path) -> None:
        write_trajectories_jsonl(path, list(self.trajectories))

    def summary(self) -> dict[str, Any]:
        component_totals: dict[str, float] = {}
        rewarded = 0
        for trajectory in self.trajectories:
            if trajectory.reward is None:
                continue
            rewarded += 1
            for name, value in trajectory.reward.components.items():
                component_totals[name] = component_totals.get(name, 0.0) + value
        return {
            "rollout_count": self.rollout_count,
            "valid_trajectories": self.rollout_count,
            "invalid_trajectories": 0,
            "mean_reward": self.mean_reward,
            "mean_episode_length": self.mean_episode_length,
            "verified_completion_rate": self.verified_completion_rate,
            "reward_component_means": {
                name: total / rewarded for name, total in sorted(component_totals.items())
            }
            if rewarded
            else {},
        }


class RLRolloutRunner:
    """Reuse EvaluationRunner execution while giving rollouts a separate lifecycle."""

    def __init__(
        self,
        evaluator: EvaluationRunner,
        *,
        runtime_store: RuntimeStore,
        observability_store: ObservabilityStore,
        trajectory_builder: TrajectoryBuilder | None = None,
        reward_pipeline: RewardPipeline | None = None,
    ) -> None:
        self.evaluator = evaluator
        self.runtime_store = runtime_store
        self.observability = ObservabilityService(observability_store)
        self.trajectory_builder = trajectory_builder or TrajectoryBuilder()
        self.reward_pipeline = reward_pipeline or RewardPipeline()

    async def collect(
        self,
        dataset: EvaluationDataset,
        *,
        trials: int = 1,
        split: str = "train",
        include_child_runs: bool = True,
    ) -> RolloutDataset:
        split = split.strip()
        if not split:
            raise ValueError("rollout split is required")
        suite = await self.evaluator.run(dataset, trials=trials)
        cases = {case.id: case for case in dataset.cases}
        trajectories: list[AgentTrajectory] = []
        seen_runs: set[str] = set()
        for result in suite.results:
            case = cases[result.case_id]
            root = await self.runtime_store.load(result.run_id)
            if root is None:
                raise TrajectoryBuildError(
                    f"evaluation Run {result.run_id or '<missing>'} has no checkpoint evidence"
                )
            thread_runs = await self.runtime_store.list(root.thread_id)
            selected = [root]
            if include_child_runs:
                selected.extend(_descendants(root.run_id, thread_runs))
            for checkpoint in selected:
                if checkpoint.run_id in seen_runs:
                    continue
                seen_runs.add(checkpoint.run_id)
                trace = await self.observability.trace(checkpoint.run_id)
                if trace is None:
                    raise TrajectoryBuildError(
                        f"Run {checkpoint.run_id} has no trace evidence"
                    )
                records = await self.runtime_store.list_tool_executions(checkpoint.run_id)
                direct_children = sorted(
                    item.run_id for item in thread_runs if item.parent_run_id == checkpoint.run_id
                )
                is_root = checkpoint.run_id == root.run_id
                provenance = {
                    **result.attribution,
                    "dataset_name": dataset.name,
                    "dataset_version": dataset.version,
                    "dataset_task_id": case.id,
                    "trial_index": result.trial_index,
                    "split": split,
                    "held_out": split in {"validation", "val", "test", "held_out"},
                    "episode_role": "root" if is_root else "child",
                    "reward_configuration": self.reward_pipeline.config.fingerprint,
                }
                trajectory = self.trajectory_builder.build(
                    checkpoint=checkpoint,
                    trace=trace,
                    tool_executions=records,
                    evaluation_result=result if is_root else None,
                    provenance=provenance,
                    child_run_ids=direct_children,
                )
                trajectories.append(
                    self.reward_pipeline.apply(
                        trajectory,
                        evaluation_result=result if is_root else None,
                    )
                )
        return RolloutDataset(
            name=f"{dataset.name}-rollouts",
            version=dataset.version,
            split=split,
            trajectories=tuple(trajectories),
            source_evaluation=dataset.name,
            source_evaluation_version=dataset.version,
        )


def _descendants(root_run_id: str, checkpoints: list[Any]) -> list[Any]:
    selected = []
    frontier = [root_run_id]
    while frontier:
        parent = frontier.pop(0)
        children = sorted(
            (item for item in checkpoints if item.parent_run_id == parent),
            key=lambda item: (item.created_at, item.run_id),
        )
        selected.extend(children)
        frontier.extend(item.run_id for item in children)
    return selected
