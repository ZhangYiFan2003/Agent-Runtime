from __future__ import annotations

import asyncio
import json
import math
import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from axiom.config import AxiomConfig, EmbeddingConfig, config_to_public_dict, load_config
from axiom.rag import CodeIndex
from axiom.rag.code_index_factory import create_code_index
from axiom.rag.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingError,
    OpenAICompatibleEmbeddingProvider,
    build_embedding_profile,
    build_embedding_text,
    embedding_input_hash,
    is_embedding_eligible,
)
from axiom.rag.hybrid import FusionWeights, reciprocal_rank_fusion
from axiom.rag.models import CodeChunk
from axiom.rag.store import SCHEMA_VERSION, CodeIndexStore
from axiom.rag.vectors import (
    VectorError,
    cosine_similarity,
    decode_vector,
    encode_vector,
)
from axiom.tools.base import ToolContext
from axiom.tools.builtins import search_code

FIXTURE = Path(__file__).parent / "fixtures" / "code_index_project"
VECTOR_QUERIES = Path(__file__).parent / "fixtures" / "code_search_vector_queries.json"


class DeterministicEmbeddingProvider:
    provider_name = "deterministic-test"
    model_name = "concept-v1"

    def __init__(self, dimensions: int = 12, *, fail: bool = False):
        self._dimensions = dimensions
        self.fail = fail
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int | None:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingError("deterministic provider failed")
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        lowered = text.casefold()
        vector = [0.0] * self._dimensions
        concepts = [
            ("auth", ["authenticate", "authenticate_user", "login", "sign-in", "validator"]),
            ("config", ["load_user_config", "configuration", "config", "settings"]),
            ("fetch_user", ["fetchuser", "fetch user", "download", "account details"]),
            ("profile", ["getuserprofile", "retrieve customer profile", "customer profile"]),
            ("worker", ["worker", "background", "job"]),
            ("client", ["buildclient", "build client", "http client object"]),
            ("role", ["role", "permission", "user role", "鐢ㄦ埛"]),
            ("greet", ["greet", "salutation", "hello", "service"]),
            ("normalize", ["normalizeuser", "normalize account name"]),
            ("load_user", ["load_user", "load user"]),
            ("api_client", ["apiclient", "api client"]),
            ("http_client", ["httpclient", "http client"]),
        ]
        concept_limit = max(0, self._dimensions - 1)
        for index, (_name, terms) in enumerate(concepts[:concept_limit]):
            if any(term in lowered for term in terms):
                vector[index] += 1.0
        if not any(vector):
            vector[-1] = 1.0
        return vector


def copy_fixture(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    return project


def rows(db_path: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        return conn.execute(query, params).fetchall()


def test_vector_encoding_and_cosine_validation() -> None:
    encoded = encode_vector([3.0, 4.0])
    decoded = decode_vector(encoded, dimensions=2)
    assert decoded == pytest.approx([0.6, 0.8], abs=1e-6)
    assert cosine_similarity(decoded, decoded) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    with pytest.raises(VectorError):
        encode_vector([0.0, 0.0])
    with pytest.raises(VectorError):
        encode_vector([math.nan, 1.0])
    with pytest.raises(VectorError):
        encode_vector([1.0, 2.0], dimensions=3)
    with pytest.raises(VectorError):
        decode_vector(b"\x00\x00", dimensions=2)
    with pytest.raises(VectorError):
        cosine_similarity([1.0], [1.0, 0.0])


def test_openai_compatible_embedding_provider_validates_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer " + "test-key"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "embed-model"
        assert payload["dimensions"] == 3
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ]
            },
        )

    provider = OpenAICompatibleEmbeddingProvider(
        provider_name="openai-compatible",
        model="embed-model",
        api_key="test-key",
        base_url="https://example.test/v1",
        dimensions=3,
        transport=httpx.MockTransport(handler),
    )

    assert provider.embed(["a", "b"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    missing_key = OpenAICompatibleEmbeddingProvider(
        provider_name="openai-compatible",
        model="embed-model",
        api_key="",
        base_url="https://example.test/v1",
    )
    with pytest.raises(EmbeddingConfigurationError):
        missing_key.embed(["a"])


def test_openai_provider_rejects_bad_response_and_http_error() -> None:
    bad_count = OpenAICompatibleEmbeddingProvider(
        provider_name="openai-compatible",
        model="embed-model",
        api_key="key",
        base_url="https://example.test/v1",
        dimensions=2,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"data": []})),
    )
    with pytest.raises(EmbeddingError):
        bad_count.embed(["a"])

    http_error = OpenAICompatibleEmbeddingProvider(
        provider_name="openai-compatible",
        model="embed-model",
        api_key="key",
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={})),
    )
    with pytest.raises(EmbeddingError):
        http_error.embed(["a"])

    timeout = OpenAICompatibleEmbeddingProvider(
        provider_name="openai-compatible",
        model="embed-model",
        api_key="key",
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(
            lambda _request: (_ for _ in ()).throw(httpx.TimeoutException("timeout"))
        ),
    )
    with pytest.raises(EmbeddingError):
        timeout.embed(["a"])

    bad_dimension = OpenAICompatibleEmbeddingProvider(
        provider_name="openai-compatible",
        model="embed-model",
        api_key="key",
        base_url="https://example.test/v1",
        dimensions=3,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
            )
        ),
    )
    with pytest.raises(EmbeddingError):
        bad_dimension.embed(["a"])


