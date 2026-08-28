from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from axiom.rag.models import CodeContextItem, CodeSearchResult

RETRIEVAL_SCHEMA_VERSION = 1
QUERY_TYPES = {
    "lexical-oriented",
    "semantic/paraphrase-oriented",
    "symbol-oriented",
    "graph/context-oriented",
}
SECRET_FIELD_FRAGMENTS = ("api_key", "authorization", "credential", "secret", "token")


@dataclass(frozen=True, slots=True)
class RelevantTarget:
    path: str
    symbol: str | None = None
    chunk: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    id: str
    query: str
    relevant: tuple[RelevantTarget, ...]
    category: str
    query_type: str
    notes: str


@dataclass(frozen=True, slots=True)
class RetrievalDataset:
    name: str
    version: str
    cases: tuple[RetrievalCase, ...]
    metadata: dict[str, Any]
    schema_version: int = RETRIEVAL_SCHEMA_VERSION


class DeterministicHashEmbeddingProvider:
    """Offline feature-hashing fixture, not a semantic embedding model."""

    provider_name = "deterministic-offline"
    model_name = "token-char-hash-v1"

    def __init__(self, dimensions: int = 384) -> None:
        if dimensions < 32:
            raise ValueError("dimensions must be at least 32")
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        features = _features(text)
        vector = [0.0] * self._dimensions
        for feature, weight in features:
            digest = hashlib.sha256(feature.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "little") % self._dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            vector[-1] = 1.0
            return vector
        return [value / norm for value in vector]


def load_retrieval_dataset(path: str | Path, *, repository_root: str | Path) -> RetrievalDataset:
    source = Path(path)
    root = Path(repository_root).resolve()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"retrieval dataset not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid retrieval dataset JSON at line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValueError("retrieval dataset must be a JSON object")
    schema_version = int(raw.get("schema_version") or RETRIEVAL_SCHEMA_VERSION)
    if schema_version != RETRIEVAL_SCHEMA_VERSION:
        raise ValueError(f"unsupported retrieval dataset schema version: {schema_version}")
    name = _required_text(raw, "name")
    version = _required_text(raw, "version")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("retrieval dataset cases must be a non-empty list")
    cases = tuple(_parse_case(item, root=root) for item in raw_cases)
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("retrieval case ids must be unique")
    queries = [case.query.casefold() for case in cases]
    if len(queries) != len(set(queries)):
        raise ValueError("retrieval case queries must be unique")
    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("retrieval dataset metadata must be an object")
    return RetrievalDataset(
        name=name,
        version=version,
        cases=cases,
        metadata={str(key): value for key, value in metadata.items()},
        schema_version=schema_version,
    )


def evaluate_rankings(
    cases: Sequence[RetrievalCase],
    rankings: dict[str, Sequence[CodeSearchResult]],
) -> dict[str, float]:
    if not cases:
        return {
            "recall_at_1": 0.0,
            "recall_at_3": 0.0,
            "recall_at_5": 0.0,
            "mrr_at_5": 0.0,
            "top_1_accuracy": 0.0,
            "relevant_coverage_at_5": 0.0,
        }
    hits = {1: 0, 3: 0, 5: 0}
    reciprocal_ranks: list[float] = []
    coverages: list[float] = []
    for case in cases:
        ranked = list(rankings.get(case.id, ()))
        first_rank = _first_relevant_rank(case, ranked[:5])
        reciprocal_ranks.append(1.0 / first_rank if first_rank is not None else 0.0)
        for limit in hits:
            if _first_relevant_rank(case, ranked[:limit]) is not None:
                hits[limit] += 1
        matched = {
            target
            for target in case.relevant
            if any(_result_matches(result, target) for result in ranked[:5])
        }
        coverages.append(len(matched) / len(case.relevant))
    total = len(cases)
    return {
        "recall_at_1": round(hits[1] / total, 4),
        "recall_at_3": round(hits[3] / total, 4),
        "recall_at_5": round(hits[5] / total, 4),
        "mrr_at_5": round(sum(reciprocal_ranks) / total, 4),
        "top_1_accuracy": round(hits[1] / total, 4),
        "relevant_coverage_at_5": round(sum(coverages) / total, 4),
    }


