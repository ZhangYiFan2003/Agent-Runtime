from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any
from uuid import uuid4

from axiom.config import AxiomConfig
from axiom.rl import (
    AgentTrajectory,
    RewardConfig,
    RewardPipeline,
    TerminationReason,
    TrajectoryObservation,
    TrajectoryOutcome,
    TrajectoryStep,
)
from axiom.tools import ToolRegistry, get_builtin_tools
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema
from axiom.tools.executor import ToolExecutor

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def _observation_fingerprint(messages: tuple[dict[str, Any], ...]) -> str:
    payload = json.dumps(messages, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def reward_config(name: str) -> RewardConfig:
    if name == "outcome_only":
        return RewardConfig(
            verified_outcome_reward=1.0,
            unverified_completion_penalty=1.0,
            no_progress_penalty=1.0,
            failed_penalty=1.0,
        )
    if name == "outcome_efficiency":
        return RewardConfig(
            verified_outcome_reward=1.0,
            successful_tool_reward=0.05,
            max_tool_reward=0.15,
            step_penalty=0.005,
            unverified_completion_penalty=1.0,
            no_progress_penalty=1.0,
            failed_penalty=1.0,
        )
    raise ValueError(f"unknown reward configuration: {name}")


class AxiomRepositoryEnvironment:
    """TRL environment whose tools execute through Axiom's Tool abstraction."""

    def __init__(self) -> None:
        self._config = AxiomConfig()
        builtins = {tool.name: tool for tool in get_builtin_tools()}
        self._registry = ToolRegistry()
        self._registry.register(builtins["grep"])
        self._registry.register(builtins["read_file"])
        self._registry.register(
            Tool(
                name="submit_answer",
                description=(
                    "Submit the repository-relative file and exact symbol for verification."
                ),
                parameters=object_schema(
                    {
                        "file_path": {
                            "type": "string",
                            "description": "Repository-relative evidence file path",
                        },
                        "symbol": {
                            "type": "string",
                            "description": "Exact class, function, or configuration key",
                        },
                    },
                    ["file_path", "symbol"],
                ),
                required_keys=["file_path", "symbol"],
                handler=self._submit_handler,
            )
        )
        self._executor = ToolExecutor(self._registry)
        self._task: dict[str, Any] = {}
        self._calls: list[dict[str, Any]] = []
        self._read_paths: set[str] = set()
        self._search_seen = False
        self._submission: dict[str, Any] | None = None
        self._reward = None

    def reset(self, **kwargs: Any) -> None:
        self._task = {
            key: kwargs[key]
            for key in (
                "task_id",
                "split",
                "repo",
                "snapshot",
                "question",
                "expected_file",
                "expected_symbol",
            )
        }
        self._calls = []
        self._read_paths = set()
        self._search_seen = False
        self._submission = None
        self._reward = None
        return None

    def search_repository(self, query: str) -> str:
        """Search literal text in the current repository snapshot.

        Args:
            query: Exact text to locate, such as a class, function, or config key.

        Returns:
            Matching repository-relative paths, line numbers, and snippets.
        """

        self._search_seen = True
        return self._execute(
            "grep",
            {"pattern": query, "path": ".", "regex": False},
            exposed_name="search_repository",
        )

    def read_repository_file(self, path: str) -> str:
        """Read a repository file to gather direct evidence before submission.

        Args:
            path: Repository-relative path returned by search_repository.

        Returns:
            Numbered file contents or an Axiom tool error.
        """

        normalized = path.replace("\\", "/").removeprefix("./")
        self._read_paths.add(normalized)
        return self._execute(
            "read_file",
            {"path": normalized, "limit": 200},
            exposed_name="read_repository_file",
        )

    def submit_answer(self, file_path: str, symbol: str) -> str:
        """Submit an answer for deterministic completion verification.

        Args:
            file_path: Repository-relative file containing the answer evidence.
            symbol: Exact class, function, or configuration key found in that file.

        Returns:
            VERIFIED when answer and evidence satisfy the completion contract.
        """

        return self._execute(
            "submit_answer",
            {"file_path": file_path, "symbol": symbol},
        )

    def get_reward(self) -> float:
        self._reward = RewardPipeline(
            reward_config(os.environ.get("AXIOM_RL_REWARD_CONFIG", "outcome_only"))
        ).score(self._trajectory())
        return self._reward.total_reward

    def _context(self) -> ToolContext:
        repo_root = (FIXTURES / str(self._task["repo"])).resolve()
        return ToolContext(
            cwd=str(repo_root),
            workspace=str(repo_root),
            config=self._config,
            run_id=f"experiment-{self._task['task_id']}",
        )

    def _execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        exposed_name: str | None = None,
    ) -> str:
        call_id = f"call-{len(self._calls) + 1}"
        result = asyncio.run(
            self._executor.execute_one(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                },
                self._context(),
            )
        )
        self._calls.append(
            {
                "call_id": call_id,
                "tool_name": exposed_name or name,
                "arguments": arguments,
                "execution_status": "FAILED" if result.is_error else "SUCCEEDED",
                "is_error": result.is_error,
                "reward_eligible": self._reward_eligible(
                    exposed_name or name,
                    arguments,
                    result,
                ),
            }
        )
        return result.content

    def _reward_eligible(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: ToolResult,
    ) -> bool:
        if result.is_error:
            return False
        if tool_name == "search_repository":
            return result.content != "(no matches)"
        if tool_name == "read_repository_file":
            path = str(arguments.get("path") or "").replace("\\", "/").removeprefix("./")
            return path == self._task["expected_file"]
        if tool_name == "submit_answer":
            return bool(self._submission and self._submission["verified"])
        return False

    async def _submit_handler(
        self,
        payload: dict[str, Any],
        _context: ToolContext,
    ) -> ToolResult:
        file_path = str(payload["file_path"]).replace("\\", "/").removeprefix("./")
        symbol = str(payload["symbol"]).strip()
        answer_correct = (
            file_path == self._task["expected_file"]
            and symbol == self._task["expected_symbol"]
        )
        evidence_complete = self._search_seen and file_path in self._read_paths
        verified = answer_correct and evidence_complete
        self._submission = {
            "file_path": file_path,
            "symbol": symbol,
            "answer_correct": answer_correct,
            "evidence_complete": evidence_complete,
            "verified": verified,
        }
        if verified:
            return ToolResult("VERIFIED: answer and repository evidence match ground truth.")
        reasons = []
        if not answer_correct:
            reasons.append("answer does not match ground truth")
        if not evidence_complete:
            reasons.append("required search/read evidence is missing")
        return ToolResult("NOT_VERIFIED: " + "; ".join(reasons), is_error=True)

    def _trajectory(self) -> AgentTrajectory:
        trajectory_id = str(uuid4())
        messages = (
            {
                "role": "user",
                "content": self._task["question"],
            },
        )
        verified = bool(self._submission and self._submission["verified"])
        if verified:
            reason = TerminationReason.VERIFIED_COMPLETION
        elif self._submission is not None:
            reason = TerminationReason.UNVERIFIED_COMPLETION
        else:
            reason = TerminationReason.NO_PROGRESS
        steps = tuple(
            TrajectoryStep(
                trajectory_id=trajectory_id,
                run_id=trajectory_id,
                parent_run_id=None,
                step_index=index,
                observation=TrajectoryObservation(
                    messages=messages,
                    fingerprint=_observation_fingerprint(messages),
                    source="trl_environment_projection",
                    # Exact served token IDs are attached later by the trainer artifact hook.
                    exact=False,
                ),
                action={"tool_call": call},
                tool_actions=(
                    {
                        **call,
                        "execution_status": (
                            "SUCCEEDED" if call["reward_eligible"] else "FAILED"
                        ),
                    },
                ),
                done=index == len(self._calls) - 1,
                termination_reason=(reason if index == len(self._calls) - 1 else None),
            )
            for index, call in enumerate(self._calls)
        )
        return AgentTrajectory(
            trajectory_id=trajectory_id,
            run_id=trajectory_id,
            trace_id=trajectory_id,
            thread_id=trajectory_id,
            turn_id=trajectory_id,
            parent_run_id=None,
            parent_step_id=None,
            run_kind="ROOT",
            execution_strategy="react",
            steps=steps,
            outcome=TrajectoryOutcome(
                status="COMPLETED" if verified else "FAILED",
                done=True,
                success=verified,
                termination_reason=reason,
                completion_verified=(verified if self._submission is not None else None),
                verification_status="VERIFIED" if verified else "NOT_VERIFIED",
                evaluation_passed=verified,
            ),
            metrics={"tool_attempts": len(self._calls)},
            provenance={
                "experiment": "agentic-rl-v1",
                "dataset_task_id": self._task["task_id"],
                "split": self._task["split"],
                "trainer": "trl",
            },
        )

    def _artifact_record(
        self,
        *,
        prompt_token_ids: list[int],
        completion_token_ids: list[int],
        completion_mask: list[int],
        tool_mask: list[int],
        completion_text: str,
        advantage: float,
        stage: str,
    ) -> dict[str, Any]:
        if self._reward is None:
            raise RuntimeError("reward must be computed before artifact finalization")
        trajectory = self._trajectory()
        segments = 0
        previous = 0
        for active, policy in zip(completion_mask, tool_mask, strict=True):
            value = int(bool(active and policy))
            if value and not previous:
                segments += 1
            previous = value
        repeated_calls = sum(Counter(call["tool_name"] for call in self._calls).values()) - len(
            {call["tool_name"] for call in self._calls}
        )
        reward_ablation = {
            name: RewardPipeline(reward_config(name)).score(trajectory).to_dict()
            for name in ("outcome_only", "outcome_efficiency")
        }
        return {
            "schema_version": 1,
            "stage": stage,
            "task": dict(self._task),
            "success": trajectory.outcome.success,
            "verified": trajectory.outcome.completion_verified is True,
            "termination_reason": trajectory.outcome.termination_reason.value,
            "failure_category": (
                None
                if trajectory.outcome.success
                else "wrong_or_missing_evidence"
                if self._submission is not None
                else "no_submission"
            ),
            "reward": self._reward.to_dict(),
            "reward_ablation": reward_ablation,
            "tool_calls": list(self._calls),
            "tool_attempts": len(self._calls),
            "agent_steps": segments,
            "prompt_tokens": sum(1 for token in prompt_token_ids if token >= 0),
            "completion_tokens": sum(completion_mask),
            "policy_tokens": sum(
                int(bool(active and policy))
                for active, policy in zip(completion_mask, tool_mask, strict=True)
            ),
            "prompt_token_ids": prompt_token_ids,
            "completion_token_ids": completion_token_ids,
            "completion_mask": completion_mask,
            "loss_mask": [
                int(bool(active and policy))
                for active, policy in zip(completion_mask, tool_mask, strict=True)
            ],
            "on_policy_capture_exact": True,
            "completion_text": completion_text,
            "advantage": advantage,
            "submission": self._submission,
            "reward_hacking_checks": {
                "correct_without_required_evidence": bool(
                    self._submission
                    and self._submission["answer_correct"]
                    and not self._submission["evidence_complete"]
                ),
                "premature_submission": bool(
                    self._submission and not self._submission["evidence_complete"]
                ),
                "repeated_tool_calls": repeated_calls,
                "avoided_all_evidence_tools": not self._search_seen and not self._read_paths,
            },
            "axiom_trajectory": {
                **trajectory.to_dict(),
                "reward": self._reward.to_dict(),
            },
        }
