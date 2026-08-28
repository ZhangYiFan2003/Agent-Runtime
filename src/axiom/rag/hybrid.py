from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from axiom.rag.models import CodeSearchResult
from axiom.rag.ranking import RankedRow, rank_candidate_rows

RRF_K = 60


@dataclass(slots=True)
class FusionWeights:
    lexical: float = 0.55
    vector: float = 0.45
    symbol: float = 0.0
    rrf_k: int = RRF_K


def reciprocal_rank_fusion(
    lexical_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
    query: str,
    limit: int,
    *,
    weights: FusionWeights,
    backend: str = "hybrid",
    symbol_rows: list[dict[str, Any]] | None = None,
) -> list[CodeSearchResult]:
    lexical_ranked = rank_candidate_rows(lexical_rows, query)
    vector_ranked = rank_candidate_rows(vector_rows, query)
    symbol_ranked = sorted(
        symbol_rows or [],
        key=lambda row: (
            -float(row.get("symbol_score") or 0.0),
            row["file_path"],
            row["start_line"],
        ),
    )
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    scores: dict[tuple[str, str, str], float] = {}
    lexical_scores: dict[tuple[str, str, str], float] = {}
    vector_scores: dict[tuple[str, str, str], float] = {}
    symbol_scores: dict[tuple[str, str, str], float] = {}
    lexical_ranks: dict[tuple[str, str, str], int] = {}
    vector_ranks: dict[tuple[str, str, str], int] = {}
    symbol_ranks: dict[tuple[str, str, str], int] = {}
    fields: dict[tuple[str, str, str], set[str]] = {}

    for rank, item in enumerate(lexical_ranked, start=1):
        key = _key(item.row)
        by_key.setdefault(key, item.row)
        lexical_ranks[key] = rank
        lexical_scores[key] = item.score
        fields.setdefault(key, set()).update(item.matched_fields)
        scores[key] = scores.get(key, 0.0) + weights.lexical / (weights.rrf_k + rank)

    for rank, item in enumerate(vector_ranked, start=1):
        key = _key(item.row)
        by_key.setdefault(key, item.row)
        vector_ranks[key] = rank
        vector_scores[key] = float(item.row.get("vector_score") or 0.0)
        fields.setdefault(key, set()).update(item.matched_fields)
        scores[key] = scores.get(key, 0.0) + weights.vector / (weights.rrf_k + rank)

    for rank, row in enumerate(symbol_ranked, start=1):
        key = _key(row)
        by_key.setdefault(key, row)
        symbol_ranks[key] = rank
        symbol_scores[key] = float(row.get("symbol_score") or 0.0)
        fields.setdefault(key, set()).update(row.get("symbol_matched_fields") or ())
        scores[key] = scores.get(key, 0.0) + weights.symbol / (weights.rrf_k + rank)

    ordered = sorted(
        scores,
        key=lambda key: (
            -scores[key],
            lexical_ranks.get(key, 10**9),
            vector_ranks.get(key, 10**9),
            symbol_ranks.get(key, 10**9),
            by_key[key]["file_path"],
            by_key[key]["start_line"],
        ),
    )
    results: list[CodeSearchResult] = []
    for key in ordered[:limit]:
        row = by_key[key]
        results.append(
            CodeSearchResult(
                path=str(row["file_path"]),
                line=int(row["start_line"]),
                snippet=_snippet(str(row["content"])),
                chunk_id=str(row.get("chunk_id") or "") or None,
                end_line=int(row["end_line"]) if row.get("end_line") is not None else None,
                content=str(row["content"]),
                chunk_type=str(row["chunk_type"]),
                symbol_name=row.get("symbol_name"),
                qualified_name=row.get("qualified_name"),
                score=scores[key],
                backend=backend,
                matched_fields=tuple(sorted(fields.get(key, set()))),
                lexical_score=lexical_scores.get(key),
                vector_score=vector_scores.get(key),
                symbol_score=symbol_scores.get(key),
                fusion_score=scores[key],
                lexical_rank=lexical_ranks.get(key),
                vector_rank=vector_ranks.get(key),
                symbol_rank=symbol_ranks.get(key),
                embedding_profile=row.get("embedding_profile"),
            )
        )
    return results


def symbol_results(
    symbol_rows: list[dict[str, Any]],
    limit: int,
) -> list[CodeSearchResult]:
    ordered = sorted(
        symbol_rows,
        key=lambda row: (
            -float(row.get("symbol_score") or 0.0),
            row["file_path"],
            row["start_line"],
        ),
    )
    seen: set[tuple[str, str, str]] = set()
    results: list[CodeSearchResult] = []
    for row in ordered:
        key = _key(row)
        if key in seen:
            continue
        seen.add(key)
        rank = len(results) + 1
        results.append(
            CodeSearchResult(
                path=str(row["file_path"]),
                line=int(row["start_line"]),
                snippet=_snippet(str(row["content"])),
                chunk_id=str(row.get("chunk_id") or "") or None,
                end_line=int(row["end_line"]) if row.get("end_line") is not None else None,
                content=str(row["content"]),
                chunk_type=str(row["chunk_type"]),
                symbol_name=row.get("symbol_name"),
                qualified_name=row.get("qualified_name"),
                score=float(row.get("symbol_score") or 0.0),
                backend="symbol",
                matched_fields=tuple(row.get("symbol_matched_fields") or ()),
                symbol_score=float(row.get("symbol_score") or 0.0),
                symbol_rank=rank,
            )
        )
        if len(results) >= limit:
            break
    return results


def vector_results(
    vector_rows: list[dict[str, Any]],
    query: str,
    limit: int,
) -> list[CodeSearchResult]:
    ranked = rank_candidate_rows(vector_rows, query)
    ranked.sort(
        key=lambda item: (
            -float(item.row.get("vector_score") or 0.0),
            item.row["file_path"],
            item.row["start_line"],
        )
    )
    deduped = _dedupe(ranked)
    return [
        CodeSearchResult(
            path=str(item.row["file_path"]),
            line=int(item.row["start_line"]),
            snippet=_snippet(str(item.row["content"])),
            chunk_id=str(item.row.get("chunk_id") or "") or None,
            end_line=int(item.row["end_line"]) if item.row.get("end_line") is not None else None,
            content=str(item.row["content"]),
            chunk_type=str(item.row["chunk_type"]),
            symbol_name=item.row.get("symbol_name"),
            qualified_name=item.row.get("qualified_name"),
            score=float(item.row.get("vector_score") or 0.0),
            backend="vector",
            matched_fields=item.matched_fields,
            vector_score=float(item.row.get("vector_score") or 0.0),
            vector_rank=index,
            embedding_profile=item.row.get("embedding_profile"),
        )
        for index, item in enumerate(deduped[:limit], start=1)
    ]


def _dedupe(items: list[RankedRow]) -> list[RankedRow]:
    best: dict[tuple[str, str, str], RankedRow] = {}
    for item in items:
        key = _key(item.row)
        previous = best.get(key)
        if previous is None or float(item.row.get("vector_score") or 0.0) > float(
            previous.row.get("vector_score") or 0.0
        ):
            best[key] = item
    return sorted(
        best.values(),
        key=lambda item: (
            -float(item.row.get("vector_score") or 0.0),
            item.row["file_path"],
            item.row["start_line"],
        ),
    )


def _key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("file_path") or ""),
        str(row.get("qualified_name") or ""),
        str(row.get("content_hash") or ""),
    )


def _snippet(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
