from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from axiom.config import EmbeddingConfig
from axiom.rag.analysis import analyze_source
from axiom.rag.call_graph import (
    HARD_MAX_DEPTH,
    HARD_MAX_PATHS,
    build_call_edges,
    find_recursive_components,
    resolve_symbol_query,
    trace_call_paths,
)
from axiom.rag.embeddings import (
    EmbeddingError,
    EmbeddingProfile,
    EmbeddingProvider,
    build_embedding_profile,
    build_embedding_text,
    embedding_input_hash,
    infer_dimensions,
    is_embedding_eligible,
)
from axiom.rag.hybrid import FusionWeights, reciprocal_rank_fusion, symbol_results, vector_results
from axiom.rag.languages import SKIP_DIRS, detect_language, is_indexable
from axiom.rag.models import (
    CallEdge,
    CallPathResult,
    CodeChunk,
    CodeContextResult,
    CodeSearchResult,
    IndexedFile,
    IndexStats,
    RecursiveComponent,
    SymbolDefinition,
    SymbolReference,
)
from axiom.rag.ranking import rank_rows
from axiom.rag.store import CodeIndexStore
from axiom.rag.symbol_search import rank_symbol_candidates
from axiom.rag.symbols.resolver import resolve_import_paths, resolve_references
from axiom.rag.tokenizer import fts_any_query, fts_match_query, tokenize_code_text, tokenize_query
from axiom.rag.vectors import encode_vector, normalize_vector


