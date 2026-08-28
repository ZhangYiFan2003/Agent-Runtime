from __future__ import annotations

import argparse
import platform
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.config import EmbeddingConfig
from axiom.rag import CodeIndex
from benchmarks.retrieval.evaluation import (
    DeterministicHashEmbeddingProvider,
    evaluate_by_query_type,
    evaluate_rankings,
    load_retrieval_dataset,
    serialize_report,
)

WEIGHT_GRID = (
    {"lexical": 0.4, "vector": 0.2, "symbol": 0.4},
    {"lexical": 0.3, "vector": 0.2, "symbol": 0.5},
    {"lexical": 0.4, "vector": 0.1, "symbol": 0.5},
    {"lexical": 0.5, "vector": 0.1, "symbol": 0.4},
    {"lexical": 0.35, "vector": 0.15, "symbol": 0.5},
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Select Retrieval v2 fusion weights on dev only")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/retrieval/development-dataset.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/retrieval/analysis/dev-weight-tuning.json"),
    )
    parser.add_argument("--dimensions", type=int, default=384)
    args = parser.parse_args()

    root = args.repository.resolve()
    dataset = load_retrieval_dataset(args.dataset, repository_root=root)
    if dataset.metadata.get("role") != "development":
        raise ValueError("weight tuning requires a dataset whose role is development")
    with tempfile.TemporaryDirectory(prefix="axiom-v2-tuning-", ignore_cleanup_errors=True) as tmp:
        config = EmbeddingConfig(
            enabled=True,
            search_mode="hybrid_v2",
            dimensions=args.dimensions,
            candidate_limit=200,
        )
        index = CodeIndex(
            root,
            db_path=Path(tmp) / "index.sqlite3",
            embedding_provider=DeterministicHashEmbeddingProvider(args.dimensions),
            search_config=config,
        )
        index.update(root / "src" / "axiom")
        trials: list[dict[str, Any]] = []
        for weights in WEIGHT_GRID:
            config.v2_lexical_weight = weights["lexical"]
            config.v2_vector_weight = weights["vector"]
            config.symbol_weight = weights["symbol"]
            rankings = {
                case.id: index.search(case.query, limit=5, mode="hybrid_v2")
                for case in dataset.cases
            }
            trials.append(
                {
                    "weights": weights,
                    "all": evaluate_rankings(dataset.cases, rankings),
                    "by_query_type": evaluate_by_query_type(dataset.cases, rankings),
                }
            )

    selected = max(
        trials,
        key=lambda trial: (
            trial["all"]["recall_at_5"],
            trial["all"]["mrr_at_5"],
            trial["all"]["recall_at_1"],
        ),
    )
    report = {
        "schema_version": 1,
        "analysis": "retrieval-v2-development-weight-selection",
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "case_count": len(dataset.cases),
        },
        "selection_rule": "maximize dev Recall@5, then MRR@5, then Recall@1",
        "search_space": "five predeclared three-source RRF weight triples; no black-box search",
        "environment": {
            "git_commit": _git_commit(root),
            "python": platform.python_version(),
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "trials": trials,
        "selected": selected,
        "holdout_used": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialize_report(report), encoding="utf-8")
    args.output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(_markdown(report))


def _git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "unknown"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Retrieval v2 Development Weight Selection",
        "",
        "> Development split only. Holdout was not evaluated.",
        "",
        f"- Cases: {report['dataset']['case_count']}",
        f"- Selection rule: {report['selection_rule']}",
        "",
        "| Lexical | Vector | Symbol | Recall@1 | Recall@3 | Recall@5 | MRR@5 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for trial in report["trials"]:
        weights = trial["weights"]
        metrics = trial["all"]
        lines.append(
            f"| {weights['lexical']:.2f} | {weights['vector']:.2f} | "
            f"{weights['symbol']:.2f} | {metrics['recall_at_1']:.4f} | "
            f"{metrics['recall_at_3']:.4f} | {metrics['recall_at_5']:.4f} | "
            f"{metrics['mrr_at_5']:.4f} |"
        )
    selected = report["selected"]
    lines.extend(
        [
            "",
            f"Selected weights: `{selected['weights']}`",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
