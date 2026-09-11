from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"
DATASET = ROOT / "dataset"

FEATURES = {
    "train": {
        "atlas": ("amber", "birch", "cedar", "dune", "ember"),
        "beacon": ("falcon", "grove", "harbor", "iris", "juniper"),
        "cinder": ("kestrel", "linden", "maple", "nova"),
    },
    "dev": {
        "atlas": ("onyx", "pine"),
        "beacon": ("quartz",),
        "cinder": ("river",),
    },
    "heldout": {
        "atlas": ("summit",),
        "beacon": ("timber", "umber"),
        "cinder": ("velvet", "willow"),
    },
}


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _class_name(feature: str) -> str:
    return feature.title() + "Service"


def _fixture_files() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for repo in sorted({repo for split in FEATURES.values() for repo in split}):
        all_features = tuple(
            feature
            for split in ("train", "dev", "heldout")
            for feature in FEATURES[split].get(repo, ())
        )
        files[Path(repo) / "README.md"] = (
            f"# {repo.title()} fixture\n\n"
            "A deterministic repository-navigation fixture for Agentic RL v1.\n"
        )
        files[Path(repo) / "src" / repo / "__init__.py"] = ""
        registry_lines = ["SERVICE_MODULES = {"]
        entrypoint_lines = ["# Deliberate call-site distractors used by navigation tasks."]
        docs_lines = ["# Historical names", ""]
        for ordinal, feature in enumerate(all_features, start=1):
            class_name = _class_name(feature)
            module = f"{feature}_service"
            config_key = f"{feature}_retry_limit"
            registry_lines.append(f'    "{feature}": "{repo}.{module}:{class_name}",')
            entrypoint_lines.extend(
                [
                    f"from .{module} import {feature}_pipeline",
                    "",
                    f"def run_{feature}_job(payload: str) -> str:",
                    f"    return {feature}_pipeline(payload)",
                    "",
                ]
            )
            docs_lines.append(
                f"Legacy{class_name} was removed; use the current implementation instead."
            )
            files[Path(repo) / "src" / repo / f"{module}.py"] = (
                "from __future__ import annotations\n\n"
                f'CONFIG_KEY = "{config_key}"\n\n\n'
                f"class {class_name}:\n"
                f'    """Deterministic {feature} processing service."""\n\n'
                "    def normalize(self, payload: str) -> str:\n"
                "        return payload.strip().lower()\n\n"
                "    def run(self, payload: str) -> str:\n"
                f'        return "{repo}:{feature}:" + self.normalize(payload)\n\n\n'
                f"def {feature}_pipeline(payload: str) -> str:\n"
                f"    return {class_name}().run(payload)\n"
            )
            files[Path(repo) / "tests" / f"test_{feature}_service.py"] = (
                f"from {repo}.{module} import {feature}_pipeline\n\n\n"
                f"def test_{feature}_pipeline_happy_path() -> None:\n"
                f'    assert {feature}_pipeline(" Payload ") == "{repo}:{feature}:payload"\n'
            )
            files[Path(repo) / "config" / f"{feature}.toml"] = (
                f"# Retry behavior for {class_name}.\n"
                f"{config_key} = {ordinal + 1}\n"
                f"queue_name = \"{repo}-{feature}\"\n"
            )
        registry_lines.append("}")
        files[Path(repo) / "src" / repo / "registry.py"] = "\n".join(registry_lines) + "\n"
        files[Path(repo) / "src" / repo / "entrypoints.py"] = (
            "\n".join(entrypoint_lines) + "\n"
        )
        files[Path(repo) / "docs" / "migration.md"] = "\n".join(docs_lines) + "\n"
    return files


def _task(split: str, repo: str, feature: str, kind: str) -> dict[str, Any]:
    class_name = _class_name(feature)
    service_file = f"src/{repo}/{feature}_service.py"
    config_file = f"config/{feature}.toml"
    test_file = f"tests/test_{feature}_service.py"
    if kind == "definition":
        question = (
            f"Locate the current definition of class {class_name}. Return its repository-relative "
            "file path and exact symbol name."
        )
        expected_file, expected_symbol = service_file, class_name
    elif kind == "config":
        question = (
            f"Which configuration file and exact key control the retry limit used by {class_name}? "
            "Follow the implementation reference to the configuration evidence."
        )
        expected_file, expected_symbol = config_file, f"{feature}_retry_limit"
    elif kind == "test":
        question = (
            f"Find the test that verifies the happy path of {feature}_pipeline. Return the test "
            "file and exact test function name."
        )
        expected_file = test_file
        expected_symbol = f"test_{feature}_pipeline_happy_path"
    else:  # pragma: no cover - generator invariant
        raise ValueError(kind)
    return {
        "task_id": f"{split}-{repo}-{feature}-{kind}",
        "split": split,
        "repo": repo,
        "snapshot": "agentic-rl-v1",
        "question": question,
        "expected_file": expected_file,
        "expected_symbol": expected_symbol,
        "allowed_evidence": ["search_repository", "read_repository_file"],
        "required_evidence": ["search_repository", "read_repository_file"],
        "required_tool_family": "repository_navigation",
        "completion_contract": {
            "type": "tool_verified_submission",
            "required_tool": "submit_answer",
            "requires_search": True,
            "requires_read_of_submitted_file": True,
        },
        "scorer": "exact_file_symbol_with_evidence_v1",
    }


def _tasks() -> dict[str, list[dict[str, Any]]]:
    return {
        split: [
            _task(split, repo, feature, kind)
            for repo, features in repos.items()
            for feature in features
            for kind in ("definition", "config", "test")
        ]
        for split, repos in FEATURES.items()
    }


def _render() -> tuple[dict[Path, str], dict[str, Any]]:
    files = {FIXTURES / path: content for path, content in _fixture_files().items()}
    tasks = _tasks()
    for split, rows in tasks.items():
        files[DATASET / f"{split}.jsonl"] = "".join(
            json.dumps(row, sort_keys=True) + "\n" for row in rows
        )

    snapshot_fingerprints: dict[str, str] = {}
    for repo in sorted({row["repo"] for rows in tasks.values() for row in rows}):
        parts = []
        prefix = FIXTURES / repo
        for path, content in sorted(files.items(), key=lambda item: str(item[0])):
            if path.is_relative_to(prefix):
                parts.append(str(path.relative_to(prefix)).replace("\\", "/").encode())
                parts.append(b"\0")
                parts.append(content.encode())
                parts.append(b"\0")
        snapshot_fingerprints[repo] = _sha256_bytes(b"".join(parts))

    fingerprint_payload = {
        "schema_version": 1,
        "tasks": tasks,
        "snapshot_fingerprints": snapshot_fingerprints,
    }
    dataset_fingerprint = _sha256_bytes(_canonical_json(fingerprint_payload))
    manifest = {
        "name": "agentic-rl-experiment-v1",
        "schema_version": 1,
        "counts": {split: len(rows) for split, rows in tasks.items()},
        "dataset_fingerprint": dataset_fingerprint,
        "snapshot_fingerprints": snapshot_fingerprints,
        "split_unit": "feature",
        "task_kinds": ["definition", "config", "test"],
    }
    files[DATASET / "manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    return files, manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files, manifest = _render()
    if args.check:
        mismatches = [
            str(path.relative_to(ROOT))
            for path, expected in files.items()
            if not path.exists() or path.read_text(encoding="utf-8") != expected
        ]
        if mismatches:
            raise SystemExit("dataset drift: " + ", ".join(mismatches))
    else:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
