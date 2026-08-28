from __future__ import annotations

import argparse
import platform
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.config import EmbeddingConfig
from axiom.rag import CodeIndex
from axiom.rag.models import CodeSearchResult
from axiom.rag.tokenizer import normalize_text
from benchmarks.retrieval.evaluation import (
    DeterministicHashEmbeddingProvider,
    RetrievalCase,
    evaluate_candidate_recall,
    evaluate_rankings,
    first_relevant_rank,
    load_retrieval_dataset,
    serialize_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze v1 Hybrid RRF retrieval misses")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--dataset", type=Path, default=Path("benchmarks/retrieval/dataset.json"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/retrieval/analysis/v1-failure-analysis.json"),
    )
    parser.add_argument("--dimensions", type=int, default=384)
    args = parser.parse_args()

    root = args.repository.resolve()
    dataset = load_retrieval_dataset(args.dataset, repository_root=root)
    with tempfile.TemporaryDirectory(
        prefix="axiom-v1-analysis-", ignore_cleanup_errors=True
    ) as tmp:
        index = CodeIndex(
            root,
            db_path=Path(tmp) / "index.sqlite3",
            embedding_provider=DeterministicHashEmbeddingProvider(args.dimensions),
            search_config=EmbeddingConfig(
                enabled=True,
                search_mode="hybrid",
                dimensions=args.dimensions,
                candidate_limit=200,
            ),
        )
        index.update(root / "src" / "axiom")
        rankings = {
            mode: {
                case.id: index.search(case.query, limit=200, mode=mode) for case in dataset.cases
            }
            for mode in ("lexical", "vector", "hybrid")
        }
        contexts = {
            case.id: index.build_code_context(
                case.query,
                mode="hybrid",
                max_seed_chunks=5,
                max_graph_depth=2,
                max_items=30,
            ).items
            for case in dataset.cases
            if case.query_type == "graph/context-oriented"
        }
        definitions = index.store.list_symbol_definitions()

    misses = [
        _analyze_case(case, rankings, contexts, definitions)
        for case in dataset.cases
        if first_relevant_rank(case, rankings["hybrid"][case.id][:5]) is None
    ]
    query_types = sorted({case.query_type for case in dataset.cases})
    report = {
        "schema_version": 1,
        "analysis": "v1-hybrid-top5-failure-analysis",
        "dataset_version": dataset.version,
        "query_count": len(dataset.cases),
        "failed_query_count": len(misses),
        "environment": {
            "git_commit": _git_commit(root),
            "python": platform.python_version(),
            "timestamp": datetime.now(UTC).isoformat(),
        },
        "definitions": {
            "candidate_pool": "deduplicated union of each v1 source's top 200 candidates",
            "candidate_generation_miss": "no relevant target in either source's top 200",
            "ranking_miss": "relevant target is in the pool but Hybrid RRF does not rank it top 5",
            "candidate_recall_at_k": "a hit exists in any source's top K candidates",
        },
        "failure_class_counts": dict(
            sorted(Counter(item["failure_classification"] for item in misses).items())
        ),
        "candidate_pool_counts": dict(
            sorted(Counter(item["candidate_pool_status"] for item in misses).items())
        ),
        "candidate_recall": evaluate_candidate_recall(
            dataset.cases,
            (rankings["lexical"], rankings["vector"]),
        ),
        "category_metrics": {
            mode: {
                query_type: evaluate_rankings(
                    [case for case in dataset.cases if case.query_type == query_type],
                    mode_rankings,
                )
                for query_type in query_types
            }
            for mode, mode_rankings in rankings.items()
        },
        "misses": misses,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(serialize_report(report), encoding="utf-8")
    args.output.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(_markdown(report))


def _analyze_case(
    case: RetrievalCase,
    rankings: dict[str, dict[str, list[CodeSearchResult]]],
    contexts: dict[str, list[Any]],
    definitions: list[Any],
) -> dict[str, Any]:
    lexical = rankings["lexical"][case.id]
    vector = rankings["vector"][case.id]
    hybrid = rankings["hybrid"][case.id]
    lexical_rank = first_relevant_rank(case, lexical)
    vector_rank = first_relevant_rank(case, vector)
    hybrid_rank = first_relevant_rank(case, hybrid)
    pool_hit = first_relevant_rank(case, _dedupe([*lexical, *vector])) is not None
    classification, explanation = _classify(
        case,
        lexical_rank=lexical_rank,
        vector_rank=vector_rank,
        hybrid_rank=hybrid_rank,
        pool_hit=pool_hit,
        graph_hit=_context_hit(case, contexts.get(case.id, [])),
        symbol_indexed=_expected_symbol_indexed(case, definitions),
    )
    return {
        "query_id": case.id,
        "query": case.query,
        "query_type": case.query_type,
        "expected_targets": [
            {"path": target.path, "symbol": target.symbol, "chunk": target.chunk}
            for target in case.relevant
        ],
        "lexical_top5": [_summary(result) for result in lexical[:5]],
        "vector_top5": [_summary(result) for result in vector[:5]],
        "hybrid_top5": [_summary(result) for result in hybrid[:5]],
        "first_relevant_rank_if_after_5": hybrid_rank
        if hybrid_rank is not None and hybrid_rank > 5
        else None,
        "candidate_pool_status": "present_but_ranked_too_low"
        if pool_hit
        else "candidate_generation_miss",
        "failure_classification": classification,
        "technical_explanation": explanation,
    }


def _classify(
    case: RetrievalCase,
    *,
    lexical_rank: int | None,
    vector_rank: int | None,
    hybrid_rank: int | None,
    pool_hit: bool,
    graph_hit: bool,
    symbol_indexed: bool,
) -> tuple[str, str]:
    ranks = f"lexical={lexical_rank}, vector={vector_rank}, hybrid={hybrid_rank}"
    if not pool_hit:
        if case.query_type == "symbol-oriented" and not symbol_indexed:
            return (
                "symbol target missing",
                f"Expected symbol is absent from the symbol index; {ranks}.",
            )
        if case.query_type == "graph/context-oriented" and not graph_hit:
            return (
                "graph edge / graph expansion missing",
                f"Neither candidate source nor bounded v1 graph context reached a target; {ranks}.",
            )
        if case.query_type == "semantic/paraphrase-oriented":
            return (
                "semantic mismatch",
                f"The paraphrase matched neither lexical nor deterministic-vector top 50; {ranks}.",
            )
        return "candidate-generation miss", f"No target entered either top-50 source; {ranks}."
    if lexical_rank is None:
        return (
            "lexical mismatch",
            "Vector generated a target but lexical retrieval did not; "
            f"fusion kept it below 5; {ranks}.",
        )
    if vector_rank is None:
        return (
            "semantic mismatch",
            "Lexical generated a target but deterministic vector did not; "
            f"fusion kept it below 5; {ranks}.",
        )
    return (
        "correct candidate exists but ranking too low",
        f"A source generated a target, but Hybrid RRF ranked it below 5; {ranks}.",
    )


def _dedupe(results: list[CodeSearchResult]) -> list[CodeSearchResult]:
    seen: set[tuple[str, str | None, str | None]] = set()
    output: list[CodeSearchResult] = []
    for result in results:
        key = (result.path, result.qualified_name, result.chunk_id)
        if key not in seen:
            seen.add(key)
            output.append(result)
    return output


def _expected_symbol_indexed(case: RetrievalCase, definitions: list[Any]) -> bool:
    expected = {
        (target.path, normalize_text(target.symbol).replace(" ", ""))
        for target in case.relevant
        if target.symbol
    }
    if not expected:
        return True
    indexed = {
        (definition.file_path, normalize_text(value).replace(" ", ""))
        for definition in definitions
        for value in (definition.name, definition.qualified_name)
    }
    return bool(expected & indexed)


def _context_hit(case: RetrievalCase, items: list[Any]) -> bool:
    return any(
        item.file_path == target.path and (not target.symbol or target.symbol in item.content)
        for item in items
        for target in case.relevant
    )


def _summary(result: CodeSearchResult) -> dict[str, Any]:
    return {
        "path": result.path,
        "line": result.line,
        "symbol": result.symbol_name,
        "qualified_name": result.qualified_name,
        "chunk": result.chunk_id,
    }


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
        "# Retrieval v1 Failure Analysis",
        "",
        f"- Queries: {report['query_count']}",
        f"- Hybrid Top-5 misses: {report['failed_query_count']}",
        f"- Git commit: `{report['environment']['git_commit']}`",
        "",
        "## Candidate pool diagnosis",
        "",
        "| Outcome | Count |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in report["candidate_pool_counts"].items())
    lines.extend(
        ["", "## Primary failure classification", "", "| Failure type | Count |", "| --- | ---: |"]
    )
    lines.extend(f"| {name} | {count} |" for name, count in report["failure_class_counts"].items())
    candidate = report["candidate_recall"]
    lines.extend(
        [
            "",
            "## Candidate recall",
            "",
            f"- Candidate Recall@20: {candidate['candidate_recall_at_20']:.4f}",
            f"- Candidate Recall@50: {candidate['candidate_recall_at_50']:.4f}",
            "",
            "## Miss cases",
            "",
            "| ID | Query type | Failure | Candidate status | First Hybrid rank after 5 |",
            "| --- | --- | --- | --- | ---: |",
        ]
    )
    for miss in report["misses"]:
        lines.append(
            f"| {miss['query_id']} | {miss['query_type']} | {miss['failure_classification']} | "
            f"{miss['candidate_pool_status']} | {miss['first_relevant_rank_if_after_5'] or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
