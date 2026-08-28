from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IndexedFile:
    path: str
    language: str
    sha256: str
    size: int
    mtime_ns: int
    indexed_at: str
    parse_status: str


@dataclass(slots=True)
class CodeChunk:
    id: str
    file_path: str
    language: str
    chunk_type: str
    symbol_name: str | None
    qualified_name: str | None
    parent_symbol: str | None
    start_line: int
    end_line: int
    content: str
    content_hash: str
    is_fallback: bool = False
    parse_status: str = "parsed"


@dataclass(slots=True)
class SymbolDefinition:
    id: str
    file_path: str
    language: str
    symbol_kind: str
    name: str
    qualified_name: str
    container_symbol_id: str | None
    container_qualified_name: str | None
    signature: str | None
    start_line: int
    end_line: int
    chunk_id: str | None
    exported: bool
    visibility: str | None
    definition_hash: str


@dataclass(slots=True)
class ImportBinding:
    id: str
    file_path: str
    language: str
    module_name: str
    imported_name: str | None
    local_name: str | None
    import_kind: str
    relative_level: int
    start_line: int
    end_line: int
    resolved_file_path: str | None
    resolution_status: str


@dataclass(slots=True)
class SymbolReference:
    id: str
    file_path: str
    language: str
    reference_kind: str
    name: str
    qualifier: str | None
    enclosing_symbol_id: str | None
    enclosing_qualified_name: str | None
    argument_count: int | None
    start_line: int
    end_line: int
    resolved_symbol_id: str | None
    resolution_status: str
    resolution_confidence: float
    resolution_reason: str | None


@dataclass(slots=True)
class CallEdge:
    id: str
    reference_id: str
    caller_symbol_id: str
    callee_symbol_id: str
    file_path: str
    language: str
    edge_kind: str
    start_line: int
    end_line: int
    resolution_confidence: float
    resolution_reason: str | None


@dataclass(slots=True)
class CallPath:
    symbol_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    cycle: bool = False


@dataclass(slots=True)
class CallPathResult:
    root_symbol_id: str
    direction: str
    max_depth: int
    paths: list[CallPath]
    truncated: bool


@dataclass(slots=True)
class RecursiveComponent:
    symbol_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]
    recursion_kind: str


@dataclass(slots=True)
class CodeSearchResult:
    path: str
    line: int
    snippet: str
    chunk_id: str | None = None
    end_line: int | None = None
    content: str | None = None
    chunk_type: str | None = None
    symbol_name: str | None = None
    qualified_name: str | None = None
    score: float | None = None
    backend: str | None = None
    matched_fields: tuple[str, ...] = ()
    lexical_score: float | None = None
    vector_score: float | None = None
    symbol_score: float | None = None
    fusion_score: float | None = None
    lexical_rank: int | None = None
    vector_rank: int | None = None
    symbol_rank: int | None = None
    embedding_profile: str | None = None


@dataclass(slots=True)
class CodeContextItem:
    chunk_id: str
    symbol_id: str | None
    file_path: str
    start_line: int
    end_line: int
    content: str
    reason: str
    seed_rank: int | None
    graph_distance: int
    estimated_tokens: int


@dataclass(slots=True)
class CodeContextResult:
    query: str
    items: list[CodeContextItem]
    estimated_chars: int
    estimated_tokens: int
    truncated: bool
    seed_count: int
    expanded_symbol_count: int
    max_context_chars: int
    max_estimated_tokens: int


@dataclass(slots=True)
class IndexStats:
    scanned_files: int = 0
    indexed_files: int = 0
    unchanged_files: int = 0
    deleted_files: int = 0
    failed_files: int = 0
    chunk_count: int = 0
    duration_ms: float = 0.0
    embedded_chunks: int = 0
    unchanged_embeddings: int = 0
    failed_embeddings: int = 0
    embedding_profile: str | None = None
    parsed_files: int = 0
    definitions_updated: int = 0
    imports_updated: int = 0
    references_extracted: int = 0
    references_resolved: int = 0
    references_unresolved: int = 0
    references_ambiguous: int = 0
    resolution_duration_ms: float = 0.0
    call_edges_built: int = 0
    call_edges_skipped_unresolved: int = 0
    call_edges_skipped_low_confidence: int = 0
    call_edges_skipped_unsupported_kind: int = 0
    call_edges_skipped_missing_caller: int = 0
    call_edges_skipped_missing_callee: int = 0
    recursive_components: int = 0
    call_graph_duration_ms: float = 0.0
