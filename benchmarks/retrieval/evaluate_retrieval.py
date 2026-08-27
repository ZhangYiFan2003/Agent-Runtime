from __future__ import annotations

import argparse
import platform
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from axiom.config import EmbeddingConfig
from axiom.rag import CodeIndex
from benchmarks.retrieval.evaluation import (
    DeterministicHashEmbeddingProvider,
    dataset_distribution,
    evaluate_context_coverage,
    evaluate_rankings,
    load_retrieval_dataset,
    serialize_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate repository-grounded code retrieval")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/retrieval/dataset.json"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/retrieval/results/retrieval-offline-baseline.json"),
    )
    parser.add_argument("--dimensions", type=int, default=384)
    args = parser.parse_args()

    root = args.repository.resolve()
    dataset = load_retrieval_dataset(args.dataset, repository_root=root)
    with tempfile.TemporaryDirectory(
        prefix="axiom-retrieval-", ignore_cleanup_errors=True
    ) as raw_tmp:
        db_path = Path(raw_tmp) / "index.sqlite3"
        provider = DeterministicHashEmbeddingProvider(args.dimensions)
        config = EmbeddingConfig(
            enabled=True,
            search_mode="hybrid",
            dimensions=args.dimensions,
            candidate_limit=200,
        )
        index = CodeIndex(
            root,
            db_path=db_path,
            embedding_provider=provider,
            search_config=config,
        )
        # The maintained ground truth is intentionally scoped to production
        # package code. Avoid indexing caches, benchmark artifacts, or tests.
        stats = index.update(root / "src" / "axiom")
        rankings = {
            mode: {case.id: index.search(case.query, limit=5, mode=mode) for case in dataset.cases}
            for mode in ("lexical", "vector", "hybrid")
        }
        graph_cases = [
            case for case in dataset.cases if case.query_type == "graph/context-oriented"
        ]
        contexts = {
            case.id: index.build_code_context(
                case.query,
                mode="hybrid",
                max_seed_chunks=5,
                max_graph_depth=2,
                max_items=30,
            ).items
            for case in graph_cases
        }

    report = {
        "schema_version": 1,
        "benchmark": "axiom-repository-retrieval",
        "benchmark_type": "offline deterministic fixture",
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "query_count": len(dataset.cases),
            **dataset_distribution(dataset),
        },
        "environment": {
            "git_commit": _git_commit(root),
            "working_tree_dirty": _working_tree_dirty(root),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "timestamp": datetime.now(UTC).isoformat(),
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
            "relevant_coverage_at_5": "mean fraction of all relevant targets present in top 5",
            "context_case_hit_rate": "fraction of graph cases with any target in bounded context",
            "context_target_coverage": "mean fraction of graph-case targets in bounded context",
        },
        "modes": {
            mode: {
                "all": evaluate_rankings(dataset.cases, mode_rankings),
                "by_query_type": {
                    query_type: evaluate_rankings(
                        [case for case in dataset.cases if case.query_type == query_type],
                        mode_rankings,
                    )
                    for query_type in sorted({case.query_type for case in dataset.cases})
                },
            }
            for mode, mode_rankings in rankings.items()
        },
        "graph_aware_context": {
            "scope": "bounded context coverage; not ranked retrieval Recall",
            **evaluate_context_coverage(graph_cases, contexts),
        },
        "real_embedding_benchmark": {
            "status": "not executed",
            "reason": (
                "offline baseline intentionally uses a deterministic token/character hash fixture"
            ),
        },
        "limitations": [
            (
                "Dataset is repository-specific and ground truth is maintained against this "
                "source tree."
            ),
            "The deterministic hash embedding is not a production semantic embedding model.",
            (
                "Vector scan is local and exhaustive; this is accuracy evidence, not a latency "
                "benchmark."
            ),
            "Graph-aware values are context coverage metrics and are not reported as Recall.",
        ],
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


def _working_tree_dirty(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _markdown(report: dict) -> str:
    lines = [
        "# Repository Retrieval Offline Baseline",
        "",
        "> Deterministic offline evidence. The hash embedding is not a semantic model.",
        "",
        f"- Git commit: `{report['environment']['git_commit']}`",
        f"- Timestamp: {report['environment']['timestamp']}",
        f"- Python: {report['environment']['python']}",
        f"- Queries: {report['dataset']['query_count']}",
        "",
        "| Mode | Recall@1 | Recall@3 | Recall@5 | MRR@5 | Relevant Coverage@5 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode in ("lexical", "vector", "hybrid"):
        metrics = report["modes"][mode]["all"]
        lines.append(
            f"| {mode} | {metrics['recall_at_1']:.4f} | {metrics['recall_at_3']:.4f} | "
            f"{metrics['recall_at_5']:.4f} | {metrics['mrr_at_5']:.4f} | "
            f"{metrics['relevant_coverage_at_5']:.4f} |"
        )
    context = report["graph_aware_context"]
    lines.extend(
        [
            "",
            "## Graph-aware bounded context",
            "",
            "These are context coverage metrics, not ranked retrieval Recall.",
            "",
            f"- Case hit rate: {context['case_hit_rate']:.4f}",
            f"- Relevant target coverage: {context['relevant_target_coverage']:.4f}",
            "",
            "## Boundary",
            "",
            "Real embedding benchmark: not executed.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
