from __future__ import annotations

import argparse
import hashlib
import platform
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.config import EmbeddingConfig
from axiom.rag import CodeIndex
from benchmarks.retrieval.evaluation import (
    DeterministicHashEmbeddingProvider,
    dataset_distribution,
    evaluate_by_query_type,
    evaluate_candidate_recall,
    evaluate_context_coverage,
    evaluate_rankings,
    load_retrieval_dataset,
    percentile,
    serialize_report,
)

MODE_MAP = {
    "lexical_v1": "lexical",
    "vector_deterministic": "vector",
    "hybrid_v1": "hybrid",
    "lexical_v2": "lexical_v2",
    "symbol": "symbol",
    "hybrid_v2": "hybrid_v2",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate frozen Retrieval v2 configuration")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/retrieval/development-dataset.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/retrieval/results/retrieval-v2-development.json"),
    )
    parser.add_argument("--dimensions", type=int, default=384)
    parser.add_argument("--latency-repetitions", type=int, default=3)
    parser.add_argument("--allow-holdout", action="store_true")
    args = parser.parse_args()

    root = args.repository.resolve()
    dataset = load_retrieval_dataset(args.dataset, repository_root=root)
    role = str(dataset.metadata.get("role") or "analysis")
    if role == "holdout" and not args.allow_holdout:
        raise ValueError("holdout evaluation requires --allow-holdout after configuration freeze")
    config = EmbeddingConfig(
        enabled=True,
        search_mode="hybrid_v2",
        dimensions=args.dimensions,
        candidate_limit=200,
    )
    with tempfile.TemporaryDirectory(prefix="axiom-v2-eval-", ignore_cleanup_errors=True) as tmp:
        provider = DeterministicHashEmbeddingProvider(args.dimensions)
        index = CodeIndex(
            root,
            db_path=Path(tmp) / "index.sqlite3",
            embedding_provider=provider,
            search_config=config,
        )
        stats = index.update(root / "src" / "axiom")
        rankings = {
            label: {
                case.id: index.search(case.query, limit=50, mode=mode) for case in dataset.cases
            }
            for label, mode in MODE_MAP.items()
        }
        latency = {
            label: _measure_latency(
                index,
                dataset.cases,
                mode=MODE_MAP[label],
                repetitions=args.latency_repetitions,
            )
            for label in ("lexical_v1", "hybrid_v1", "hybrid_v2")
        }
        graph_cases = [
            case for case in dataset.cases if case.query_type == "graph/context-oriented"
        ]
        contexts = {
            case.id: index.build_code_context(
                case.query,
                mode="hybrid_v2",
                max_seed_chunks=5,
                max_graph_depth=2,
                max_items=30,
            ).items
            for case in graph_cases
        }

    mode_metrics = {
        label: {
            "all": evaluate_rankings(dataset.cases, mode_rankings),
            "by_query_type": evaluate_by_query_type(dataset.cases, mode_rankings),
        }
        for label, mode_rankings in rankings.items()
    }
    report = {
        "schema_version": 2,
        "benchmark": "axiom-repository-retrieval-v2",
        "benchmark_type": "offline deterministic fixture",
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "role": role,
            "query_count": len(dataset.cases),
            "sha256": _sha256(args.dataset),
            **dataset_distribution(dataset),
        },
        "environment": {
            "git_commit": _git_commit(root),
            "working_tree_dirty": _working_tree_dirty(root),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "frozen_v2_configuration": {
            "lexical_weight": config.v2_lexical_weight,
            "vector_weight": config.v2_vector_weight,
            "symbol_weight": config.symbol_weight,
            "candidate_limit_per_source": config.candidate_limit,
            "weight_selection": "development split only",
        },
        "index": {
            "scanned_files": stats.scanned_files,
            "indexed_files": stats.indexed_files,
            "failed_files": stats.failed_files,
            "chunks": stats.chunk_count,
            "embedded_chunks": stats.embedded_chunks,
        },
        "metric_definitions": {
            "recall_at_k": "fraction of queries with at least one relevant target in top K",
            "mrr_at_5": "mean reciprocal rank of first relevant target, truncated at rank 5",
            "candidate_recall_at_k": "fraction of queries hit by any source's top K candidates",
            "latency": "warm local retrieval calls at result limit 5; milliseconds",
            "graph_context": "bounded context coverage, not ranked retrieval Recall",
        },
        "modes": mode_metrics,
        "candidate_recall": {
            "v1_lexical_vector": evaluate_candidate_recall(
                dataset.cases,
                (rankings["lexical_v1"], rankings["vector_deterministic"]),
            ),
            "v2_lexical_vector_symbol": evaluate_candidate_recall(
                dataset.cases,
                (
                    rankings["lexical_v2"],
                    rankings["vector_deterministic"],
                    rankings["symbol"],
                ),
            ),
        },
        "latency_ms": latency,
        "graph_aware_context_v2": {
            "scope": "bounded context coverage; not ranked retrieval Recall",
            **evaluate_context_coverage(graph_cases, contexts),
        },
        "comparison": {
            "lexical_v1_to_v2": _comparison(
                mode_metrics["lexical_v1"]["all"], mode_metrics["lexical_v2"]["all"]
            ),
            "hybrid_v1_to_v2": _comparison(
                mode_metrics["hybrid_v1"]["all"], mode_metrics["hybrid_v2"]["all"]
            ),
        },
        "real_embedding_benchmark": {
            "status": "not executed",
            "reason": "provider credential unavailable; no ignored secret configuration was read",
        },
        "limitations": [
            "Repository-specific maintained ground truth.",
            "Deterministic hash embeddings test pipeline behavior, not semantic model quality.",
            "Local latency is a reproducible benchmark sample, not a production SLO.",
            "No LLM-as-a-judge is used.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialize_report(report), encoding="utf-8")
    args.output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(_markdown(report))


def _measure_latency(
    index: CodeIndex, cases: tuple[Any, ...], *, mode: str, repetitions: int
) -> dict[str, Any]:
    if repetitions <= 0:
        raise ValueError("latency repetitions must be positive")
    if cases:
        index.search(cases[0].query, limit=5, mode=mode)
    samples: list[float] = []
    for _ in range(repetitions):
        for case in cases:
            started = time.perf_counter()
            index.search(case.query, limit=5, mode=mode)
            samples.append((time.perf_counter() - started) * 1000)
    return {
        "samples": len(samples),
        "p50": round(percentile(samples, 0.50), 4),
        "p95": round(percentile(samples, 0.95), 4),
    }


def _comparison(
    before: dict[str, float], after: dict[str, float]
) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for metric in ("recall_at_1", "recall_at_3", "recall_at_5", "mrr_at_5"):
        old = before[metric]
        new = after[metric]
        delta = round(new - old, 4)
        result[metric] = {
            "before": old,
            "after": new,
            "absolute_delta": delta,
            "relative_delta": round(delta / old, 4) if old else None,
        }
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "unknown"


def _working_tree_dirty(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Retrieval v2 {report['dataset']['role'].title()} Result",
        "",
        "> Deterministic offline evidence; vector quality is not production semantic quality.",
        "",
        f"- Dataset: `{report['dataset']['name']}` ({report['dataset']['query_count']} queries)",
        f"- Git commit: `{report['environment']['git_commit']}`",
        f"- V2 weights: `{report['frozen_v2_configuration']}`",
        "",
        "| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@5 | p50 ms | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label in MODE_MAP:
        metrics = report["modes"][label]["all"]
        timing = report["latency_ms"].get(label, {})
        p50 = f"{timing['p50']:.4f}" if timing else "-"
        p95 = f"{timing['p95']:.4f}" if timing else "-"
        lines.append(
            f"| {label} | {metrics['recall_at_1']:.4f} | {metrics['recall_at_3']:.4f} | "
            f"{metrics['recall_at_5']:.4f} | {metrics['mrr_at_5']:.4f} | "
            f"{p50} | {p95} |"
        )
    lines.extend(
        [
            "",
            "## Hybrid category metrics",
            "",
            "| Query type | V1 Recall@5 | V1 MRR@5 | V2 Recall@5 | V2 MRR@5 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for query_type, v2_metrics in report["modes"]["hybrid_v2"]["by_query_type"].items():
        v1_metrics = report["modes"]["hybrid_v1"]["by_query_type"][query_type]
        lines.append(
            f"| {query_type} | {v1_metrics['recall_at_5']:.4f} | "
            f"{v1_metrics['mrr_at_5']:.4f} | {v2_metrics['recall_at_5']:.4f} | "
            f"{v2_metrics['mrr_at_5']:.4f} |"
        )
    candidate = report["candidate_recall"]
    graph = report["graph_aware_context_v2"]
    lines.extend(
        [
            "",
            "## Candidate recall",
            "",
            "- V1 sources @20/@50: "
            f"{candidate['v1_lexical_vector']['candidate_recall_at_20']:.4f} / "
            f"{candidate['v1_lexical_vector']['candidate_recall_at_50']:.4f}",
            "- V2 sources @20/@50: "
            f"{candidate['v2_lexical_vector_symbol']['candidate_recall_at_20']:.4f} / "
            f"{candidate['v2_lexical_vector_symbol']['candidate_recall_at_50']:.4f}",
            "",
            "## Graph context",
            "",
            f"- Case hit rate: {graph['case_hit_rate']:.4f}",
            f"- Relevant target coverage: {graph['relevant_target_coverage']:.4f}",
            "",
            "Real embedding benchmark: not executed.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
