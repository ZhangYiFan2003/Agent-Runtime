from __future__ import annotations

import sqlite3

from axiom.memory import (
    MemoryKind,
    MemoryManager,
    MemoryScopeType,
    MemoryService,
    MemoryStore,
)
from axiom.runtime.events import RuntimeEvent


def test_memory_manager_persists_and_isolates_scopes(tmp_path):
    db_path = tmp_path / "memory.db"
    scope_a = str(tmp_path / "project-a")
    scope_b = str(tmp_path / "project-b")

    first = MemoryManager(db_path, scope=scope_a)
    first_id = first.save("alpha design decision")
    first.save("alpha runtime note")
    MemoryManager(db_path, scope=scope_b).save("beta private note")

    reloaded_a = MemoryManager(db_path, scope=scope_a)
    reloaded_b = MemoryManager(db_path, scope=scope_b)

    entries_a = reloaded_a.list()
    entries_b = reloaded_b.list()
    search_a = reloaded_a.search("alpha design")

    assert first_id == 1
    assert [entry.content for entry in entries_a] == [
        "alpha runtime note",
        "alpha design decision",
    ]
    assert [entry.content for entry in entries_b] == ["beta private note"]
    assert [entry.content for entry in search_a] == ["alpha design decision"]

    assert reloaded_b.clear() == 1
    assert reloaded_b.list() == []
    assert [entry.content for entry in reloaded_a.list()] == [
        "alpha runtime note",
        "alpha design decision",
    ]