def test_schema_version_4_migrates_v3_additively(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    CodeIndex(project, db_path=db_path).update()
    with sqlite3.connect(db_path) as conn:
        conn.execute("update schema_metadata set value = '3' where key = 'schema_version'")
        lexical_count = conn.execute("select count(*) from code_chunks_fts").fetchone()[0]

    store = CodeIndexStore(db_path)

    assert SCHEMA_VERSION == "6"
    assert store.schema_version() == "6"
    assert rows(db_path, "select name from sqlite_master where name = 'embedding_profiles'")
    assert rows(db_path, "select name from sqlite_master where name = 'chunk_embeddings'")
    fts_count = rows(db_path, "select count(*) as count from code_chunks_fts")[0]["count"]
    assert fts_count == lexical_count
    assert CodeIndexStore(db_path).schema_version() == "6"


def test_embedding_profile_and_input_text_are_stable() -> None:
    chunk = CodeChunk(
        id="chunk",
        file_path="app.py",
        language="python",
        chunk_type="function",
        symbol_name="load_user_config",
        qualified_name="load_user_config",
        parent_symbol=None,
        start_line=1,
        end_line=2,
        content="def load_user_config(): pass",
        content_hash="hash",
    )
    text = build_embedding_text(chunk, max_chars=10_000)
    assert text == build_embedding_text(chunk, max_chars=10_000)
    assert "created_at" not in text
    assert embedding_input_hash(text) == embedding_input_hash(text)
    assert is_embedding_eligible(chunk)

    provider = DeterministicEmbeddingProvider(dimensions=8)
    profile = build_embedding_profile(provider, dimensions=8)
    assert profile.id == build_embedding_profile(provider, dimensions=8).id
    assert "key" not in profile.id


def test_incremental_embedding_backfill_modify_delete_and_profile_change(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    provider = DeterministicEmbeddingProvider()
    config = EmbeddingConfig(enabled=True, search_mode="hybrid")
    index = CodeIndex(project, db_path=db_path, embedding_provider=provider, search_config=config)

    first = index.update()
    assert first.embedded_chunks > 0
    assert first.embedding_profile
    first_count = index.store.count_embeddings(first.embedding_profile)

    second = index.update()
    assert second.embedded_chunks == 0
    assert second.unchanged_embeddings == first_count

    app = project / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\ndef new_vector_target():\n    return 1\n")
    modified = index.update()
    assert modified.embedded_chunks >= 1

    (project / "client.ts").unlink()
    deleted = index.update()
    assert deleted.deleted_files == 1
    assert not rows(
        db_path,
        """
        select e.chunk_id
        from chunk_embeddings e
        join code_chunks c on c.id = e.chunk_id
        where c.file_path = 'client.ts'
        """,
    )

    other_provider = DeterministicEmbeddingProvider(dimensions=6)
    other_index = CodeIndex(
        project,
        db_path=db_path,
        embedding_provider=other_provider,
        search_config=config,
    )
    changed_profile = other_index.sync_embeddings()
    assert changed_profile.embedded_chunks > 0
    assert changed_profile.embedding_profile != first.embedding_profile


def test_embedding_failure_keeps_lexical_search_and_removes_stale_vectors(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    config = EmbeddingConfig(enabled=True, search_mode="auto")
    index = CodeIndex(
        project,
        db_path=db_path,
        embedding_provider=DeterministicEmbeddingProvider(),
        search_config=config,
    )
    stats = index.update()
    assert stats.embedded_chunks > 0

    failing = CodeIndex(
        project,
        db_path=db_path,
        embedding_provider=DeterministicEmbeddingProvider(fail=True),
        search_config=config,
    )
    app = project / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\ndef failure_target():\n    return 1\n")
    failed = failing.update()
    assert failed.failed_embeddings == 1
    assert failing.search("load user config", limit=3)
    assert not rows(
        db_path,
        """
        select e.chunk_id
        from chunk_embeddings e
        join code_chunks c on c.id = e.chunk_id
        where c.file_path = 'app.py'
        """,
    )

    recovered = CodeIndex(
        project,
        db_path=db_path,
        embedding_provider=DeterministicEmbeddingProvider(),
        search_config=config,
    ).sync_embeddings()
    assert recovered.embedded_chunks > 0


def test_vector_only_and_hybrid_search_modes(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    provider = DeterministicEmbeddingProvider()
    config = EmbeddingConfig(enabled=True, search_mode="auto", candidate_limit=100)
    index = CodeIndex(
        project,
        db_path=tmp_path / "index.sqlite3",
        embedding_provider=provider,
        search_config=config,
    )
    index.update()

    vector = index.search("find the sign-in validator", limit=5, mode="vector")
    assert vector[0].symbol_name == "authenticate_user"
    assert vector[0].backend == "vector"

    hybrid = index.search("load_user_config", limit=5, mode="hybrid")
    assert hybrid[0].symbol_name == "load_user_config"
    assert hybrid[0].backend == "hybrid"
    assert hybrid[0].fusion_score is not None
    assert hybrid[0].lexical_rank is not None

    auto = index.search("retrieve customer profile", limit=5)
    assert auto[0].backend == "hybrid"

    lexical = index.search("load_user_config", limit=5, mode="lexical")
    assert lexical[0].backend in {"fts5", "like-fallback"}


def test_vector_mode_requires_provider_but_auto_falls_back(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    config = EmbeddingConfig(enabled=True, search_mode="auto")
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3", search_config=config)
    index.update()

    assert index.search("load user config", limit=3)
    with pytest.raises(EmbeddingError):
        index.search("load user config", limit=3, mode="vector")


def test_embedding_disabled_does_not_create_remote_provider(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    config = AxiomConfig(
        embedding=EmbeddingConfig(
            enabled=False,
            model="embed-model",
            api_key="secret-key",
            base_url="https://example.test/v1",
        )
    )
    index = create_code_index(project, config)

    assert index.embedding_provider is None
    index.update()
    assert index.search("load user config", limit=3)


def test_rrf_uses_ranks_not_raw_score_addition() -> None:
    lexical_rows = [
        _row("a", "A", bm25_score=1000.0),
        _row("b", "B", bm25_score=0.1),
    ]
    vector_rows = [
        _row("b", "B", vector_score=0.9),
        _row("a", "A", vector_score=0.1),
    ]

    results = reciprocal_rank_fusion(
        lexical_rows,
        vector_rows,
        "query",
        2,
        weights=FusionWeights(lexical=0.5, vector=0.5, rrf_k=60),
    )

    assert {result.symbol_name for result in results} == {"A", "B"}
    assert all(result.fusion_score is not None and result.fusion_score < 1 for result in results)


def test_three_source_rrf_fuses_symbol_candidates_without_duplicates() -> None:
    lexical_rows = [_row("a", "Alpha", bm25_score=1.0)]
    vector_rows = [_row("b", "Beta", vector_score=0.9)]
    symbol_rows = [
        {**_row("a", "Alpha"), "symbol_score": 120.0},
        {**_row("c", "Gamma"), "symbol_score": 110.0},
    ]

    results = reciprocal_rank_fusion(
        lexical_rows,
        vector_rows,
        "Alpha",
        5,
        weights=FusionWeights(lexical=0.3, vector=0.2, symbol=0.5),
        symbol_rows=symbol_rows,
    )

    assert [result.symbol_name for result in results].count("Alpha") == 1
    assert {result.symbol_name for result in results} == {"Alpha", "Beta", "Gamma"}
    assert next(result for result in results if result.symbol_name == "Alpha").symbol_rank == 1


def test_symbol_source_supports_exact_and_normalized_identifiers(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "symbols.sqlite3")
    index.update()

    exact = index.search("HttpClient", limit=3, mode="symbol")
    snake = index.search("http_client", limit=3, mode="symbol")
    camel = index.search("httpClient", limit=3, mode="symbol")

    assert exact[0].symbol_name == "HttpClient"
    assert snake[0].symbol_name == "HttpClient"
    assert camel[0].symbol_name == "HttpClient"
    assert "exact_symbol" in exact[0].matched_fields
    fused = index.search("http_client", limit=3, mode="hybrid_v2")
    assert fused[0].symbol_name == "HttpClient"
    assert fused[0].backend == "hybrid-v2"


def test_lexical_v2_keeps_exact_symbol_ahead_of_content_only_matches(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "field-priority.sqlite3")
    index.update()

    results = index.search("load_user_config", limit=5, mode="lexical_v2")

    assert results[0].symbol_name == "load_user_config"
    assert "exact_symbol" in results[0].matched_fields


def test_config_env_and_public_masking() -> None:
    config = load_config(
        env={
            "AXIOM_EMBEDDING_ENABLED": "true",
            "AXIOM_EMBEDDING_PROVIDER": "openai-compatible",
            "AXIOM_EMBEDDING_MODEL": "text-embedding-test",
            "AXIOM_EMBEDDING_API_KEY": "secret-key",
            "AXIOM_EMBEDDING_BASE_URL": "https://example.test/v1",
            "AXIOM_EMBEDDING_DIMENSIONS": "8",
            "AXIOM_CODE_SEARCH_MODE": "hybrid",
        }
    )

    assert config.embedding.enabled is True
    assert config.embedding.dimensions == 8
    assert config.embedding.search_mode == "hybrid"
    public = config_to_public_dict(config)
    assert public["embedding"]["api_key"] == "***"


def test_search_code_tool_uses_configured_hybrid_index(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    provider = DeterministicEmbeddingProvider()
    config = EmbeddingConfig(enabled=True, search_mode="hybrid")
    index = CodeIndex(
        project,
        embedding_provider=provider,
        search_config=config,
    )
    index.update()

    context = ToolContext(
        cwd=str(project),
        config=AxiomConfig(embedding=config),
    )
    result = asyncio.run(search_code({"query": "load_user_config", "limit": 3}, context))

    assert result.is_error is False
    assert "app.py:" in result.content
    assert result.display_summary


def test_offline_vector_fixture_metrics(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    provider = DeterministicEmbeddingProvider()
    config = EmbeddingConfig(enabled=True, search_mode="hybrid", candidate_limit=100)
    index = CodeIndex(
        project,
        db_path=tmp_path / "index.sqlite3",
        embedding_provider=provider,
        search_config=config,
    )
    index.update()
    cases = json.loads(VECTOR_QUERIES.read_text(encoding="utf-8"))

    metrics = {}
    for mode in ["lexical", "vector", "hybrid"]:
        top1 = 0
        recall5 = 0
        reciprocal_ranks: list[float] = []
        for case in cases:
            results = index.search(case["query"], limit=5, mode=mode)
            expected = (case["expected_path"], case["expected_symbol"])
            ranked = [(item.path, item.symbol_name) for item in results]
            if ranked and ranked[0] == expected:
                top1 += 1
            if expected in ranked:
                recall5 += 1
                reciprocal_ranks.append(1 / (ranked.index(expected) + 1))
            else:
                reciprocal_ranks.append(0.0)
        metrics[mode] = (
            top1 / len(cases),
            recall5 / len(cases),
            sum(reciprocal_ranks) / len(reciprocal_ranks),
        )

    assert metrics["vector"][1] >= 0.80
    assert metrics["hybrid"][1] >= metrics["lexical"][1]
    assert metrics["hybrid"][2] >= 0.70


def _row(
    path: str,
    symbol: str,
    *,
    bm25_score: float = 0.0,
    vector_score: float = 0.0,
) -> dict[str, object]:
    return {
        "chunk_id": symbol,
        "file_path": f"{path}.py",
        "start_line": 1,
        "content": f"def {symbol}(): pass",
        "content_hash": symbol,
        "chunk_type": "function",
        "symbol_name": symbol,
        "qualified_name": symbol,
        "parent_symbol": None,
        "is_fallback": 0,
        "bm25_score": bm25_score,
        "vector_score": vector_score,
        "backend": "test",
    }
