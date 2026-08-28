from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from axiom.rag import CodeIndex
from axiom.rag.store import SCHEMA_VERSION, CodeIndexStore, supports_fts5
from axiom.rag.tokenizer import (
    compact_identifier,
    fts_match_query,
    identifier_forms,
    split_identifier,
    tokenize_code_text,
    tokenize_query,
)

FIXTURE = Path(__file__).parent / "fixtures" / "code_index_project"
QUERIES = Path(__file__).parent / "fixtures" / "code_search_queries.json"


def copy_fixture(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    return project


def rows(db_path: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        return conn.execute(query, params).fetchall()


def test_tokenizer_splits_identifiers_and_chinese() -> None:
    assert split_identifier("getUserProfile") == [
        "getuserprofile",
        "get",
        "user",
        "profile",
    ]
    assert split_identifier("load_user_config") == [
        "load_user_config",
        "load",
        "user",
        "config",
    ]
    assert split_identifier("OAuth2Client") == [
        "oauth2client",
        "oauth",
        "2",
        "client",
    ]
    assert split_identifier("HTTPClient") == ["httpclient", "http", "client"]

    chinese = tokenize_code_text("用户权限校验")
    assert {"用户", "权限", "校验"}.issubset(chinese)

    mixed = tokenize_code_text("load用户Config")
    assert "load用户config" in mixed
    assert "config" in mixed

    deduped = tokenize_query("api api db id")
    assert deduped == ["api", "db", "id"]


def test_identifier_normalization_handles_camel_snake_qualified_and_paths() -> None:
    expected = "sqlitecheckpointstore"
    assert compact_identifier("SQLiteCheckpointStore") == expected
    assert compact_identifier("sqlite_checkpoint_store") == expected
    assert compact_identifier("sqlite/checkpoint-store") == expected
    assert "runtimeapiserver" in identifier_forms("runtime.api.RuntimeApiServer")


def test_fts5_capability_detection() -> None:
    conn = sqlite3.connect(":memory:")
    assert supports_fts5(conn) is True


def test_schema_version_5_and_fts_table_are_created(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"

    CodeIndex(project, db_path=db_path).update()

    assert CodeIndexStore(db_path).schema_version() == SCHEMA_VERSION
    assert SCHEMA_VERSION == "6"
    assert rows(
        db_path,
        "select name from sqlite_master where type = 'table' and name = 'code_chunks_fts'",
    )
    chunk_count = rows(db_path, "select count(*) as count from code_chunks")[0]["count"]
    fts_count = rows(db_path, "select count(*) as count from code_chunks_fts")[0]["count"]
    assert fts_count == chunk_count


def test_fts_rows_sync_for_new_modified_and_deleted_files(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    index = CodeIndex(project, db_path=db_path)
    index.update()

    (project / "extra.js").write_text(
        "export function extraSearch() { return 1; }\n",
        encoding="utf-8",
    )
    added = index.update()
    assert added.indexed_files == 1
    assert rows(db_path, "select chunk_id from code_chunks_fts where file_path = 'extra.js'")

    app = project / "app.py"
    updated_app = app.read_text(encoding="utf-8") + "\ndef sync_target():\n    return 1\n"
    app.write_text(updated_app, encoding="utf-8")
    modified = index.update()
    assert modified.indexed_files == 1
    assert rows(db_path, "select chunk_id from code_chunks_fts where lexical_text match 'sync'")

    (project / "extra.js").unlink()
    deleted = index.update()
    assert deleted.deleted_files == 1
    assert not rows(db_path, "select chunk_id from code_chunks_fts where file_path = 'extra.js'")
    orphan_count = rows(
        db_path,
        """
        select count(*) as count
        from code_chunks_fts f
        left join code_chunks c on c.id = f.chunk_id
        where c.id is null
        """,
    )[0]["count"]
    assert orphan_count == 0


def test_schema_v2_migrates_in_place_without_reparsing(tmp_path: Path) -> None:
    db_path = tmp_path / "v2.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("create table schema_metadata (key text primary key, value text not null)")
        conn.execute("insert into schema_metadata(key, value) values ('schema_version', '2')")
        conn.execute(
            """
            create table indexed_files (
                path text primary key,
                language text not null,
                sha256 text not null,
                size integer not null,
                mtime_ns integer not null,
                indexed_at text not null,
                parse_status text not null
            )
            """
        )
        conn.execute(
            """
            create table code_chunks (
                id text primary key,
                file_path text not null references indexed_files(path) on delete cascade,
                language text not null,
                chunk_type text not null,
                symbol_name text,
                qualified_name text,
                parent_symbol text,
                start_line integer not null,
                end_line integer not null,
                content text not null,
                content_hash text not null,
                is_fallback integer not null default 0,
                parse_status text not null
            )
            """
        )
        conn.execute(
            """
            insert into indexed_files(
                path, language, sha256, size, mtime_ns, indexed_at, parse_status
            )
            values ('app.py', 'python', 'hash', 1, 1, 'now', 'parsed')
            """
        )
        conn.execute(
            """
            insert into code_chunks(
                id, file_path, language, chunk_type, symbol_name, qualified_name,
                parent_symbol, start_line, end_line, content, content_hash,
                is_fallback, parse_status
            )
            values (
                'chunk-1', 'app.py', 'python', 'function', 'load_user_config',
                'load_user_config', null, 1, 2, 'def load_user_config(): pass',
                'content-hash', 0, 'parsed'
            )
            """
        )

    store = CodeIndexStore(db_path)
    store_again = CodeIndexStore(db_path)

    assert store.schema_version() == "6"
    assert store_again.schema_version() == "6"
    assert rows(db_path, "select id from code_chunks where id = 'chunk-1'")
    assert rows(db_path, "select chunk_id from code_chunks_fts where chunk_id = 'chunk-1'")


def test_pre_v2_legacy_schema_rebuilds_to_v3(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table code_chunks (
                id integer primary key autoincrement,
                root text not null,
                path text not null,
                line integer not null,
                content text not null
            )
            """
        )

    index = CodeIndex(project, db_path=db_path)

    assert index.store.schema_version() == "6"
    columns = {row["name"] for row in rows(db_path, "pragma table_info(code_chunks)")}
    assert "file_path" in columns
    assert "root" not in columns


def test_search_ranking_identifier_splitting_chinese_and_dedup(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    index.update()

    exact = index.search("load_user_config", limit=5)
    assert exact[0].symbol_name == "load_user_config"
    assert exact[0].backend == "fts5"
    assert "exact_symbol" in exact[0].matched_fields

    camel = index.search("get user profile", limit=5)
    assert camel[0].symbol_name == "getUserProfile"

    snake = index.search("load user config", limit=5)
    assert snake[0].symbol_name == "load_user_config"

    chinese = index.search("用户 权限", limit=5)
    assert chinese[0].symbol_name == "用户权限校验"

    qualified = index.search("HttpClient fetchUser", limit=5)
    assert qualified[0].qualified_name == "HttpClient.fetchUser"
    assert "qualified_name" in qualified[0].matched_fields

    notes = index.search("project notes", limit=5)
    assert notes[0].path == "notes.txt"
    assert notes[0].chunk_type == "file"
    assert "fallback" in notes[0].matched_fields

    limited = index.search("user", limit=2)
    assert len(limited) == 2
    assert len({(item.path, item.qualified_name, item.snippet) for item in limited}) == 2

    (project / "other.py").write_text(
        "def greet():\n    return 'other'\n",
        encoding="utf-8",
    )
    index.update()
    greet_hits = {
        (Path(result.path).name, result.symbol_name) for result in index.search("greet", limit=10)
    }
    assert ("app.py", "greet") in greet_hits
    assert ("other.py", "greet") in greet_hits


def test_search_escapes_special_fts_characters_and_empty_query(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    index.update()

    assert index.search("") == []
    assert index.search("   ") == []
    for unsafe_query in ['"', "'", "OR", "NOT", "NEAR", "*", "(", ")", ":", "-", "“用户”"]:
        assert isinstance(index.search(unsafe_query, limit=5), list)
    assert isinstance(index.search('load OR NOT "user" (config)*', limit=5), list)
    assert index.search('load "user" (config)*', limit=5)
    assert fts_match_query(["load", 'OR"', "config*"]) == '"load" AND "OR""" AND "config*"'


def test_like_fallback_when_fts5_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import axiom.rag.store as store_module

    monkeypatch.setattr(store_module, "supports_fts5", lambda _conn: False)
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    index.update()

    assert index.store.schema_version() == "6"
    results = index.search("load user config", limit=5)
    assert results
    assert results[0].symbol_name == "load_user_config"
    assert results[0].backend == "like-fallback"


def test_offline_relevance_fixture_metrics(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    index.update()
    cases = json.loads(QUERIES.read_text(encoding="utf-8"))

    top1 = 0
    recall5 = 0
    reciprocal_ranks: list[float] = []
    for case in cases:
        results = index.search(case["query"], limit=5)
        expected = (case["expected_path"], case["expected_symbol"])
        ranked = [(item.path, item.symbol_name) for item in results]
        if ranked and ranked[0] == expected:
            top1 += 1
        if expected in ranked:
            recall5 += 1
            reciprocal_ranks.append(1 / (ranked.index(expected) + 1))
        else:
            reciprocal_ranks.append(0.0)

    assert len(cases) >= 15
    assert top1 / len(cases) >= 0.80
    assert recall5 / len(cases) == 1.0
    assert sum(reciprocal_ranks) / len(reciprocal_ranks) >= 0.90