def test_memory_store_migrates_legacy_rows_idempotently(tmp_path):
    db_path = tmp_path / "memory.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table memories (
                id integer primary key autoincrement,
                scope text not null,
                content text not null,
                created_at text not null
            )
            """
        )
        conn.execute(
            "insert into memories(scope, content, created_at) values (?, ?, ?)",
            ("project-a", "legacy decision", "2026-01-01T00:00:00+00:00"),
        )

    store = MemoryStore(db_path)
    store.ensure_schema()

    records = store.list_records(kind=MemoryKind.FACT, scope_id="project-a")
    assert store.schema_version() == "2"
    assert [(record.id, record.content) for record in records] == [("legacy_1", "legacy decision")]


def test_memory_service_typed_records_scope_isolation_and_supersession(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a", user_scope="user-a")

    old_fact = service.save_fact("prefer pytest", key="test_runner", category="preference")
    new_fact = service.save_fact("prefer uv run pytest", key="test_runner", category="preference")
    service.save_fact("user likes concise output", scope_type=MemoryScopeType.USER)
    service.save_summary("thread-a", "summary one", source_event_start_id=1, source_event_end_id=5)
    service.save_conversation("thread-a", role="user", content="hello", event_id=6)
    secret_label = "api" + "_key"
    service.save_tool_result(
        "thread-a",
        tool_name="shell",
        success=True,
        content=f"{secret_label}=abc\n" + ("x" * 800),
        source_event_id=7,
    )

    active_project = service.list_records(
        kind=MemoryKind.FACT,
        scope_type=MemoryScopeType.PROJECT,
        scope_id="project-a",
    )
    user_records = service.list_records(
        kind=MemoryKind.FACT,
        scope_type=MemoryScopeType.USER,
        scope_id="user-a",
    )
    tool_records = service.list_records(kind=MemoryKind.TOOL_RESULT, scope_id="thread-a")

    assert [record.content for record in active_project] == ["prefer uv run pytest"]
    assert old_fact.id != new_fact.id
    assert user_records[0].content == "user likes concise output"
    assert tool_records[0].metadata["tool_name"] == "shell"
    assert tool_records[0].metadata["truncated"] is True
    assert secret_label not in tool_records[0].content


def test_memory_context_budget_is_deterministic_and_evicts_old_conversation(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a", user_scope="user-a")
    service.save_summary(
        "thread-a", "short latest summary", source_event_start_id=2, source_event_end_id=3
    )
    service.save_conversation(
        "thread-a",
        role="user",
        content="old question " * 20,
        event_id=2,
        turn_id="turn-old",
        metadata={"run_id": "run-old"},
    )
    service.save_conversation(
        "thread-a",
        role="assistant",
        content="old answer " * 20,
        event_id=3,
        turn_id="turn-old",
        metadata={"run_id": "run-old"},
    )
    service.save_conversation(
        "thread-a",
        role="user",
        content="recent cancellation question",
        event_id=4,
        turn_id="turn-recent",
        metadata={"run_id": "run-recent"},
    )
    service.save_conversation(
        "thread-a",
        role="assistant",
        content="recent cancellation answer",
        event_id=5,
        turn_id="turn-recent",
        metadata={"run_id": "run-recent"},
    )
    service.save_fact("cancellation uses Runtime API", key="runtime_cancel", confidence=0.9)
    service.save_tool_result(
        "thread-a",
        tool_name="shell",
        success=True,
        content="tool output",
        source_event_id=6,
    )

    result = service.build_context(
        thread_id="thread-a",
        query="cancellation runtime",
        max_context_chars=260,
        max_estimated_tokens=200,
    )
    serialized = "".join(section.content for section in result.sections)

    assert result.truncated is True
    assert len(serialized) <= 260
    assert "short latest summary" in serialized
    assert "recent cancellation question" in serialized
    assert "recent cancellation answer" in serialized
    assert "old question" not in serialized


def test_legacy_save_appears_once_in_typed_context(tmp_path):
    db_path = tmp_path / "memory.db"
    scope = "project-a"

    MemoryManager(db_path, scope=scope).save("Use Python 3.12")
    service = MemoryService(db_path, project_scope=scope)
    result = service.build_context(query="Python")
    serialized = "".join(section.content for section in result.sections)

    assert serialized.count("Use Python 3.12") == 1


def test_memory_clear_keeps_thread_conversation_source_records(tmp_path):
    db_path = tmp_path / "memory.db"
    manager = MemoryManager(db_path, scope="project-a")
    manager.save("project fact")
    service = MemoryService(db_path, project_scope="project-a")
    service.save_conversation("thread-a", role="user", content="hello", event_id=10)
    service.save_fact("user fact", scope_type=MemoryScopeType.USER)

    assert manager.clear() == 1

    assert (
        service.list_records(
            kind=MemoryKind.FACT,
            scope_type=MemoryScopeType.PROJECT,
            scope_id="project-a",
        )
        == []
    )
    assert service.list_records(
        kind=MemoryKind.CONVERSATION,
        scope_type=MemoryScopeType.THREAD,
        scope_id="thread-a",
    )
    assert service.list_records(
        kind=MemoryKind.FACT,
        scope_type=MemoryScopeType.USER,
        scope_id="local",
    )


def _runtime_event(
    event_id: int,
    event_type: str,
    text: str = "",
    *,
    run_id: str | None = "run-a",
    turn_id: str | None = "turn-a",
    parent_run_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> RuntimeEvent:
    return RuntimeEvent(
        id=event_id,
        thread_id="thread-a",
        type=event_type,
        payload={"text": text, **(payload or {})},
        created_at="",
        run_id=run_id,
        turn_id=turn_id,
        parent_run_id=parent_run_id,
    )


def test_memory_history_uses_completed_root_identity_despite_interleaving_and_duplicates(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    events = [
        _runtime_event(1, "user.message", "first question"),
        _runtime_event(2, "user.message", "second question", run_id="run-b", turn_id="turn-b"),
        _runtime_event(3, "assistant.message", "second answer", run_id="run-b", turn_id="turn-b"),
        _runtime_event(4, "text_delta", "first "),
        _runtime_event(5, "text_delta", "answer"),
        _runtime_event(
            6, "assistant.message", "child output", run_id="child", parent_run_id="run-a"
        ),
        _runtime_event(7, "turn.completed", run_id="child", parent_run_id="run-a"),
        _runtime_event(8, "assistant.message", "first answer"),
        _runtime_event(9, "turn.completed"),
        _runtime_event(10, "assistant.message", "first answer"),
        _runtime_event(11, "turn.completed"),
        _runtime_event(12, "turn.completed", run_id="run-b", turn_id="turn-b"),
        _runtime_event(13, "user.message", "pending user", run_id="pending", turn_id="pending"),
        _runtime_event(
            14, "assistant.message", "partial answer", run_id="pending", turn_id="pending"
        ),
    ]
    events.append(events[7])

    history = service.history_from_runtime_events(reversed(events))

    assert [(message.role, message.content) for message in history] == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
        ("assistant", "second answer"),
    ]


def test_memory_history_only_accepts_unambiguous_identified_legacy_messages(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    identity = {"run_id": "legacy", "turn_id": "legacy-turn", "thread_id": "thread-a"}
    events = [
        _runtime_event(1, "user.message", "unidentified question", run_id=None, turn_id=None),
        _runtime_event(2, "assistant.message", "unidentified answer", run_id=None, turn_id=None),
        _runtime_event(
            3, "user.message", "known question", run_id=None, turn_id=None, payload=identity
        ),
        _runtime_event(
            4, "assistant.message", "known answer", run_id=None, turn_id=None, payload=identity
        ),
        _runtime_event(5, "run.completed", run_id=None, turn_id=None, payload=identity),
        _runtime_event(6, "user.message", "conflicting question"),
        _runtime_event(7, "assistant.message", "wrong identity", payload={"run_id": "other-run"}),
        _runtime_event(8, "turn.completed"),
    ]

    history = service.history_from_runtime_events(events)

    assert [(message.role, message.content) for message in history] == [
        ("user", "known question"),
        ("assistant", "known answer"),
    ]


def test_tool_result_digest_redacts_secret_path_and_binary_like_output(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    binary_like = "A" * 100
    absolute_path = "D:" + "\\Code\\secret.txt"
    secret_label = "api" + "_key"
    service.save_tool_result(
        "thread-a",
        tool_name="shell",
        success=True,
        content=f"{secret_label}=value\n{absolute_path}\n{binary_like}",
        source_event_id=1,
    )

    record = service.list_records(kind=MemoryKind.TOOL_RESULT, scope_id="thread-a")[0]

    assert secret_label not in record.content
    assert absolute_path not in record.content
    assert binary_like not in record.content
    assert "[redacted-path]" in record.content
    assert "[redacted-binary]" in record.content