def evaluate_candidate_recall(
    cases: Sequence[RetrievalCase],
    candidate_sources: Sequence[dict[str, Sequence[CodeSearchResult]]],
    *,
    cutoffs: Sequence[int] = (20, 50),
) -> dict[str, float]:
    """Compute query-level candidate hit rate before final ranking."""
    if any(cutoff <= 0 for cutoff in cutoffs):
        raise ValueError("candidate recall cutoffs must be positive")
    if not cases:
        return {f"candidate_recall_at_{cutoff}": 0.0 for cutoff in cutoffs}
    return {
        f"candidate_recall_at_{cutoff}": round(
            sum(
                any(
                    first_relevant_rank(case, source.get(case.id, ())[:cutoff]) is not None
                    for source in candidate_sources
                )
                for case in cases
            )
            / len(cases),
            4,
        )
        for cutoff in cutoffs
    }


def evaluate_by_query_type(
    cases: Sequence[RetrievalCase],
    rankings: dict[str, Sequence[CodeSearchResult]],
) -> dict[str, dict[str, float]]:
    return {
        query_type: evaluate_rankings(
            [case for case in cases if case.query_type == query_type],
            rankings,
        )
        for query_type in sorted({case.query_type for case in cases})
    }


def first_relevant_rank(
    case: RetrievalCase,
    ranked: Sequence[CodeSearchResult],
) -> int | None:
    for rank, result in enumerate(ranked, start=1):
        if any(result_matches_target(result, target) for target in case.relevant):
            return rank
    return None


def result_matches_target(result: CodeSearchResult, target: RelevantTarget) -> bool:
    if result.path != target.path:
        return False
    if target.chunk and result.chunk_id != target.chunk:
        return False
    if not target.symbol:
        return True
    names = {result.symbol_name, result.qualified_name}
    if target.symbol in names:
        return True
    return bool(result.content and target.symbol in result.content)


def evaluate_context_coverage(
    cases: Sequence[RetrievalCase],
    contexts: dict[str, Sequence[CodeContextItem]],
) -> dict[str, float]:
    if not cases:
        return {"case_hit_rate": 0.0, "relevant_target_coverage": 0.0}
    case_hits = 0
    coverage_values: list[float] = []
    for case in cases:
        items = list(contexts.get(case.id, ()))
        matched = {
            target
            for target in case.relevant
            if any(_context_matches(item, target) for item in items)
        }
        case_hits += bool(matched)
        coverage_values.append(len(matched) / len(case.relevant))
    return {
        "case_hit_rate": round(case_hits / len(cases), 4),
        "relevant_target_coverage": round(sum(coverage_values) / len(cases), 4),
    }


def dataset_distribution(dataset: RetrievalDataset) -> dict[str, dict[str, int]]:
    return {
        "categories": dict(sorted(Counter(case.category for case in dataset.cases).items())),
        "query_types": dict(sorted(Counter(case.query_type for case in dataset.cases).items())),
    }


def validate_dataset_splits(
    datasets: Sequence[RetrievalDataset],
    *,
    expected_query_type_counts: dict[str, int] | None = None,
) -> None:
    """Reject leakage and malformed category balance across retrieval splits."""
    seen_ids: set[str] = set()
    seen_queries: set[str] = set()
    for dataset in datasets:
        for case in dataset.cases:
            if case.id in seen_ids:
                raise ValueError(f'retrieval case id appears in multiple splits: "{case.id}"')
            normalized_query = normalize_query_identity(case.query)
            if normalized_query in seen_queries:
                raise ValueError(f'retrieval query appears in multiple splits: "{case.query}"')
            seen_ids.add(case.id)
            seen_queries.add(normalized_query)
        if expected_query_type_counts is not None:
            counts = Counter(case.query_type for case in dataset.cases)
            if dict(counts) != expected_query_type_counts:
                raise ValueError(
                    f'retrieval split "{dataset.name}" has query-type counts {dict(counts)}; '
                    f"expected {expected_query_type_counts}"
                )


