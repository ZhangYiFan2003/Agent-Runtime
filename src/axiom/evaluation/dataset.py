from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.evaluation.models import EvaluationDataset, EvaluationSuiteResult


def load_dataset(path: str | Path) -> EvaluationDataset:
    source = Path(path).expanduser()
    if source.suffix.lower() != ".json":
        raise ValueError("Evaluation v1 datasets must use the .json format")
    data = _read_object(source, artifact="evaluation dataset")
    return EvaluationDataset.from_dict(data)


def save_result(result: EvaluationSuiteResult, path: str | Path) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def load_result(path: str | Path) -> EvaluationSuiteResult:
    return EvaluationSuiteResult.from_dict(
        _read_object(Path(path).expanduser(), artifact="evaluation result")
    )


def _read_object(path: Path, *, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{artifact} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {artifact} JSON at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{artifact} must be a JSON object")
    return value
