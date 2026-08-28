from __future__ import annotations

from dataclasses import dataclass

from axiom.rag.models import CodeChunk, SymbolDefinition
from axiom.rag.tokenizer import compact_identifier, identifier_forms, tokenize_query


@dataclass(frozen=True, slots=True)
class SymbolCandidate:
    definition: SymbolDefinition
    chunk: CodeChunk
    score: float
    matched_fields: tuple[str, ...]


def rank_symbol_candidates(
    query: str,
    definitions: list[SymbolDefinition],
    chunks: list[CodeChunk],
    *,
    limit: int,
) -> list[SymbolCandidate]:
    """Rank definition-backed chunks using general identifier and path evidence."""
    if limit <= 0:
        return []
    chunks_by_id = {chunk.id: chunk for chunk in chunks}
    query_forms = identifier_forms(query)
    query_tokens = set(tokenize_query(query))
    best_by_chunk: dict[str, SymbolCandidate] = {}
    for definition in definitions:
        if not definition.chunk_id or definition.chunk_id not in chunks_by_id:
            continue
        score, fields = _score_definition(definition, query_forms, query_tokens)
        if score <= 0:
            continue
        candidate = SymbolCandidate(
            definition=definition,
            chunk=chunks_by_id[definition.chunk_id],
            score=score,
            matched_fields=tuple(fields),
        )
        previous = best_by_chunk.get(definition.chunk_id)
        if previous is None or candidate.score > previous.score:
            best_by_chunk[definition.chunk_id] = candidate
    return sorted(
        best_by_chunk.values(),
        key=lambda item: (
            -item.score,
            item.definition.file_path,
            item.definition.start_line,
            item.definition.qualified_name,
        ),
    )[:limit]


def _score_definition(
    definition: SymbolDefinition,
    query_forms: set[str],
    query_tokens: set[str],
) -> tuple[float, list[str]]:
    name_form = compact_identifier(definition.name)
    qualified_form = compact_identifier(definition.qualified_name)
    name_tokens = set(tokenize_query(definition.name))
    score = 0.0
    fields: list[str] = []

    if name_form and name_form in query_forms:
        score = 120.0
        fields.append("exact_symbol")
    elif qualified_form and qualified_form in query_forms:
        score = 110.0
        fields.append("qualified_name")
    elif name_tokens and name_tokens <= query_tokens:
        score = 80.0 + len(name_tokens)
        fields.append("normalized_symbol")
    elif name_tokens:
        overlap = len(name_tokens & query_tokens) / len(name_tokens)
        if overlap >= 0.5:
            score = 40.0 * overlap
            fields.append("partial_symbol")

    path_tokens = set(tokenize_query(definition.file_path))
    path_overlap = len(path_tokens & query_tokens)
    if score > 0 and path_overlap:
        score += min(path_overlap * 4.0, 12.0)
        fields.append("file_path")
    return score, fields