def normalize_query_identity(query: str) -> str:
    return " ".join(query.casefold().split())


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between zero and one")
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def serialize_report(report: dict[str, Any]) -> str:
    _reject_secret_fields(report)
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _parse_case(raw: object, *, root: Path) -> RetrievalCase:
    if not isinstance(raw, dict):
        raise ValueError("every retrieval case must be an object")
    case_id = _required_text(raw, "id")
    query = _required_text(raw, "query")
    category = _required_text(raw, "category")
    query_type = _required_text(raw, "query_type")
    notes = _required_text(raw, "notes")
    if query_type not in QUERY_TYPES:
        raise ValueError(f'retrieval case "{case_id}" has invalid query_type: {query_type}')
    raw_relevant = raw.get("relevant")
    if not isinstance(raw_relevant, list) or not raw_relevant:
        raise ValueError(f'retrieval case "{case_id}" relevant must be a non-empty list')
    relevant = tuple(_parse_target(case_id, item, root=root) for item in raw_relevant)
    if len(relevant) != len(set(relevant)):
        raise ValueError(f'retrieval case "{case_id}" has duplicate relevant targets')
    return RetrievalCase(case_id, query, relevant, category, query_type, notes)


def _parse_target(case_id: str, raw: object, *, root: Path) -> RelevantTarget:
    if not isinstance(raw, dict):
        raise ValueError(f'retrieval case "{case_id}" relevant targets must be objects')
    path = _required_text(raw, "path")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != path:
        raise ValueError(f'retrieval case "{case_id}" has invalid ground-truth path: {path}')
    target_path = (root / Path(*pure.parts)).resolve()
    try:
        target_path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f'retrieval case "{case_id}" ground truth escapes repository') from exc
    if not target_path.is_file():
        raise ValueError(f'retrieval case "{case_id}" ground-truth file does not exist: {path}')
    symbol = _optional_text(raw.get("symbol"))
    chunk = _optional_text(raw.get("chunk"))
    if symbol:
        source = target_path.read_text(encoding="utf-8", errors="ignore")
        if symbol not in source:
            raise ValueError(
                f'retrieval case "{case_id}" symbol is not present in ground-truth file: '
                f"{path}::{symbol}"
            )
    return RelevantTarget(path=path, symbol=symbol, chunk=chunk)


def _first_relevant_rank(case: RetrievalCase, ranked: Sequence[CodeSearchResult]) -> int | None:
    return first_relevant_rank(case, ranked)


def _result_matches(result: CodeSearchResult, target: RelevantTarget) -> bool:
    return result_matches_target(result, target)


def _context_matches(item: CodeContextItem, target: RelevantTarget) -> bool:
    if item.file_path != target.path:
        return False
    if target.chunk and item.chunk_id != target.chunk:
        return False
    return not target.symbol or target.symbol in item.content


def _features(text: str) -> list[tuple[str, float]]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).casefold()
    tokens = re.findall(r"[a-z0-9]+", normalized.replace("_", " "))
    features: list[tuple[str, float]] = [(f"token:{token}", 2.0) for token in tokens]
    features.extend(
        (f"bigram:{left}:{right}", 1.5) for left, right in zip(tokens, tokens[1:], strict=False)
    )
    compact = " ".join(tokens)
    features.extend(
        (f"char3:{compact[index : index + 3]}", 0.25) for index in range(max(0, len(compact) - 2))
    )
    return features


def _required_text(data: dict[str, Any], field: str) -> str:
    value = str(data.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _reject_secret_fields(value: object, path: str = "result") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).casefold()
            if any(fragment in normalized for fragment in SECRET_FIELD_FRAGMENTS):
                raise ValueError(
                    f"secret-like field is not allowed in benchmark artifact: {path}.{key}"
                )
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")
