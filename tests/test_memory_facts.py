from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from contextlib import suppress

import pytest

from axiom.config import load_config
from axiom.memory import (
    FactCandidate,
    MemoryKind,
    MemoryScopeType,
    MemoryService,
)
from axiom.runtime.api import RuntimeApiServer, RuntimeTurnContext
from axiom.runtime.events import RuntimeEvent
from axiom.types import Message


class Event(RuntimeEvent):
    def __init__(
        self,
        event_id: int,
        event_type: str,
        payload: dict[str, object],
        *,
        run_id: str | None = "run-a",
        turn_id: str | None = "turn-a",
        parent_run_id: str | None = None,
    ):
        super().__init__(
            id=event_id,
            thread_id="thread-a",
            type=event_type,
            payload=payload,
            created_at="",
            run_id=run_id,
            turn_id=turn_id,
            parent_run_id=parent_run_id,
        )


class BrokenExtractor:
    async def extract(self, _messages: Sequence[Message], *, summary: str | None = None):
        raise RuntimeError("extractor failed")


class FakeEngine:
    async def ask(self, _message: str):
        yield {"type": "text_delta", "text": "ok"}
        yield {"type": "done", "stop_reason": "stop"}


def test_fact_extraction_scope_policy_and_cross_thread_reuse(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a", user_scope="user-a")
    events = [
        Event(
            1,
            "user.message",
            {
                "text": "\n".join(
                    [
                        "remember project environment:python_version=3.12",
                        "remember user preference:response_language=Chinese",
                        "remember thread constraint:modify_scope=tests only",
                    ]
                )
            },
        ),
        Event(2, "assistant.message", {"text": "remember user preference:theme=dark"}),
        Event(3, "tool_result", {"content": "remember user preference:shell=bash"}),
        Event(
            4, "user.message", {"text": "maybe use Java someday"}, run_id="run-b", turn_id="turn-b"
        ),
        Event(5, "turn.completed", {}),
    ]

    result = asyncio.run(service.extract_facts_from_thread("thread-a", events))

    assert result.candidate_count == 3
    assert result.accepted_count == 3
    same_thread = "".join(
        section.content for section in service.build_context(thread_id="thread-a").sections
    )
    other_thread = "".join(
        section.content for section in service.build_context(thread_id="thread-b").sections
    )
    assert "python_version" in same_thread
    assert "response_language" in same_thread
    assert "modify_scope" in same_thread
    assert "python_version" in other_thread
    assert "response_language" in other_thread
    assert "modify_scope" not in other_thread


def test_duplicate_merge_and_conflict_supersession_keep_one_active_value(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")

    old = service.save_fact(
        "merge commit",
        key="workflow:merge_strategy",
        category="workflow",
        confidence=0.95,
    )
    duplicate = service.save_fact(
        "merge commit",
        key="workflow:merge_strategy",
        category="workflow",
        confidence=0.96,
    )
    new = service.save_fact(
        "squash merge",
        key="workflow:merge_strategy",
        category="workflow",
        confidence=0.97,
    )

    active = service.list_records(kind=MemoryKind.FACT, scope_id="project-a")
    history = service.fact_history("workflow:merge_strategy", limit=10)

    assert duplicate.id == old.id
    assert new.id != old.id
    assert [record.content for record in active] == ["squash merge"]
    assert len([record for record in history if record.metadata.get("active", True)]) == 1
    assert any(record.metadata.get("superseded_by") == new.id for record in history)
    assert (
        next(record for record in history if record.id == old.id).metadata["observation_count"] == 2
    )


def test_same_scope_normalized_key_has_one_active_value_across_categories(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")

    first = service.save_fact(
        "old value",
        key="workflow:merge_strategy",
        category="workflow",
    )
    second = service.save_fact(
        "new value",
        key="workflow:merge_strategy",
        category="project_decision",
    )

    active = service.list_records(kind=MemoryKind.FACT, scope_id="project-a")
    history = service.fact_history("workflow:merge_strategy")
    assert [record.content for record in active] == ["new value"]
    assert second.id != first.id
    assert any(record.metadata.get("superseded_by") == second.id for record in history)


def test_database_rejects_two_active_values_for_same_scoped_key(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    service.save_fact(
        "squash merge",
        key="workflow:merge_strategy",
        category="workflow",
    )

    with sqlite3.connect(tmp_path / "memory.db") as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            insert into memory_records(
                id, kind, scope_type, scope_id, content, created_at, updated_at,
                source_event_start_id, source_event_end_id, metadata
            )
            values (?, 'fact', 'project', 'project-a', ?, ?, ?, null, null, ?)
            """,
            (
                "manual_duplicate",
                "merge commit",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                json.dumps(
                    {
                        "active": True,
                        "key": "workflow:merge_strategy",
                        "category": "workflow",
                    }
                ),
            ),
        )

    active = service.list_records(kind=MemoryKind.FACT, scope_id="project-a")
    assert [record.content for record in active] == ["squash merge"]


def test_context_uses_scope_precedence_for_same_logical_key(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a", user_scope="user-a")
    service.save_fact(
        "Chinese",
        key="preference:response_language",
        category="preference",
        scope_type=MemoryScopeType.USER,
    )
    service.save_fact(
        "English",
        key="preference:response_language",
        category="preference",
        scope_type=MemoryScopeType.PROJECT,
    )
    service.store.save_fact_value(
        scope_type=MemoryScopeType.THREAD,
        scope_id="thread-a",
        key="preference:response_language",
        category="preference",
        content="French",
        confidence=0.95,
        source_event_start_id=1,
        source_event_end_id=1,
    )

    serialized = "".join(
        section.content for section in service.build_context(thread_id="thread-a").sections
    )

    assert "French" in serialized
    assert "English" not in serialized
    assert "Chinese" not in serialized


def test_retraction_excludes_fact_from_context_but_keeps_history(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    service.save_fact(
        "squash merge",
        key="workflow:merge_strategy",
        category="workflow",
    )

    forgotten = service.forget_fact("workflow:merge_strategy")

    serialized = "".join(
        section.content for section in service.build_context(query="merge").sections
    )
    history = service.fact_history("workflow:merge_strategy")
    assert forgotten == 1
    assert "squash merge" not in serialized
    assert len(history) == 1
    assert history[0].metadata["retracted"] is True


def test_conflict_insert_failure_keeps_old_fact_active(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    old = service.save_fact(
        "merge commit",
        key="workflow:merge_strategy",
        category="workflow",
    )

    with suppress(Exception):
        service.store.save_fact_value(
            scope_type=MemoryScopeType.PROJECT,
            scope_id="project-a",
            key="workflow:merge_strategy",
            category="workflow",
            content="squash merge",
            confidence=0.95,
            record_id=old.id,
        )

    active = service.list_records(kind=MemoryKind.FACT, scope_id="project-a")
    assert [(record.id, record.content) for record in active] == [(old.id, "merge commit")]


def test_sensitive_candidates_are_rejected_and_checkpointed(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    label = "api" + "_key"
    events = [
        Event(1, "user.message", {"text": f"remember project workflow:secret={label}=abc"}),
        Event(2, "assistant.message", {"text": "ok"}),
        Event(3, "turn.completed", {}),
        Event(
            4,
            "user.message",
            {"text": "remember project workflow:merge_strategy=squash"},
            run_id="run-b",
            turn_id="turn-b",
        ),
        Event(5, "assistant.message", {"text": "ok"}, run_id="run-b", turn_id="turn-b"),
        Event(6, "turn.completed", {}, run_id="run-b", turn_id="turn-b"),
    ]

    first = asyncio.run(service.extract_facts_from_thread("thread-a", events))
    second = asyncio.run(service.extract_facts_from_thread("thread-a", events))

    active = service.list_records(kind=MemoryKind.FACT, scope_id="project-a")
    assert first.candidate_count == 2
    assert first.rejected_count == 1
    assert second.candidate_count == 0
    assert [record.content for record in active] == ["squash"]


def test_duplicate_merge_bounds_supporting_provenance(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")

    for event_id in range(1, 30):
        service.store.save_fact_value(
            scope_type=MemoryScopeType.PROJECT,
            scope_id="project-a",
            key="workflow:merge_strategy",
            category="workflow",
            content="squash merge",
            confidence=0.95,
            source_event_start_id=event_id,
            source_event_end_id=event_id,
        )

    record = service.list_records(kind=MemoryKind.FACT, scope_id="project-a")[0]
    assert record.created_at < record.updated_at
    assert record.metadata["observation_count"] == 29
    assert len(record.metadata["supporting_event_ranges"]) == 20
    assert record.metadata["supporting_event_ranges"][0] == [1, 1]


def test_extractor_failure_does_not_advance_incremental_checkpoint(tmp_path):
    db_path = tmp_path / "memory.db"
    broken = MemoryService(
        db_path,
        project_scope="project-a",
        fact_extractor=BrokenExtractor(),
    )
    events = [
        Event(1, "user.message", {"text": "remember project workflow:merge_strategy=squash"}),
        Event(2, "assistant.message", {"text": "ok"}),
        Event(3, "turn.completed", {}),
    ]

    failed = asyncio.run(broken.extract_facts_from_thread("thread-a", events))
    recovered = MemoryService(db_path, project_scope="project-a")
    retried = asyncio.run(recovered.extract_facts_from_thread("thread-a", events))

    assert failed.error == "RuntimeError"
    assert retried.accepted_count == 1
    assert recovered.list_records(kind=MemoryKind.FACT, scope_id="project-a")[0].content == "squash"


def test_fact_extraction_waits_for_identified_root_completion_and_deduplicates_callbacks(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    events = [
        Event(1, "user.message", {"text": "remember project workflow:pending=confirmed"}),
        Event(
            2,
            "user.message",
            {"text": "remember project workflow:ready=done"},
            run_id="run-b",
            turn_id="turn-b",
        ),
        Event(3, "assistant.message", {"text": "done"}, run_id="run-b", turn_id="turn-b"),
        Event(4, "turn.completed", {}, run_id="run-b", turn_id="turn-b"),
    ]

    first = asyncio.run(service.extract_facts_from_thread("thread-a", events))
    assert first.accepted_count == 1
    assert first.processed_event_end_id == 4

    events.extend(
        [
            Event(5, "assistant.message", {"text": "confirmed"}),
            Event(6, "turn.completed", {}),
            Event(7, "assistant.message", {"text": "confirmed"}),
            Event(8, "turn.completed", {}),
            Event(
                9,
                "user.message",
                {"text": "remember project workflow:child=exclude"},
                run_id="child",
                parent_run_id="run-a",
            ),
            Event(
                10,
                "assistant.message",
                {"text": "child output"},
                run_id="child",
                parent_run_id="run-a",
            ),
            Event(11, "turn.completed", {}, run_id="child", parent_run_id="run-a"),
            Event(
                12,
                "user.message",
                {"text": "remember project workflow:unknown=exclude"},
                run_id=None,
                turn_id=None,
            ),
            Event(13, "assistant.message", {"text": "unknown output"}, run_id=None, turn_id=None),
            Event(14, "turn.completed", {}, run_id=None, turn_id=None),
        ]
    )
    second = asyncio.run(service.extract_facts_from_thread("thread-a", events))
    repeated = asyncio.run(service.extract_facts_from_thread("thread-a", events))

    assert second.accepted_count == 1
    assert second.processed_event_end_id == 6
    assert repeated.candidate_count == 0
    records = service.list_records(kind=MemoryKind.FACT, scope_id="project-a")
    assert {record.metadata["key"] for record in records} == {"workflow:pending", "workflow:ready"}
    assert {record.source_event_start_id for record in records} == {1, 2}


def test_thread_fact_does_not_overwrite_project_fact(tmp_path):
    service = MemoryService(tmp_path / "memory.db", project_scope="project-a")
    project = service.save_fact(
        "squash merge",
        key="workflow:merge_strategy",
        category="workflow",
    )
    thread_candidate = FactCandidate(
        key="workflow:merge_strategy",
        value="merge commit for this thread",
        category="workflow",
        scope_type=MemoryScopeType.THREAD,
        confidence=0.95,
        explicit=True,
        source_event_start_id=1,
        source_event_end_id=1,
    )
    service.store.save_fact_value(
        scope_type=thread_candidate.scope_type,
        scope_id="thread-a",
        key=thread_candidate.key,
        category=thread_candidate.category,
        content=thread_candidate.value,
        confidence=thread_candidate.confidence,
        source_event_start_id=thread_candidate.source_event_start_id,
        source_event_end_id=thread_candidate.source_event_end_id,
    )

    assert service.list_records(kind=MemoryKind.FACT, scope_id="project-a")[0].id == project.id
    context = "".join(
        section.content for section in service.build_context(thread_id="thread-a").sections
    )
    assert "merge commit for this thread" in context
    assert "squash merge" not in context


def test_runtime_fact_extraction_failure_does_not_fail_completed_turn(tmp_path):
    contexts: list[RuntimeTurnContext] = []

    def engine_factory(context: RuntimeTurnContext) -> FakeEngine:
        contexts.append(context)
        return FakeEngine()

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-api-key",
        port=0,
        workers=0,
        data_dir=tmp_path / "runtime-data",
        engine_factory=engine_factory,
    )
    server.memory_service = MemoryService(
        tmp_path / "runtime-data" / "memory.db",
        project_scope=str(tmp_path),
        fact_extractor=BrokenExtractor(),
    )
    thread_id = server.repository.create_thread()

    response = asyncio.run(server._run_turn(thread_id, "remember project workflow:x=y"))

    assert response["thread_id"] == thread_id
    assert response["text"] == "ok"
    assert response["run_id"]
    assert response["turn_id"]
    assert response["status"] == "COMPLETED"
    assert contexts
    completed = [
        event
        for event in server.repository.list_events(thread_id)
        if event.type == "turn.completed"
    ]
    assert len(completed) == 1
    assert completed[0].run_id == response["run_id"]
    assert completed[0].turn_id == response["turn_id"]
