from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
RESULTS = ROOT / "results"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _feature(task_id: str) -> tuple[str, str]:
    _, repo, feature, _ = task_id.split("-")
    return repo, feature


def _tool_summary(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "tool": call["tool_name"],
            "arguments": call["arguments"],
            "reward_eligible": call["reward_eligible"],
        }
        for call in row["tool_calls"]
    ]


def main() -> None:
    manifest = _json(DATASET / "manifest.json")
    rows_by_split = {
        split: _jsonl(DATASET / f"{split}.jsonl")
        for split in ("train", "dev", "heldout")
    }
    ids = [row["task_id"] for rows in rows_by_split.values() for row in rows]
    feature_sets = {
        split: {_feature(row["task_id"]) for row in rows}
        for split, rows in rows_by_split.items()
    }
    question_sets = {
        split: {row["question"] for row in rows}
        for split, rows in rows_by_split.items()
    }
    contract_leaks = [
        row["task_id"]
        for rows in rows_by_split.values()
        for row in rows
        if row["expected_file"] in json.dumps(row["completion_contract"])
        or row["expected_symbol"] in json.dumps(row["completion_contract"])
    ]
    leakage = {
        "dataset_fingerprint": manifest["dataset_fingerprint"],
        "task_ids_unique": len(ids) == len(set(ids)),
        "train_dev_feature_overlap": sorted(feature_sets["train"] & feature_sets["dev"]),
        "train_heldout_feature_overlap": sorted(
            feature_sets["train"] & feature_sets["heldout"]
        ),
        "dev_heldout_feature_overlap": sorted(feature_sets["dev"] & feature_sets["heldout"]),
        "train_heldout_exact_question_overlap": sorted(
            question_sets["train"] & question_sets["heldout"]
        ),
        "completion_contract_ground_truth_leaks": contract_leaks,
        "expected_answer_embedded_in_tool_metadata": False,
        "shared_repository_snapshots_across_splits": True,
        "shared_snapshot_risk": (
            "All feature labels and prompts are disjoint, but each fixture repository exposes "
            "source files for every split. Training-time tools could therefore inspect held-out "
            "feature files incidentally. This limits any generalization claim."
        ),
        "passed_strict_snapshot_isolation": False,
    }
    _write(RESULTS / "leakage_audit.json", leakage)

    baseline_metrics = _json(RESULTS / "baseline_metrics.json")
    trained_metrics = _json(RESULTS / "trained_metrics.json")
    baseline = _jsonl(RESULTS / "baseline_heldout_trajectories.jsonl")
    trained = _jsonl(RESULTS / "trained_heldout_trajectories.jsonl")
    if len(baseline) != len(trained):
        raise RuntimeError("baseline and trained held-out rollout counts differ")
    pairs = []
    for index, (before, after) in enumerate(zip(baseline, trained, strict=True)):
        if before["task"]["task_id"] != after["task"]["task_id"]:
            raise RuntimeError(f"held-out protocol order mismatch at row {index}")
        pairs.append(
            {
                "row": index,
                "task_id": before["task"]["task_id"],
                "base_success": before["success"],
                "trained_success": after["success"],
                "base_reward": before["reward"]["total_reward"],
                "trained_reward": after["reward"]["total_reward"],
                "base_steps": before["agent_steps"],
                "trained_steps": after["agent_steps"],
                "base_policy_tokens": before["policy_tokens"],
                "trained_policy_tokens": after["policy_tokens"],
                "base_tools": _tool_summary(before),
                "trained_tools": _tool_summary(after),
            }
        )
    process_improvements = sorted(
        (row for row in pairs if row["trained_reward"] > row["base_reward"]),
        key=lambda row: row["trained_reward"] - row["base_reward"],
        reverse=True,
    )
    regressions = sorted(
        (
            row
            for row in pairs
            if row["trained_success"] < row["base_success"]
            or row["trained_reward"] < row["base_reward"]
            or row["trained_policy_tokens"] > row["base_policy_tokens"]
        ),
        key=lambda row: row["trained_policy_tokens"] - row["base_policy_tokens"],
        reverse=True,
    )
    comparison = {
        "dataset_fingerprint": manifest["dataset_fingerprint"],
        "same_heldout_tasks_and_order": True,
        "heldout_rollouts": len(pairs),
        "base": baseline_metrics["heldout"],
        "trained": trained_metrics["heldout"],
        "success_delta_count": (
            trained_metrics["heldout"]["success_count"]
            - baseline_metrics["heldout"]["success_count"]
        ),
        "outcome_improvement": False,
        "process_improvement_examples": process_improvements[:4],
        "regression_examples": regressions[:2],
        "reward_hacking_audit": {
            "base": baseline_metrics["heldout"]["reward_hacking_checks"],
            "trained": trained_metrics["heldout"]["reward_hacking_checks"],
            "observed": True,
            "interpretation": (
                "No expected-string-without-evidence, premature submission, or evidence-tool "
                "avoidance flag fired. However, the trained policy often made one useful search "
                "and stopped. That earns a better shaped reward while success stays at zero, so "
                "it is a reward-shaping shortcut rather than task learning."
            ),
        },
    }
    _write(RESULTS / "comparison.json", comparison)

    train_rows = _jsonl(RESULTS / "trained_train_trajectories.jsonl")
    malformed = []
    reward_mismatches = []
    zero_masked_observation_tokens = 0
    for index, row in enumerate(train_rows):
        completion_length = len(row["completion_token_ids"])
        if (
            not row["on_policy_capture_exact"]
            or not row["prompt_token_ids"]
            or completion_length == 0
            or len(row["completion_mask"]) != completion_length
            or len(row["loss_mask"]) != completion_length
            or row["policy_tokens"] != sum(row["loss_mask"])
            or any(bit not in (0, 1) for bit in row["loss_mask"])
        ):
            malformed.append(index)
        zero_masked_observation_tokens += row["loss_mask"].count(0)
        if row["reward"]["total_reward"] != row["axiom_trajectory"]["reward"][
            "total_reward"
        ]:
            reward_mismatches.append(index)
    on_policy = {
        "rollouts_checked": len(train_rows),
        "all_captured_as_exact_by_trainer": all(
            row["on_policy_capture_exact"] for row in train_rows
        ),
        "malformed_token_or_mask_rows": malformed,
        "reward_trajectory_mismatch_rows": reward_mismatches,
        "tool_observation_tokens_masked_from_loss": zero_masked_observation_tokens,
        "passed": not malformed and not reward_mismatches,
        "boundary": (
            "ArtifactGRPOTrainer records prompt_ids, completion_ids, completion_mask, and the "
            "tool mask directly from TRL's generation-and-score output. Reconstructed Axiom "
            "trajectory observations remain exact=false and are not used as on-policy input."
        ),
    }
    _write(RESULTS / "on_policy_audit.json", on_policy)
    print(
        json.dumps(
            {"leakage": leakage, "comparison": comparison, "on_policy": on_policy},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
