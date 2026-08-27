from __future__ import annotations

import asyncio
import shutil
import sqlite3
from pathlib import Path

from axiom.config import AxiomConfig
from axiom.rag import CodeIndex
from axiom.rag.store import SCHEMA_VERSION, CodeIndexStore
from axiom.rag.symbols.extractor import stable_reference_id, stable_symbol_id
from axiom.tools.base import ToolContext
from axiom.tools.builtins import find_references, find_symbol, search_code

FIXTURE = Path(__file__).parent / "fixtures" / "symbol_index_project"


def copy_fixture(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    shutil.copytree(FIXTURE, project)
    return project


def rows(db_path: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with conn:
        return conn.execute(query, params).fetchall()


def test_symbol_ids_are_stable_and_not_chunk_ids() -> None:
    first = stable_symbol_id(
        file_path="python_pkg/utils.py",
        language="python",
        symbol_kind="function",
        qualified_name="load_config",
        signature="def load_config(path: str) -> dict[str, str]:",
    )
    body_changed = stable_symbol_id(
        file_path="python_pkg/utils.py",
        language="python",
        symbol_kind="function",
        qualified_name="load_config",
        signature="def load_config(path: str) -> dict[str, str]:",
    )
    reformatted = stable_symbol_id(
        file_path="python_pkg/utils.py",
        language="python",
        symbol_kind="function",
        qualified_name="load_config",
        signature="def   load_config(path: str)   ->   dict[str, str]:",
    )
    renamed = stable_symbol_id(
        file_path="python_pkg/utils.py",
        language="python",
        symbol_kind="function",
        qualified_name="load_settings",
        signature="def load_settings(path: str) -> dict[str, str]:",
    )
    parameter_renamed = stable_symbol_id(
        file_path="python_pkg/utils.py",
        language="python",
        symbol_kind="function",
        qualified_name="load_config",
        signature="def load_config(config_path: str) -> dict[str, str]:",
    )
    parameter_type_changed = stable_symbol_id(
        file_path="python_pkg/utils.py",
        language="python",
        symbol_kind="function",
        qualified_name="load_config",
        signature="def load_config(path: Path) -> dict[str, str]:",
    )
    overload = stable_symbol_id(
        file_path="java_pkg/Helper.java",
        language="java",
        symbol_kind="method",
        qualified_name="Helper.validate",
        signature="public boolean validate(String value, int level) {",
    )
    overload_same_file = stable_symbol_id(
        file_path="java_pkg/Helper.java",
        language="java",
        symbol_kind="method",
        qualified_name="Helper.validate",
        signature="public boolean validate(String value) {",
    )
    same_name_other_file = stable_symbol_id(
        file_path="python_pkg/other.py",
        language="python",
        symbol_kind="function",
        qualified_name="load_config",
        signature="def load_config(path: str) -> dict[str, str]:",
    )

    assert first == body_changed
    assert first == reformatted
    assert first != renamed
    assert first != parameter_renamed
    assert first != parameter_type_changed
    assert first != overload
    assert overload != overload_same_file
    assert first != same_name_other_file

    ref_a = stable_reference_id(
        file_path="a.py",
        enclosing_symbol_id="one",
        reference_kind="call",
        name="load_config",
        qualifier=None,
        start_line=1,
        end_line=1,
    )
    ref_b = stable_reference_id(
        file_path="a.py",
        enclosing_symbol_id="two",
        reference_kind="call",
        name="load_config",
        qualifier=None,
        start_line=1,
        end_line=1,
    )
    assert ref_a != ref_b


def test_duplicate_nested_call_references_do_not_break_index_update(tmp_path: Path) -> None:
    project = tmp_path / "duplicate-reference-project"
    project.mkdir()
    (project / "calls.py").write_text(
        'def lookup(call):\n    return call.get("function", {}).get("name")\n',
        encoding="utf-8",
    )
    index = CodeIndex(project, db_path=tmp_path / "duplicate-reference.sqlite3")

    stats = index.update()
    references = index.store.list_symbol_references()

    assert stats.failed_files == 0
    assert len({reference.id for reference in references}) == len(references)


def test_duplicate_stable_definition_ids_do_not_break_index_update(tmp_path: Path) -> None:
    project = tmp_path / "duplicate-definition-project"
    project.mkdir()
    (project / "nested.py").write_text(
        "def outer_a():\n"
        "    def same_name():\n"
        "        return 'a'\n"
        "    return same_name()\n"
        "\n"
        "def outer_b():\n"
        "    def same_name():\n"
        "        return 'b'\n"
        "    return same_name()\n",
        encoding="utf-8",
    )
    index = CodeIndex(project, db_path=tmp_path / "duplicate-definition.sqlite3")

    stats = index.update()
    definitions = index.store.list_symbol_definitions()

    assert stats.failed_files == 0
    assert len({definition.id for definition in definitions}) == len(definitions)


def test_symbol_extraction_and_resolution_across_languages(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    stats = index.update()

    assert stats.parsed_files > 0
    assert stats.definitions_updated > 0
    assert stats.imports_updated > 0
    assert stats.references_extracted > 0
    assert stats.references_resolved > 0

    assert index.find_definitions("Service", language="python")
    assert index.find_definitions("Helper", language="java")
    assert index.find_definitions("fetchUser", language="typescript")
    assert index.find_definitions("buildClient", language="javascript")

    load_config = index.find_definitions("load_config", file_path="python_pkg/utils.py")[0]
    refs = index.find_references(load_config.id)
    assert any(ref.file_path == "python_pkg/service.py" for ref in refs)
    assert all(ref.resolution_status == "resolved" for ref in refs)

    dynamic = index.find_references("dynamic")
    assert any(ref.resolution_status == "dynamic" for ref in dynamic)

    os_refs = index.find_references("os")
    assert not os_refs or all(
        ref.resolution_status in {"external", "unresolved"} for ref in os_refs
    )


def test_schema_v5_migrates_from_v4_without_dropping_lexical_or_vectors(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    index = CodeIndex(project, db_path=db_path)
    index.update()
    with sqlite3.connect(db_path) as conn:
        conn.execute("update schema_metadata set value = '4' where key = 'schema_version'")
        fts_count = conn.execute("select count(*) from code_chunks_fts").fetchone()[0]

    store = CodeIndexStore(db_path)

    assert SCHEMA_VERSION == "6"
    assert store.schema_version() == "6"
    assert rows(db_path, "select name from sqlite_master where name = 'symbol_definitions'")
    assert rows(db_path, "select count(*) as count from code_chunks_fts")[0]["count"] == fts_count
    assert CodeIndexStore(db_path).schema_version() == "6"


def test_incremental_symbol_update_delete_and_resolution_recovery(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    db_path = tmp_path / "index.sqlite3"
    index = CodeIndex(project, db_path=db_path)
    index.update()

    service = project / "python_pkg" / "service.py"
    added_source = "\n\ndef added_symbol():\n    return load_settings('x')\n"
    service.write_text(
        service.read_text(encoding="utf-8") + added_source,
        encoding="utf-8",
    )
    updated = index.update()
    assert updated.parsed_files == 1
    assert index.find_definitions("added_symbol")

    utils = project / "python_pkg" / "utils.py"
    original_utils = utils.read_text(encoding="utf-8")
    utils.write_text(
        original_utils.replace("def load_config", "def load_config_renamed"),
        encoding="utf-8",
    )
    renamed = index.update()
    assert renamed.parsed_files == 1
    assert any(
        ref.resolution_status == "unresolved" for ref in index.find_references("load_settings")
    )

    utils.write_text(original_utils, encoding="utf-8")
    recovered = index.update()
    assert recovered.parsed_files == 1
    assert any(ref.resolution_status == "resolved" for ref in index.find_references("load_config"))

    (project / "javascript_pkg" / "helpers.js").unlink()
    deleted = index.update()
    assert deleted.deleted_files == 1
    assert not index.find_definitions("buildClient", file_path="javascript_pkg/helpers.js")


def test_symbol_queries_and_tools_are_relative_path_only(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    index.update()

    symbol = index.find_definitions("load_config")[0]
    assert symbol.file_path == "python_pkg/utils.py"
    resolved = index.resolve_symbol_at("python_pkg/service.py", 14)
    assert resolved is not None

    context = ToolContext(cwd=str(project), config=AxiomConfig())
    symbol_result = asyncio.run(find_symbol({"name": "load_config"}, context))
    refs_result = asyncio.run(find_references({"symbol": "load_config"}, context))
    search_result = asyncio.run(search_code({"query": "load config", "limit": 3}, context))

    assert str(project) not in symbol_result.content
    assert str(project) not in refs_result.content
    assert symbol_result.is_error is False
    assert refs_result.is_error is False
    assert search_result.is_error is False


def test_symbol_fixture_resolution_metrics_are_conservative(tmp_path: Path) -> None:
    project = copy_fixture(tmp_path)
    index = CodeIndex(project, db_path=tmp_path / "index.sqlite3")
    index.update()
    references = index.store.list_symbol_references()
    dynamic = [ref for ref in references if ref.resolution_status == "dynamic"]
    definitions = {
        definition.id: definition for definition in index.store.list_symbol_definitions()
    }

    expected_targets = {
        ("python_pkg/service.py", 11, None, "Helper"): "Helper",
        ("python_pkg/service.py", 18, None, "load_settings"): "load_config",
        ("python_pkg/service.py", 19, "utils_alias", "load_config"): "load_config",
        ("python_pkg/service.py", 22, None, "load_settings"): "load_config",
        ("java_pkg/Service.java", 14, "Helper", "staticName"): "Helper.staticName",
        ("javascript_pkg/service.js", 5, "helperModule", "JsClient"): "JsClient",
        ("javascript_pkg/service.js", 6, None, "buildClient"): "buildClient",
        ("typescript_pkg/helpers.ts", 15, None, "fetchUser"): "fetchUser",
        ("typescript_pkg/service.ts", 6, None, "Client"): "Client",
        ("typescript_pkg/service.ts", 7, None, "fetchAccount"): "fetchUser",
        ("typescript_pkg/service.ts", 8, None, "normalizeUser"): "normalizeUser",
        ("typescript_pkg/service.ts", 9, "helpers", "fetchUser"): "fetchUser",
    }
    expected_by_language = {
        "python": 4,
        "java": 1,
        "javascript": 2,
        "typescript": 5,
    }
    predicted_by_language = dict.fromkeys(expected_by_language, 0)
    correct_by_language = dict.fromkeys(expected_by_language, 0)
    for reference in references:
        if reference.language not in expected_by_language:
            continue
        if reference.resolution_status == "resolved":
            predicted_by_language[reference.language] += 1
        key = (
            reference.file_path,
            reference.start_line,
            reference.qualifier,
            reference.name,
        )
        expected = expected_targets.get(key)
        resolved = definitions.get(reference.resolved_symbol_id or "")
        if expected and resolved and resolved.qualified_name == expected:
            correct_by_language[reference.language] += 1

    for language, expected_count in expected_by_language.items():
        predicted_count = predicted_by_language[language]
        correct_count = correct_by_language[language]
        precision = correct_count / predicted_count
        recall = correct_count / expected_count
        f1 = 2 * precision * recall / (precision + recall)
        assert precision == 1.0
        assert recall > 0
        assert f1 > 0

    assert dynamic
    assert sum(correct_by_language.values()) == len(expected_targets)