class CodeIndex:
    def __init__(
        self,
        root: str | Path,
        db_path: str | Path | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        search_config: EmbeddingConfig | None = None,
    ):
        self.root = Path(root).resolve()
        self.db_path = (
            Path(db_path).expanduser() if db_path else self.root / ".axiom" / "code_index.sqlite3"
        )
        self.store = CodeIndexStore(self.db_path)
        self.last_stats = IndexStats()
        self.embedding_provider = embedding_provider
        self.search_config = search_config or EmbeddingConfig()
        self._embedding_profile: EmbeddingProfile | None = None
        self._symbol_candidate_data: tuple[list[SymbolDefinition], list[CodeChunk]] | None = None

    def rebuild(self, path: str | Path | None = None) -> int:
        self.last_stats = self.update(path, force=True)
        return self.last_stats.chunk_count

    def update(self, path: str | Path | None = None, *, force: bool = False) -> IndexStats:
        started = time.perf_counter()
        self._symbol_candidate_data = None
        base = self._resolve(path or self.root)
        files = [base] if base.is_file() else list(self._iter_files(base))
        scanned_paths = {self._relative(file_path) for file_path in files}
        stats = IndexStats(scanned_files=len(files))

        for file_path in files:
            rel = self._relative(file_path)
            try:
                file_hash = self._sha256(file_path)
                stat = file_path.stat()
                language = detect_language(file_path)
                indexed = self.store.get_file(rel)
                if not force and indexed and indexed.sha256 == file_hash:
                    stats.unchanged_files += 1
                    continue

                source = file_path.read_text(encoding="utf-8", errors="ignore")
                analysis = analyze_source(rel, language, source)
                indexed_file = IndexedFile(
                    path=rel,
                    language=language,
                    sha256=file_hash,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    indexed_at=datetime.now(UTC).isoformat(),
                    parse_status=analysis.parse_status,
                )
                self.store.replace_file_analysis(indexed_file, analysis)
                stats.indexed_files += 1
                stats.parsed_files += 1
                stats.definitions_updated += len(analysis.symbols)
                stats.imports_updated += len(analysis.imports)
                stats.references_extracted += len(analysis.references)
            except OSError:
                stats.failed_files += 1

        stats.deleted_files = self.remove_missing_files(base, scanned_paths)
        self._resolve_workspace_symbols(stats)
        if self.embedding_provider and self.search_config.enabled:
            self._merge_embedding_stats(stats, self.sync_embeddings())
        stats.chunk_count = self.store.count_chunks()
        stats.duration_ms = (time.perf_counter() - started) * 1000
        self.last_stats = stats
        return stats

    def remove_missing_files(
        self,
        path: str | Path | None = None,
        scanned_paths: set[str] | None = None,
    ) -> int:
        base = self._resolve(path or self.root)
        existing = scanned_paths
        if existing is None:
            files = [base] if base.is_file() else list(self._iter_files(base))
            existing = {self._relative(file_path) for file_path in files}

        missing: list[str] = []
        for stored_path in self.store.list_file_paths():
            absolute = (self.root / stored_path).resolve()
            if base.is_file() and absolute != base:
                continue
            if not base.is_file() and not _is_relative_to(absolute, base):
                continue
            if stored_path not in existing:
                missing.append(stored_path)
        return self.store.delete_files(missing)

    def sync_embeddings(self) -> IndexStats:
        stats = IndexStats()
        provider = self.embedding_provider
        if provider is None:
            return stats
        try:
            chunks = [chunk for chunk in self.store.list_chunks() if is_embedding_eligible(chunk)]
            if not chunks:
                return stats
            profile = self._profile_for_provider(provider, chunks[0])
            stats.embedding_profile = profile.id
            self.store.upsert_embedding_profile(profile)
            current_hashes = self.store.get_embedding_hashes(profile.id)
            pending: list[tuple[str, str, str]] = []
            for chunk in chunks:
                text = build_embedding_text(
                    chunk,
                    max_chars=self.search_config.max_input_chars,
                )
                input_hash = embedding_input_hash(text)
                if current_hashes.get(chunk.id) == input_hash:
                    stats.unchanged_embeddings += 1
                    continue
                pending.append((chunk.id, input_hash, text))
            if not pending:
                return stats
            texts = [item[2] for item in pending]
            vectors = provider.embed(texts)
            rows = [
                (
                    chunk_id,
                    input_hash,
                    encode_vector(vector, dimensions=profile.dimensions),
                )
                for (chunk_id, input_hash, _text), vector in zip(pending, vectors, strict=True)
            ]
            self.store.replace_embeddings(profile, rows)
            stats.embedded_chunks = len(rows)
        except (EmbeddingError, ValueError):
            stats.failed_embeddings += 1
        return stats

    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        mode: str = "auto",
    ) -> list[CodeSearchResult]:
        tokens = tokenize_query(query)
        if not tokens:
            return []

        selected_mode = self._resolve_search_mode(mode)
        if selected_mode == "lexical":
            return self._search_lexical(query, limit)
        if selected_mode == "lexical_v2":
            return self._search_lexical_v2(query, limit)
        if selected_mode == "vector":
            return self._search_vector(query, limit)
        if selected_mode == "hybrid":
            return self._search_hybrid(query, limit)
        if selected_mode == "symbol":
            return self._search_symbol(query, limit)
        if selected_mode == "hybrid_v2":
            return self._search_hybrid_v2(query, limit)
        return self._search_auto(query, limit)

    def build_code_context(
        self,
        query: str,
        *,
        mode: str = "auto",
        max_context_chars: int = 24_000,
        max_estimated_tokens: int = 6_000,
        max_seed_chunks: int = 8,
        max_graph_depth: int = 2,
        max_items: int = 30,
    ) -> CodeContextResult:
        from axiom.rag.context import CodeContextBuilder

        return CodeContextBuilder(self).build(
            query,
            mode=mode,
            max_context_chars=max_context_chars,
            max_estimated_tokens=max_estimated_tokens,
            max_seed_chunks=max_seed_chunks,
            max_graph_depth=max_graph_depth,
            max_items=max_items,
        )

    def find_definitions(
        self,
        name: str,
        *,
        file_path: str | None = None,
        language: str | None = None,
        limit: int = 20,
    ) -> list[SymbolDefinition]:
        return self.store.find_definitions(
            name,
            file_path=file_path,
            language=language,
            limit=limit,
        )

    def find_references(
        self,
        symbol_id_or_name: str,
        *,
        limit: int = 100,
    ) -> list[SymbolReference]:
        return self.store.find_references(symbol_id_or_name, limit=limit)

    def find_callers(
        self,
        symbol_id_or_name: str,
        *,
        limit: int = 100,
    ) -> list[CallEdge]:
        symbol = self._lookup_symbol(symbol_id_or_name)
        return self.store.find_callers(symbol.id, limit=limit)

    def find_callees(
        self,
        symbol_id_or_name: str,
        *,
        limit: int = 100,
    ) -> list[CallEdge]:
        symbol = self._lookup_symbol(symbol_id_or_name)
        return self.store.find_callees(symbol.id, limit=limit)

    def trace_call_paths(
        self,
        symbol_id_or_name: str,
        *,
        direction: str = "outgoing",
        max_depth: int = 3,
        max_paths: int = 100,
    ) -> CallPathResult:
        symbol = self._lookup_symbol(symbol_id_or_name)
        return trace_call_paths(
            symbol.id,
            self.store.list_call_edges(),
            direction=direction,
            max_depth=min(max_depth, HARD_MAX_DEPTH),
            max_paths=min(max_paths, HARD_MAX_PATHS),
        )

    def find_recursive_components(self) -> list[RecursiveComponent]:
        return find_recursive_components(self.store.list_call_edges())

    def resolve_symbol_at(
        self,
        file_path: str,
        line: int,
        column: int | None = None,
    ) -> SymbolDefinition | None:
        del column
        rel = self._relative(self._resolve(file_path))
        references = [
            reference
            for reference in self.store.list_symbol_references()
            if reference.file_path == rel and reference.start_line <= line <= reference.end_line
        ]
        for reference in references:
            if reference.resolved_symbol_id:
                definitions = self.store.find_definitions(reference.name, limit=100)
                for definition in definitions:
                    if definition.id == reference.resolved_symbol_id:
                        return definition
        candidates = [
            definition
            for definition in self.store.list_symbol_definitions()
            if definition.file_path == rel and definition.start_line <= line <= definition.end_line
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.end_line - item.start_line, item.start_line))
        return candidates[0]

    def _search_auto(self, query: str, limit: int) -> list[CodeSearchResult]:
        if not self.embedding_provider or not self.search_config.enabled:
            return self._search_lexical(query, limit)
        try:
            profile = self._current_profile()
            if profile is None or self.store.count_embeddings(profile.id) == 0:
                return self._search_lexical(query, limit)
            return self._search_hybrid(query, limit)
        except EmbeddingError:
            return self._search_lexical(query, limit)

    def _search_lexical(self, query: str, limit: int) -> list[CodeSearchResult]:
        rows = self._lexical_rows(query, limit=self.search_config.candidate_limit)
        return rank_rows(rows, query, limit)

    def _search_lexical_v2(self, query: str, limit: int) -> list[CodeSearchResult]:
        rows = self._lexical_rows_v2(query, limit=self.search_config.candidate_limit)
        return rank_rows(rows, query, limit)

    def _search_vector(self, query: str, limit: int) -> list[CodeSearchResult]:
        rows = self._vector_rows(query, limit=self.search_config.candidate_limit)
        return vector_results(rows, query, limit)

    def _search_symbol(self, query: str, limit: int) -> list[CodeSearchResult]:
        return symbol_results(
            self._symbol_rows(query, limit=self.search_config.candidate_limit),
            limit,
        )

    def _search_hybrid(self, query: str, limit: int) -> list[CodeSearchResult]:
        try:
            vector_rows = self._vector_rows(query, limit=self.search_config.candidate_limit)
        except EmbeddingError:
            return self._search_lexical(query, limit)
        lexical_rows = self._lexical_rows(query, limit=self.search_config.candidate_limit)
        lexical_backend = lexical_rows[0]["backend"] if lexical_rows else "fts5"
        backend = "hybrid-like-fallback" if lexical_backend == "like-fallback" else "hybrid"
        return reciprocal_rank_fusion(
            lexical_rows,
            vector_rows,
            query,
            limit,
            weights=FusionWeights(
                lexical=self.search_config.lexical_weight,
                vector=self.search_config.vector_weight,
            ),
            backend=backend,
        )

    def _search_hybrid_v2(self, query: str, limit: int) -> list[CodeSearchResult]:
        try:
            vector_rows = self._vector_rows(query, limit=self.search_config.candidate_limit)
        except EmbeddingError:
            vector_rows = []
        lexical_rows = self._lexical_rows_v2(query, limit=self.search_config.candidate_limit)
        symbol_rows = self._symbol_rows(query, limit=self.search_config.candidate_limit)
        lexical_backend = lexical_rows[0]["backend"] if lexical_rows else "fts5"
        backend = "hybrid-v2-like-fallback" if lexical_backend == "like-fallback" else "hybrid-v2"
        return reciprocal_rank_fusion(
            lexical_rows,
            vector_rows,
            query,
            limit,
            weights=FusionWeights(
                lexical=self.search_config.v2_lexical_weight,
                vector=self.search_config.v2_vector_weight,
                symbol=self.search_config.symbol_weight,
            ),
            backend=backend,
            symbol_rows=symbol_rows,
        )

    def _lexical_rows(self, query: str, *, limit: int) -> list[dict[str, object]]:
        tokens = tokenize_query(query)
        if not tokens:
            return []

        rows: list[dict[str, object]]
        if self.store.has_fts5():
            try:
                rows = self.store.search_fts(fts_match_query(tokens), limit=limit)
            except sqlite3.DatabaseError:
                rows = self._fallback_rows(tokens)
        else:
            rows = self._fallback_rows(tokens)
        return rows

    def _lexical_rows_v2(self, query: str, *, limit: int) -> list[dict[str, object]]:
        tokens = tokenize_query(query)
        rows = self._lexical_rows(query, limit=limit)
        if not tokens or len(rows) >= limit or len(tokens) == 1 or not self.store.has_fts5():
            return rows
        try:
            relaxed = self.store.search_fts(fts_any_query(tokens), limit=limit)
        except sqlite3.DatabaseError:
            return rows
        return _dedupe_rows([*rows, *relaxed])[:limit]

    def _vector_rows(self, query: str, *, limit: int) -> list[dict[str, object]]:
        provider = self.embedding_provider
        if provider is None:
            raise EmbeddingError("embedding provider is not configured")
        profile = self._current_profile()
        if profile is None:
            raise EmbeddingError("embedding profile is not available")
        query_vector = normalize_vector(provider.embed([query])[0])
        return self.store.search_vectors(query_vector, profile=profile, limit=limit)

    def _symbol_rows(self, query: str, *, limit: int) -> list[dict[str, object]]:
        if self._symbol_candidate_data is None:
            self._symbol_candidate_data = (
                self.store.list_symbol_definitions(),
                self.store.list_chunks(),
            )
        definitions, chunks = self._symbol_candidate_data
        candidates = rank_symbol_candidates(
            query,
            definitions,
            chunks,
            limit=limit,
        )
        return [
            {
                "chunk_id": candidate.chunk.id,
                "file_path": candidate.chunk.file_path,
                "start_line": candidate.chunk.start_line,
                "end_line": candidate.chunk.end_line,
                "content": candidate.chunk.content,
                "content_hash": candidate.chunk.content_hash,
                "chunk_type": candidate.chunk.chunk_type,
                "symbol_name": candidate.definition.name,
                "qualified_name": candidate.definition.qualified_name,
                "parent_symbol": candidate.chunk.parent_symbol,
                "is_fallback": candidate.chunk.is_fallback,
                "symbol_score": candidate.score,
                "symbol_matched_fields": candidate.matched_fields,
                "backend": "symbol",
            }
            for candidate in candidates
        ]

    def _iter_files(self, base: Path):
        for path in base.rglob("*"):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.is_file() and is_indexable(path):
                yield path

    def _resolve(self, value: str | Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = self.root / path
        resolved = path.resolve()
        resolved.relative_to(self.root)
        return resolved

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _fallback_rows(self, tokens: list[str]) -> list[dict[str, object]]:
        rows = self.store.search_like(tokens, limit=500)
        filtered: list[dict[str, object]] = []
        for row in rows:
            searchable_tokens = tokenize_code_text(
                " ".join(
                    [
                        str(row["content"]),
                        str(row["symbol_name"] or ""),
                        str(row["qualified_name"] or ""),
                        str(row["file_path"]),
                        str(row["chunk_type"]),
                    ]
                )
            )
            searchable = set(searchable_tokens)
            if all(token in searchable for token in tokens):
                filtered.append(row)
        return filtered

    def _resolve_workspace_symbols(self, stats: IndexStats) -> None:
        started = time.perf_counter()
        definitions = self.store.list_symbol_definitions()
        imports = self.store.list_import_bindings()
        references = self.store.list_symbol_references()
        file_paths = set(self.store.list_file_paths())
        resolved_imports = resolve_import_paths(imports, file_paths)
        resolved_references = resolve_references(definitions, resolved_imports, references)
        graph = build_call_edges(definitions, resolved_references)
        recursive_components = find_recursive_components(graph.edges)
        self.store.replace_workspace_graph(resolved_imports, resolved_references, graph.edges)
        stats.references_resolved = sum(
            1 for reference in resolved_references if reference.resolution_status == "resolved"
        )
        stats.references_unresolved = sum(
            1 for reference in resolved_references if reference.resolution_status == "unresolved"
        )
        stats.references_ambiguous = sum(
            1 for reference in resolved_references if reference.resolution_status == "ambiguous"
        )
        stats.call_edges_built = len(graph.edges)
        stats.call_edges_skipped_unresolved = graph.skipped_unresolved
        stats.call_edges_skipped_low_confidence = graph.skipped_low_confidence
        stats.call_edges_skipped_unsupported_kind = graph.skipped_unsupported_kind
        stats.call_edges_skipped_missing_caller = graph.skipped_missing_caller
        stats.call_edges_skipped_missing_callee = graph.skipped_missing_callee
        stats.recursive_components = len(recursive_components)
        stats.resolution_duration_ms = (time.perf_counter() - started) * 1000
        stats.call_graph_duration_ms = stats.resolution_duration_ms

    def _lookup_symbol(self, symbol_id_or_name: str) -> SymbolDefinition:
        return resolve_symbol_query(
            symbol_id_or_name,
            self.store.list_symbol_definitions(),
        )

    def _profile_for_provider(
        self,
        provider: EmbeddingProvider,
        sample_chunk,
    ) -> EmbeddingProfile:
        if self._embedding_profile is not None:
            return self._embedding_profile
        sample_text = build_embedding_text(
            sample_chunk,
            max_chars=self.search_config.max_input_chars,
        )
        dimensions = infer_dimensions(provider, sample_text)
        self._embedding_profile = build_embedding_profile(provider, dimensions=dimensions)
        return self._embedding_profile

    def _current_profile(self) -> EmbeddingProfile | None:
        if not self.embedding_provider:
            return None
        if self._embedding_profile is not None:
            return self._embedding_profile
        chunks = [chunk for chunk in self.store.list_chunks() if is_embedding_eligible(chunk)]
        if not chunks:
            return None
        return self._profile_for_provider(self.embedding_provider, chunks[0])

    def _resolve_search_mode(self, mode: str) -> str:
        configured = mode if mode != "auto" else self.search_config.search_mode
        if configured not in {
            "auto",
            "lexical",
            "lexical_v2",
            "vector",
            "hybrid",
            "symbol",
            "hybrid_v2",
        }:
            return "auto"
        return configured

    def _merge_embedding_stats(self, target: IndexStats, source: IndexStats) -> None:
        target.embedded_chunks += source.embedded_chunks
        target.unchanged_embeddings += source.unchanged_embeddings
        target.failed_embeddings += source.failed_embeddings
        target.embedding_profile = source.embedding_profile


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _dedupe_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[str] = set()
    result: list[dict[str, object]] = []
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id and chunk_id not in seen:
            seen.add(chunk_id)
            result.append(row)
    return result
