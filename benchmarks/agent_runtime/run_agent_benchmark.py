from __future__ import annotations

import argparse
import subprocess
from collections import Counter
from pathlib import Path

from axiom.config import load_config
from axiom.evaluation import load_dataset
from axiom.evaluation.scorers import scorer_from_spec


def validate_dataset(path: str | Path) -> dict[str, object]:
    dataset = load_dataset(path)
    tags = Counter(tag for case in dataset.cases for tag in case.tags)
    for case in dataset.cases:
        for spec in case.scorers:
            scorer_from_spec(spec)
        tool_specs = [spec for spec in case.scorers if spec.type == "tool_usage"]
        forbidden = {
            tool
            for spec in tool_specs
            for tool in spec.config.get("forbidden_tools", [])
            if isinstance(tool, str)
        }
        if "write_file" not in forbidden:
            raise ValueError(f'evaluation case "{case.id}" must forbid write_file')
        required = {
            tool
            for spec in tool_specs
            for tool in spec.config.get("required_tools", [])
            if isinstance(tool, str)
        }
        if required & {"write_file", "bash", "execute_command"}:
            raise ValueError(f'evaluation case "{case.id}" requires a mutating tool')
    return {
        "dataset": dataset.name,
        "version": dataset.version,
        "case_count": len(dataset.cases),
        "tags": dict(sorted(tags.items())),
        "provider_required": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset", type=Path, default=Path("benchmarks/agent-runtime/dataset.json")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path(".tmp/agent-runtime-eval"))
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    summary = validate_dataset(args.dataset)
    print(summary)
    if args.validate_only:
        return
    if not load_config(project_root=Path.cwd()).llm.api_key:
        raise SystemExit(
            "real-provider benchmark not executed: provider credential is not configured"
        )
    command = [
        "axiom",
        "eval",
        "run",
        str(args.dataset),
        "--cwd",
        ".",
        "--data-dir",
        str(args.data_dir),
        "--verbose",
    ]
    if args.output:
        command.extend(["--output", str(args.output)])
    raise SystemExit(subprocess.run(command, check=False).returncode)


if __name__ == "__main__":
    main()
