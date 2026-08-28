from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from benchmarks.retrieval.evaluate_retrieval_v2 import _markdown


def main() -> None:
    parser = argparse.ArgumentParser(description="Render existing Retrieval v2 JSON as Markdown")
    parser.add_argument("artifacts", nargs="+", type=Path)
    args = parser.parse_args()
    for artifact in args.artifacts:
        report = _read(artifact)
        artifact.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"benchmark artifact must be an object: {path}")
    return value


if __name__ == "__main__":
    main()
