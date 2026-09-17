from __future__ import annotations

import asyncio
import sqlite3

from axiom.runtime.api import RuntimeRequestContext, RuntimeTurnContext
from axiom.runtime.checkpoints import (
    SQLiteCheckpointStore,
    advance_run_state,
    load_run_state,
)
from axiom.runtime.events import ThreadEventRepository
from axiom.runtime.models import Checkpoint, RunState


def test_checkpoint_is_backward_compatible_name_for_run_state():
    state = RunState.create(thread_id="thread", run_id="run", input="work")
    restored = RunState.from_dict(state.to_dict())

    assert Checkpoint is RunState
    assert isinstance(restored, RunState)
    assert restored.to_dict() == state.to_dict()


def test_thread_can_contain_multiple_root_runs(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.db")
    first = RunState.create(thread_id="thread", run_id="run-1", input="first")
    second = RunState.create(thread_id="thread", run_id="run-2", input="second")

    asyncio.run(advance_run_state(store, first))
    asyncio.run(advance_run_state(store, second))

    states = asyncio.run(store.list("thread"))
    assert [state.run_id for state in states] == ["run-1", "run-2"]
    assert all(state.parent_run_id is None for state in states)


def test_child_run_remains_a_durable_run_not_a_step(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.db")
    parent = RunState.create(thread_id="thread", run_id="parent", input="parent")
    child = RunState.create(
        thread_id="thread",
        run_id="child",
        input="child",
        parent_run_id=parent.run_id,
        parent_step_id="ephemeral-step-correlation",
    )
    asyncio.run(advance_run_state(store, parent))
    asyncio.run(advance_run_state(store, child))

    restored = asyncio.run(load_run_state(store, child.run_id))
    assert restored is not None
    assert restored.parent_run_id == parent.run_id


def test_run_recovery_does_not_require_event_replay(tmp_path):
    database = tmp_path / "runtime.db"
    store = SQLiteCheckpointStore(database)
    state = RunState.create(thread_id="thread", run_id="run", input="recover")
    state.step_index = 3
    asyncio.run(advance_run_state(store, state))

    reopened = SQLiteCheckpointStore(database)
    restored = asyncio.run(load_run_state(reopened, state.run_id))
    assert restored is not None
    assert restored.sequence == 1
    assert restored.step_index == 3


def test_event_history_is_independent_evidence(tmp_path):
    database = tmp_path / "runtime.db"
    events = ThreadEventRepository(database)
    thread_id = events.create_thread()
    events.append_event(thread_id, "user.message", {"text": "hello"})
    store = SQLiteCheckpointStore(database)
    state = RunState.create(thread_id=thread_id, run_id="run", input="recover")
    asyncio.run(advance_run_state(store, state))
    with sqlite3.connect(database) as conn:
        conn.execute("delete from events where thread_id = ?", (thread_id,))

    assert events.list_events(thread_id) == []
    assert asyncio.run(load_run_state(store, state.run_id)) is not None


def test_schema_has_no_turn_or_step_domain_tables(tmp_path):
    database = tmp_path / "runtime.db"
    SQLiteCheckpointStore(database)
    ThreadEventRepository(database)
    with sqlite3.connect(database) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            ).fetchall()
        }

    assert "checkpoints" in tables
    assert "turns" not in tables
    assert "steps" not in tables


def test_turn_context_is_compatibility_alias_for_request_context():
    assert RuntimeTurnContext is RuntimeRequestContext
