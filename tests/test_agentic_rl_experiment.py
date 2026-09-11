from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "agentic-rl-v1"


def _load_environment_module():
    path = EXPERIMENT / "environment.py"
    spec = importlib.util.spec_from_file_location("agentic_rl_v1_environment", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(split: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (EXPERIMENT / "dataset" / f"{split}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_experiment_dataset_counts_and_feature_splits_are_frozen() -> None:
    manifest = json.loads(
        (EXPERIMENT / "dataset" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["counts"] == {"train": 42, "dev": 12, "heldout": 15}
    assert manifest["dataset_fingerprint"].startswith("sha256:")

    features_by_split = {
        split: {
            (row["repo"], row["task_id"].split("-")[-2]) for row in _rows(split)
        }
        for split in ("train", "dev", "heldout")
    }
    assert features_by_split["train"].isdisjoint(features_by_split["dev"])
    assert features_by_split["train"].isdisjoint(features_by_split["heldout"])
    assert features_by_split["dev"].isdisjoint(features_by_split["heldout"])


def test_environment_verifies_answer_only_after_axiom_search_and_read(monkeypatch) -> None:
    module = _load_environment_module()
    row = _rows("heldout")[0]
    monkeypatch.setenv("AXIOM_RL_REWARD_CONFIG", "outcome_only")
    environment = module.AxiomRepositoryEnvironment()
    environment.reset(**row)

    search_result = environment.search_repository(row["expected_symbol"])
    assert row["expected_file"] in search_result.replace("\\", "/")
    read_result = environment.read_repository_file(row["expected_file"])
    assert row["expected_symbol"] in read_result
    assert environment.submit_answer(
        row["expected_file"], row["expected_symbol"]
    ).startswith("VERIFIED")
    assert environment.get_reward() == 1.0


def test_environment_rejects_ground_truth_guess_without_evidence(monkeypatch) -> None:
    module = _load_environment_module()
    row = _rows("heldout")[0]
    monkeypatch.setenv("AXIOM_RL_REWARD_CONFIG", "outcome_only")
    environment = module.AxiomRepositoryEnvironment()
    environment.reset(**row)

    result = environment.submit_answer(row["expected_file"], row["expected_symbol"])
    assert result.startswith("NOT_VERIFIED")
    assert environment.get_reward() == -1.0
