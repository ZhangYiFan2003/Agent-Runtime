from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from axiom.artifacts import ArtifactBlob, ArtifactRecord, ArtifactReuseEntry
from axiom.config import (
    AxiomConfig,
    StorageConfig,
    TrafficGovernanceConfig,
    config_to_public_dict,
    load_config,
)
from axiom.provenance import ClaimEvidenceLink, ClaimRecord, EvidenceRecord, EvidenceSourceType
from axiom.runtime.capacity import AdmissionRejectedError
from axiom.runtime.checkpoints import BudgetLedgerConflictError, CheckpointConflictError
from axiom.runtime.control_plane import ApiError, ControlOperationName
from axiom.runtime.durable import DurableAgentRuntime
from axiom.runtime.models import (
    BudgetLedgerRecord,
    Checkpoint,
    Interrupt,
    RunError,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolRetryState,
)
from axiom.runtime.storage import DurableStorage, create_durable_storage
from axiom.tools import ToolRegistry


def _submission(storage, thread_id, *, key="request", text="hello", **kwargs):
    return asyncio.run(storage.runtime.admit_submission(
        Checkpoint.create(thread_id=thread_id, input=text),
        idempotency_key=key, request_fingerprint=text, **kwargs,
    ))


def _submission_counts(storage):
    store = storage.runtime
    connection = store._connect() if storage.backend == "sqlite" else store.pool.connection()
    with connection as conn:
        tables = ["checkpoints", "submission_idempotency", "events", "traffic_rate_buckets"]
        if storage.backend == "postgres":
            tables.append("runs")
        return {table: conn.execute(f"select count(*) from {table}").fetchone()[0]
                for table in tables}


@contextmanager
def _isolated_postgres_storage(tmp_path, prefix: str) -> Iterator[DurableStorage]:
    dsn = os.environ.get("AXIOM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AXIOM_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    conninfo = pytest.importorskip("psycopg.conninfo")
    schema = f"{prefix}_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    isolated_dsn = conninfo.make_conninfo(dsn, options=f"-c search_path={schema}")
    storage = create_durable_storage(
        StorageConfig(backend="postgres", postgres_dsn=isolated_dsn),
        default_sqlite_path=tmp_path / "unused.db",
    )
    try:
        yield storage
    finally:
        storage.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


@pytest.fixture(params=("sqlite", "postgres"))
def durable_storage(request, tmp_path) -> Iterator[DurableStorage]:
    if request.param == "sqlite":
        storage = create_durable_storage(
            StorageConfig(), default_sqlite_path=tmp_path / "runtime.db"
        )
        yield storage
        storage.close()
        return
    with _isolated_postgres_storage(tmp_path, "axiom_test") as storage:
        yield storage


@pytest.fixture
def postgres_storage(tmp_path) -> Iterator[DurableStorage]:
    with _isolated_postgres_storage(tmp_path, "axiom_postgres_contract") as storage:
        yield storage


def _completed_submission(storage, thread_id, index):
    state, _ = _submission(storage, thread_id, key=f"page-{index}", text=f"question {index}")
    state.status = RunStatus.COMPLETED
    state.output_text = f"answer {index}"
    asyncio.run(storage.runtime.save(state))
    return state


def test_message_pages_ignore_log_window_and_preserve_both_boundaries(durable_storage):
    storage = durable_storage
    thread_id = storage.events.create_thread()
    states = [_completed_submission(storage, thread_id, index) for index in range(4)]
    # All timestamps can tie; the event sequence is the stable ordering authority.
    connection = (storage.runtime._connect() if storage.backend == "sqlite"
                  else storage.runtime.pool.connection())
    with connection as conn:
        if storage.backend == "sqlite":
            conn.executemany(
                "insert into events(thread_id,type,payload,created_at) values (?, ?, ?, ?)",
                [(thread_id, "tool.delta", "{}", "2026-10-19T00:00:00+00:00")] * 1601,
            )
        else:
            conn.execute("select id from threads where id = %s for update", (thread_id,))
            with conn.cursor() as cursor:
                cursor.executemany(
                    "insert into events(thread_id,type,payload,created_at) "
                    "values (%s, %s, %s::jsonb, %s)",
                    [(thread_id, "tool.delta", "{}", "2026-10-19T00:00:00+00:00")] * 1601,
                )
    latest, watermark = storage.events.page_messages(thread_id, limit=3)
    assert [event.payload["text"] for event in latest] == [
        "question 2", "answer 2", "question 3", "answer 3",
    ]  # LIMIT + 1, newest page has the lookahead at its beginning.
    displayed = latest[-3:]
    older, _ = storage.events.page_messages(thread_id, before_id=displayed[0].id, limit=3)
    assert [event.payload["text"] for event in older[-3:]] == [
        "question 1", "answer 1", "question 2",
    ]
    assert watermark == displayed[-1].id
    _completed_submission(storage, thread_id, 4)
    new, _ = storage.events.page_messages(thread_id, after_id=watermark, limit=3)
    assert [event.payload["text"] for event in new] == ["question 4", "answer 4"]
    # An older-page request cannot change the independent incremental cursor.
    assert all(event.id > watermark for event in new)
    assert {event.run_id for event in latest} == {state.run_id for state in states[-2:]}
    logs, _ = storage.events.page_events(thread_id, latest=True, limit=3)
    assert len(logs) == 4  # Final submission events; not a full history read.
    assert len(storage.events.list_events(thread_id)) > 1600


def test_message_page_excludes_children_and_ambiguous_records(durable_storage):
    storage = durable_storage
    thread_id = storage.events.create_thread()
    root = _completed_submission(storage, thread_id, 0)
    child = Checkpoint.create(thread_id=thread_id, input="child", parent_run_id=root.run_id)
    child.status = RunStatus.COMPLETED
    asyncio.run(storage.runtime.save(child))
    for role in ("user", "assistant"):
        storage.events.append_event(thread_id, f"{role}.message", {
            "run_id": child.run_id, "turn_id": child.turn_id, "text": "hidden child",
        })
    storage.events.append_event(thread_id, "user.message", {"text": "unassociated legacy"})
    duplicate = {
        "thread_id": thread_id, "run_id": root.run_id, "turn_id": root.turn_id,
        "text": "question 0",
    }
    storage.events.append_event(thread_id, "user.message", duplicate)
    messages, _ = storage.events.page_messages(thread_id)
    assert [message.payload["text"] for message in messages] == ["question 0", "answer 0"]
    storage.events.append_event(thread_id, "user.message", {**duplicate, "text": "conflict"})
    messages, _ = storage.events.page_messages(thread_id)
    assert messages == []
    assert any(event.payload.get("text") == "conflict"
               for event in storage.events.list_events(thread_id))


def test_event_pages_have_exclusive_boundaries_and_run_scope(durable_storage):
    storage = durable_storage
    thread_id = storage.events.create_thread()
    states = [_completed_submission(storage, thread_id, index) for index in range(3)]
    ids = []
    after = 0
    while True:
        page, _ = storage.events.page_events(thread_id, after_id=after, limit=2)
        visible = page[:2]
        ids.extend(event.id for event in visible)
        if len(page) <= 2:
            break
        after = visible[-1].id
    assert ids == [event.id for event in storage.events.list_events(thread_id)]
    scoped, _ = storage.events.page_events(thread_id, run_id=states[1].run_id, limit=2)
    assert len(scoped) == 3 and {event.run_id for event in scoped} == {states[1].run_id}
    empty, watermark = storage.events.page_events(thread_id, after_id=ids[-1], limit=2)
    assert empty == [] and watermark == 0


@pytest.mark.parametrize("sort", ["newest", "oldest", "status", "duration"])
def test_run_summary_pages_sort_and_filter_globally(durable_storage, sort):
    storage = durable_storage
    states = []
    for index in range(9):
        state = Checkpoint.create(
            thread_id=storage.events.create_thread(), input="large recovery authority",
            run_id=f"run_page_{index:02}",
            execution_strategy="react" if index % 2 else "plan_execute",
        )
        state.created_at = f"2026-10-19T00:00:{index // 3:02}+00:00"
        state.status = [RunStatus.RUNNING, RunStatus.FAILED, RunStatus.COMPLETED][index % 3]
        asyncio.run(storage.runtime.save(state))
        states.append(state)
    child = Checkpoint.create(thread_id=states[0].thread_id, input="child",
                              parent_run_id=states[0].run_id, run_id="child_page")
    asyncio.run(storage.runtime.save(child))
    expected = asyncio.run(storage.runtime.list_run_summaries(limit=100, sort=sort))
    assert len(expected) == 10
    actual = []
    position = None
    while True:
        page = asyncio.run(storage.runtime.list_run_summaries(
            limit=2, position=position, sort=sort,
        ))
        actual.extend(page[:2])
        if len(page) <= 2:
            break
        last = page[1]
        position = (last["_cursor_value"], last["created_at"], last["run_id"])
    assert [item["run_id"] for item in actual] == [item["run_id"] for item in expected]
    parent_summary = next(item for item in actual if item["run_id"] == states[0].run_id)
    assert parent_summary["children_count"] == 1
    filtered = asyncio.run(storage.runtime.list_run_summaries(
        limit=2, search="RUN_PAGE_", status="COMPLETED", strategy="plan_execute", sort=sort,
    ))
    assert {item["run_id"] for item in filtered} == {"run_page_02", "run_page_08"}
    assert asyncio.run(storage.runtime.thread_run_head(states[0].thread_id)) == {
        "run_id": states[0].run_id, "status": "RUNNING",
    }


@pytest.mark.parametrize("history_size", [20, 120])
def test_sqlite_run_page_reads_two_bounded_queries_without_checkpoint_decoding(
    tmp_path, monkeypatch, history_size,
):
    from axiom.runtime.checkpoints import SQLiteCheckpointStore

    store = SQLiteCheckpointStore(tmp_path / "bounded.db")
    # Use actual historical checkpoints without admission's unrelated capacity work.
    with store._connect() as conn:
        for index in range(history_size):
            state = Checkpoint.create(thread_id="thread", input="x" * 1000,
                                      run_id=f"run_{index:03}")
            state.status = RunStatus.COMPLETED
            for sequence in range(1, 4):
                store._write_checkpoint(conn, state, sequence=sequence,
                                        updated_at=state.updated_at)
    statements = []
    connect = store._connect

    def trace_connect():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    def forbid_decode(*args, **kwargs):
        raise AssertionError("Run list must not deserialize recovery authority")

    monkeypatch.setattr(store, "_connect", trace_connect)
    monkeypatch.setattr(Checkpoint, "from_dict", forbid_decode)
    rows = asyncio.run(store.list_run_summaries(limit=5))
    reads = [sql for sql in statements if sql.lstrip().lower().startswith("with")]
    assert len(rows) == 6 and len(reads) == 2
    assert "limit 6" in reads[0].lower()
    assert "state_json from" not in reads[0].lower()
    assert "parent_run_id in (" in reads[1]


@pytest.mark.postgres
@pytest.mark.parametrize("history_size", [20, 120])
def test_postgres_run_page_reads_two_queries_without_checkpoint_decoding(
    postgres_storage, monkeypatch, history_size,
):
    from axiom.runtime.postgres import PostgresRuntimeStore, _insert_initial_run

    pool = postgres_storage.runtime.pool
    with pool.connection() as conn:
        for index in range(history_size):
            state = Checkpoint.create(thread_id="thread", input="x" * 1000,
                                      run_id=f"run_{index:03}")
            state.status = RunStatus.COMPLETED
            _insert_initial_run(conn, state, updated_at=state.updated_at, runnable=False)
        conn.execute(
            "insert into checkpoints(run_id, sequence, schema_version, thread_id, turn_id, "
            "status, state_json, created_at) select run_id, 1 + extra, schema_version, "
            "thread_id, turn_id, status, state_json, created_at from checkpoints "
            "cross join (values (1), (2)) more(extra) where sequence = 1"
        )
    reads = []

    class ObservedConnection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, parameters=()):
            reads.append((statement, parameters))
            return self.conn.execute(statement, parameters)

    class ObservedPool:
        @contextmanager
        def connection(self):
            with pool.connection() as conn:
                yield ObservedConnection(conn)

    def forbid_decode(*args, **kwargs):
        raise AssertionError("Run list must not deserialize recovery authority")

    monkeypatch.setattr(Checkpoint, "from_dict", forbid_decode)
    rows = asyncio.run(PostgresRuntimeStore(ObservedPool()).list_run_summaries(limit=5))
    assert len(rows) == 6 and len(reads) == 2
    assert reads[0][1] == (6,)
    assert "from checkpoints" not in reads[0][0].lower()
    assert "parent_run_id in (" in reads[1][0]


def test_postgres_event_id_allocation_waits_for_same_thread_commit(postgres_storage):
    from axiom.runtime.postgres import PostgresEventRepository

    storage = postgres_storage
    thread_id = storage.events.create_thread()
    other_thread = storage.events.create_thread()
    initial, watermark = storage.events.page_events(thread_id, latest=True)
    assert watermark == initial[-1].id
    allocated = threading.Event()
    release_commit = threading.Event()
    second_attempted = threading.Event()

    class PausedConnection:
        def __init__(self, conn, first):
            self.conn = conn
            self.first = first

        def execute(self, statement, parameters=()):
            if not self.first and "select id from threads" in statement.lower():
                second_attempted.set()
            result = self.conn.execute(statement, parameters)
            if self.first and "insert into events" in statement.lower():
                allocated.set()
                assert release_commit.wait(10)
            return result

    class PausedPool:
        def __init__(self, first):
            self.first = first

        @contextmanager
        def connection(self):
            with storage.runtime.pool.connection() as conn:
                yield PausedConnection(conn, self.first)

    first = PostgresEventRepository(PausedPool(True))
    second = PostgresEventRepository(PausedPool(False))
    with ThreadPoolExecutor(max_workers=3) as executor:
        first_write = executor.submit(first.append_event, thread_id, "first", {})
        try:
            assert allocated.wait(10)
            second_write = executor.submit(second.append_event, thread_id, "second", {})
            assert second_attempted.wait(10)
            # The second transaction cannot publish a higher cursor while the first
            # has allocated a lower ID but has not committed yet.
            visible, _ = storage.events.page_events(thread_id, after_id=watermark)
            assert visible == []
            # A held Thread transaction must not lock the event stream globally.
            independent = executor.submit(
                storage.events.append_event, other_thread, "independent", {},
            )
            assert independent.result(10) > watermark
            other_events, _ = storage.events.page_events(other_thread)
            assert other_events[-1].type == "independent"
        finally:
            release_commit.set()
        first_id, second_id = first_write.result(10), second_write.result(10)
    visible, _ = storage.events.page_events(thread_id, after_id=watermark)
    assert [event.id for event in visible] == [first_id, second_id]
    assert first_id < second_id


def test_storage_configuration_defaults_to_sqlite_and_redacts_dsn(tmp_path):
    config = load_config(project_root=tmp_path, env={})
    assert config.storage.backend == "sqlite"

    config.storage.postgres_dsn = "postgresql://user:secret@example.invalid/db"
    assert config_to_public_dict(config)["storage"]["postgres_dsn"] == "***"


@pytest.mark.parametrize("key", [None, "request"])
def test_submission_atomically_persists_identity_and_events(durable_storage, key):
    thread_id = durable_storage.events.create_thread()
    state, created = _submission(durable_storage, thread_id, key=key)
    assert created and state.sequence == 1
    assert [message.content for message in state.messages] == ["hello"]
    stored = asyncio.run(durable_storage.runtime.load(state.run_id))
    assert stored.turn_id == state.turn_id
    events = [event for event in durable_storage.events.list_events(thread_id)
              if event.type in {"turn.started", "user.message"}]
    assert [event.type for event in events] == ["turn.started", "user.message"]
    assert {(event.thread_id, event.run_id, event.turn_id) for event in events} == {
        (thread_id, state.run_id, state.turn_id)
    }
    assert all(event.payload["run_id"] == state.run_id for event in events)
    admitted_events = [event for event in durable_storage.events.list_events(thread_id)
                       if event.type == "run.admitted"]
    assert len(admitted_events) == 1
    assert admitted_events[0].run_id == state.run_id
    assert admitted_events[0].turn_id == state.turn_id
    assert admitted_events[0].payload["status"] == RunStatus.RUNNING.value
    assert events[0].payload["message_chars"] == len("hello")


@pytest.mark.parametrize("crash_event", ["user.message", "run.admitted"])
def test_submission_failure_rolls_back_run_mapping_events_and_rate(
    durable_storage, monkeypatch, crash_event,
):
    from axiom.runtime import postgres

    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    before = _submission_counts(durable_storage)
    if durable_storage.backend == "sqlite":
        target, attribute = store, "_append_event"
    else:
        target, attribute = postgres, "_append_submission_event"
    original = getattr(target, attribute)

    def fail_after_event(conn, state, event_type, payload):
        event_id = original(conn, state, event_type, payload)
        if event_type == crash_event:
            raise RuntimeError("injected submission crash")
        return event_id

    monkeypatch.setattr(target, attribute, fail_after_event)
    candidate = Checkpoint.create(thread_id=thread_id, input="hello")
    original_candidate = deepcopy(candidate.to_dict())
    with pytest.raises(RuntimeError, match="injected submission crash"):
        asyncio.run(store.admit_submission(
            candidate, idempotency_key="request", request_fingerprint="hello",
            traffic=TrafficGovernanceConfig(global_submission_rate=1, global_submission_burst=1),
        ))
    assert candidate.to_dict() == original_candidate
    assert _submission_counts(durable_storage) == before
    assert asyncio.run(store.list(thread_id)) == []
    assert asyncio.run(store.lookup_submission(thread_id, "request", "hello")) is None


@pytest.mark.parametrize("principal_limit", [False, True], ids=["global", "principal"])
def test_capacity_rejection_does_not_reserve_submission_identity(durable_storage, principal_limit):
    first_thread = durable_storage.events.create_thread()
    _submission(durable_storage, first_thread)
    rejected_thread = durable_storage.events.create_thread()
    before = _submission_counts(durable_storage)
    limit = {"max_queued_runs_per_principal": 1} if principal_limit else {"max_queued_runs": 1}
    with pytest.raises((AdmissionRejectedError, ApiError)) as rejected:
        _submission(durable_storage, rejected_thread, **limit)
    if principal_limit:
        assert rejected.value.code == "PRINCIPAL_QUEUE_QUOTA_EXCEEDED"
    else:
        assert isinstance(rejected.value, AdmissionRejectedError)
    assert _submission_counts(durable_storage) == before
    assert asyncio.run(durable_storage.runtime.list(rejected_thread)) == []
    assert asyncio.run(durable_storage.runtime.lookup_submission(
        rejected_thread, "request", "hello"
    )) is None
    # An explicitly rejected key remains available when capacity is relaxed.
    accepted, created = _submission(durable_storage, rejected_thread)
    assert created and accepted.thread_id == rejected_thread


@pytest.mark.parametrize("case", ["different-keys", "same-key", "conflicting-payload"])
def test_submission_race_uses_independent_database_connections(
    durable_storage, case, monkeypatch,
):
    from axiom.runtime.checkpoints import SQLiteCheckpointStore
    from axiom.runtime.postgres import PostgresRuntimeStore

    thread_id = durable_storage.events.create_thread()
    before = _submission_counts(durable_storage)
    barrier = threading.Barrier(2)
    if durable_storage.backend == "sqlite":
        stores = [SQLiteCheckpointStore(durable_storage.runtime.db_path) for _ in range(2)]
        for store in stores:
            original = store._connect

            def connect(original=original):
                conn = original()
                barrier.wait(timeout=10)
                return conn

            monkeypatch.setattr(store, "_connect", connect)
    else:
        class GatedPool:
            @contextmanager
            def connection(self):
                with durable_storage.runtime.pool.connection() as conn:
                    barrier.wait(timeout=10)
                    yield conn

        stores = [PostgresRuntimeStore(GatedPool()) for _ in range(2)]

    def submit(index):
        text = f"message {index}" if case == "conflicting-payload" else "hello"
        try:
            return asyncio.run(stores[index].admit_submission(
                Checkpoint.create(thread_id=thread_id, input=text),
                idempotency_key=f"key-{index}" if case == "different-keys" else "same",
                request_fingerprint=text,
            ))
        except ApiError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, range(2)))
    accepted = [result for result in results if isinstance(result, tuple)]
    assert sum(created for _, created in accepted) == 1
    if case == "same-key":
        assert len(accepted) == 2
        assert accepted[0][0].run_id == accepted[1][0].run_id
    else:
        errors = [result for result in results if isinstance(result, ApiError)]
        expected = "THREAD_BUSY" if case == "different-keys" else "IDEMPOTENCY_KEY_CONFLICT"
        assert len(errors) == 1 and errors[0].code == expected
        if case == "different-keys":
            assert errors[0].details["active_run_id"] == accepted[0][0].run_id
    assert len(asyncio.run(durable_storage.runtime.list(thread_id))) == 1
    events = durable_storage.events.list_events(thread_id)
    assert [event.type for event in events].count("user.message") == 1
    assert [event.type for event in events].count("turn.started") == 1
    increments = {"checkpoints": 1, "submission_idempotency": 1, "events": 3,
                  "traffic_rate_buckets": 0, "runs": 1}
    assert _submission_counts(durable_storage) == {
        table: count + increments[table] for table, count in before.items()
    }


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.INTERRUPTED,
                                   RunStatus.WAITING_APPROVAL, RunStatus.WAITING_CHILD])
def test_occupied_thread_rejects_new_key_without_writing(durable_storage, status):
    thread_id = durable_storage.events.create_thread()
    state, _ = _submission(durable_storage, thread_id)
    state.status = status
    asyncio.run(durable_storage.runtime.save(state))
    before = _submission_counts(durable_storage)
    with pytest.raises(ApiError) as error:
        _submission(durable_storage, thread_id, key="new", max_queued_runs=1)
    assert error.value.code == "THREAD_BUSY"
    assert error.value.details["active_run_id"] == state.run_id
    assert _submission_counts(durable_storage) == before
    replay, created = _submission(durable_storage, thread_id, max_queued_runs=1)
    assert replay.run_id == state.run_id and not created
    assert replay.status == status


def test_submission_replay_precedes_capacity_and_rate_and_conflict_is_precise(durable_storage):
    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    traffic = TrafficGovernanceConfig(global_submission_rate=0.001, global_submission_burst=1)
    state, _ = _submission(durable_storage, thread_id, traffic=traffic)
    before = _submission_counts(durable_storage)
    replay, created = _submission(durable_storage, thread_id, traffic=traffic, max_queued_runs=1)
    assert not created and replay.run_id == state.run_id
    with pytest.raises(ApiError) as conflict:
        _submission(durable_storage, thread_id, text="different", traffic=traffic)
    assert conflict.value.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert _submission_counts(durable_storage) == before
    other_thread = durable_storage.events.create_thread()
    with pytest.raises(ApiError) as limited:
        _submission(durable_storage, other_thread, traffic=traffic)
    assert limited.value.code == "RATE_LIMITED"
    assert asyncio.run(store.list(other_thread)) == []
    state.status = RunStatus.COMPLETED
    state.output_text = "answer"
    asyncio.run(store.save(state))
    _submission(durable_storage, other_thread)
    replay, created = _submission(durable_storage, thread_id, traffic=traffic, max_queued_runs=1)
    assert not created and replay.run_id == state.run_id and replay.status == RunStatus.COMPLETED


def test_completion_projection_repairs_crash_gap_and_history_is_authoritative(
    durable_storage, monkeypatch,
):
    from axiom.runtime import postgres

    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    state, _ = _submission(durable_storage, thread_id)
    state.status = RunStatus.COMPLETED
    state.output_text = "persisted answer"
    target, attribute = ((store, "_project_completed_in_transaction")
                         if durable_storage.backend == "sqlite"
                         else (postgres, "_project_completed_in_transaction"))
    with monkeypatch.context() as crash_gap:
        crash_gap.setattr(target, attribute, lambda *_args: (None, False))
        asyncio.run(store.save(state))
    child = Checkpoint.create(thread_id=thread_id, input="child input", parent_run_id=state.run_id)
    child.status = RunStatus.COMPLETED
    child.output_text = "child output"
    asyncio.run(store.save(child))
    assert not any(event.type == "assistant.message"
                   for event in durable_storage.events.list_events(thread_id))
    next_state, _ = _submission(durable_storage, thread_id, key="next", text="next input")
    assert [(message.role, message.content) for message in next_state.messages] == [
        ("user", "hello"), ("assistant", "persisted answer"), ("user", "next input")
    ]
    event_id, created = asyncio.run(store.project_completed_turn(state))
    assert event_id is not None and not created
    assert asyncio.run(store.project_completed_turn(state)) == (event_id, False)
    projected = [event for event in durable_storage.events.list_events(thread_id)
                 if event.type in {"assistant.message", "turn.completed"}]
    assert [event.type for event in projected] == ["assistant.message", "turn.completed"]
    assert {(event.run_id, event.turn_id) for event in projected} == {(state.run_id, state.turn_id)}


def test_terminal_save_projects_messages_in_its_transaction(durable_storage, monkeypatch):
    from axiom.runtime import postgres

    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    state, _ = _submission(durable_storage, thread_id)
    target, attribute = ((store, "_append_event") if durable_storage.backend == "sqlite"
                         else (postgres, "_append_submission_event"))
    original = getattr(target, attribute)

    def fail(conn, state, event_type, payload):
        event_id = original(conn, state, event_type, payload)
        if event_type == "turn.completed":
            raise RuntimeError("terminal projection crash")
        return event_id

    state.status = RunStatus.COMPLETED
    state.output_text = "answer"
    before = _submission_counts(durable_storage)
    with monkeypatch.context() as crash:
        crash.setattr(target, attribute, fail)
        with pytest.raises(RuntimeError, match="terminal projection crash"):
            asyncio.run(store.save(state))
    assert asyncio.run(store.load(state.run_id)).status == RunStatus.RUNNING
    assert _submission_counts(durable_storage) == before
    asyncio.run(store.save(state))
    assert asyncio.run(store.project_completed_turn(state))[0] is not None


@pytest.mark.parametrize("existing_type", ["assistant.message", "turn.completed"])
def test_projection_repairs_either_half_without_duplicate_events(durable_storage, monkeypatch,
                                                               existing_type):
    from axiom.runtime import postgres

    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    state, _ = _submission(durable_storage, thread_id)
    state.status = RunStatus.COMPLETED
    state.output_text = "durable answer"
    target, attribute = ((store, "_project_completed_in_transaction")
                         if durable_storage.backend == "sqlite"
                         else (postgres, "_project_completed_in_transaction"))
    with monkeypatch.context() as gap:
        gap.setattr(target, attribute, lambda *_args: (None, False))
        asyncio.run(store.save(state))
    durable_storage.events.append_event(thread_id, existing_type, {
        "thread_id": thread_id, "run_id": state.run_id, "turn_id": state.turn_id,
        "text": state.output_text, "status": state.status.value,
    })
    event_id, created = asyncio.run(store.project_completed_turn(state))
    assert event_id is not None
    assert created is (existing_type == "turn.completed")
    assert asyncio.run(store.project_completed_turn(state)) == (event_id, False)
    events = durable_storage.events.list_events(thread_id)
    assert sum(event.type == "assistant.message" for event in events) == 1
    assert sum(event.type == "turn.completed" for event in events) == 1


def test_child_runs_do_not_take_another_thread_slot(durable_storage):
    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    parent, _ = _submission(durable_storage, thread_id)
    parent.status = RunStatus.WAITING_CHILD
    asyncio.run(store.save(parent))
    children = [Checkpoint.create(thread_id=thread_id, input=f"child {index}",
                                  parent_run_id=parent.run_id) for index in range(2)]
    for child in children:
        asyncio.run(store.save(child))
    assert len(asyncio.run(store.list(thread_id))) == 3
    other_thread = durable_storage.events.create_thread()
    accepted, created = _submission(durable_storage, other_thread)
    assert created and accepted.thread_id == other_thread


def test_old_root_reactivation_cannot_overlap_new_root(durable_storage):
    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    old, _ = _submission(durable_storage, thread_id)
    old.status = RunStatus.FAILED
    asyncio.run(store.save(old))
    active, _ = _submission(durable_storage, thread_id, key="new")
    before = _submission_counts(durable_storage)
    old.status = RunStatus.RUNNING
    with pytest.raises(ApiError) as conflict:
        asyncio.run(store.save(old))
    assert conflict.value.code == "THREAD_BUSY"
    assert conflict.value.details["active_run_id"] == active.run_id
    assert _submission_counts(durable_storage) == before


def test_schema_upgrade_preserves_existing_conflicting_roots(durable_storage):
    from axiom.runtime.checkpoints import SQLiteCheckpointStore
    from axiom.runtime.postgres import _insert_initial_run, initialize_postgres_schema

    store = durable_storage.runtime
    thread_id = durable_storage.events.create_thread()
    legacy_roots = [Checkpoint.create(thread_id=thread_id, input=f"legacy {index}")
                    for index in range(2)]
    connection = (
        store._connect() if durable_storage.backend == "sqlite" else store.pool.connection()
    )
    with connection as conn:
        if durable_storage.backend == "sqlite":
            # Simulate the pre-admission SQLite schema while retaining its Run history.
            conn.execute("drop table submission_idempotency")
            conn.execute("drop table traffic_rate_buckets")
        for state in legacy_roots:
            if durable_storage.backend == "sqlite":
                store._write_checkpoint(
                    conn, state, sequence=1, updated_at=state.updated_at
                )
            else:
                _insert_initial_run(conn, state, updated_at=state.updated_at, runnable=True)
    before = [state.to_dict() for state in asyncio.run(store.list(thread_id))]
    if durable_storage.backend == "sqlite":
        SQLiteCheckpointStore(store.db_path)
    else:
        initialize_postgres_schema(store.pool)
    assert [state.to_dict() for state in asyncio.run(store.list(thread_id))] == before
    with pytest.raises(ApiError) as conflict:
        _submission(durable_storage, thread_id)
    assert conflict.value.code == "THREAD_BUSY"
    assert [state.to_dict() for state in asyncio.run(store.list(thread_id))] == before
    if durable_storage.backend == "postgres":
        # Neither claim entry point can run conflicting historical roots together.
        for claim in (lambda: store.claim_run(legacy_roots[0].run_id, "worker", 30),
                      lambda: store.claim_next("worker", 30)):
            with pytest.raises(ApiError) as blocked:
                asyncio.run(claim())
            assert blocked.value.code == "THREAD_BUSY"


@pytest.mark.postgres
def test_postgres_v8_read_indexes_upgrade_preserves_history_and_replay(postgres_storage):
    from axiom.runtime.postgres import POSTGRES_SCHEMA_VERSION, initialize_postgres_schema

    storage = postgres_storage
    thread_id = storage.events.create_thread()
    root = _completed_submission(storage, thread_id, 0)
    child = Checkpoint.create(thread_id=thread_id, input="child", parent_run_id=root.run_id)
    child.status = RunStatus.COMPLETED
    asyncio.run(storage.runtime.save(child))
    pending, _ = _submission(storage, storage.events.create_thread(), key="pending")
    before = _submission_counts(storage)
    history = [state.to_dict() for state in asyncio.run(storage.runtime.list(thread_id))]
    events = storage.events.list_events(thread_id)
    indexes = {"idx_runs_created", "idx_runs_parent", "idx_events_messages",
               "idx_events_message_identity"}
    with storage.runtime.pool.connection() as conn:
        for index in sorted(indexes):
            conn.execute(f"drop index {index}")
        conn.execute("update axiom_schema_versions set version = 8 where component = 'runtime'")

    initialize_postgres_schema(storage.runtime.pool)
    initialize_postgres_schema(storage.runtime.pool)

    with storage.runtime.pool.connection() as conn:
        assert conn.execute(
            "select version from axiom_schema_versions where component = 'runtime'"
        ).fetchone()[0] == POSTGRES_SCHEMA_VERSION
        actual = {row[0] for row in conn.execute(
            "select indexname from pg_indexes where schemaname = current_schema()"
        ).fetchall()}
    assert indexes <= actual
    assert _submission_counts(storage) == before
    assert [state.to_dict() for state in asyncio.run(storage.runtime.list(thread_id))] == history
    assert storage.events.list_events(thread_id) == events
    messages, _ = storage.events.page_messages(thread_id)
    assert [event.payload["text"] for event in messages] == ["question 0", "answer 0"]
    replay, created = _submission(storage, thread_id, key="page-0", text="question 0")
    assert not created and replay.run_id == root.run_id and replay.status == RunStatus.COMPLETED
    replay, created = _submission(storage, pending.thread_id, key="pending")
    assert not created and replay.run_id == pending.run_id
    summary = asyncio.run(storage.runtime.list_run_summaries(search=root.run_id))
    assert len(summary) == 1 and summary[0]["children_count"] == 1
    assert _submission_counts(storage) == before


@pytest.mark.postgres
def test_delivery_requeue_cannot_reactivate_old_root_over_new_submission(postgres_storage):
    store = postgres_storage.runtime
    thread_id = postgres_storage.events.create_thread()
    old = Checkpoint.create(thread_id=thread_id, input="old")
    old.status = RunStatus.FAILED
    old.error = RunError(type="RUN_DELIVERY_EXHAUSTED", message="delivery exhausted")
    old.failure_queued_at = datetime.now(UTC).isoformat()
    asyncio.run(store.save(old))
    active, _ = _submission(postgres_storage, thread_id)
    before = _submission_counts(postgres_storage)
    with pytest.raises(ApiError) as blocked:
        asyncio.run(store.requeue_delivery_exhausted(old.run_id))
    assert blocked.value.code == "THREAD_BUSY"
    assert blocked.value.details["active_run_id"] == active.run_id
    assert _submission_counts(postgres_storage) == before
    assert asyncio.run(store.load(old.run_id)).status == RunStatus.FAILED


def test_artifact_metadata_store_contract(durable_storage):
    blob = durable_storage.artifacts.upsert_blob(
        ArtifactBlob("a" * 64, f"blobs/sha256/aa/{'a' * 64}", 4, "test")
    )
    artifact = durable_storage.artifacts.create_artifact(
        ArtifactRecord(
            artifact_id="art-contract",
            blob_sha256=blob.sha256,
            run_id="run-artifact",
            thread_id="thread-artifact",
            invocation_id="inv-artifact",
            tool_name="publish_artifact",
            name="result.txt",
            media_type="text/plain",
            size_bytes=4,
        )
    )
    reuse = durable_storage.artifacts.put_reuse_entry(
        ArtifactReuseEntry("reuse-key", artifact.artifact_id, "test", "v1")
    )

    assert durable_storage.artifacts.get_blob(blob.sha256) == blob
    assert durable_storage.artifacts.get_artifact(artifact.artifact_id) == artifact
    assert durable_storage.artifacts.list_run_artifacts("run-artifact") == [artifact]
    assert durable_storage.artifacts.get_reuse_entry("reuse-key") == reuse


def test_provenance_store_contract(durable_storage):
    claim = ClaimRecord(
        claim_id="clm-contract",
        invocation_id="run-contract:claim",
        run_id="run-contract",
        thread_id="thread-contract",
        turn_id="turn-contract",
        text="contract claim",
    )
    evidence = EvidenceRecord(
        evidence_id="evd-contract",
        run_id=claim.run_id,
        thread_id=claim.thread_id,
        turn_id=claim.turn_id,
        source_type=EvidenceSourceType.CODE_LOCATION,
        source_id="src/example.py:1-1",
        source_run_id=claim.run_id,
        summary="src/example.py:1-1",
        locator={"path": "src/example.py", "start_line": 1, "end_line": 1},
        source_digest="a" * 64,
    )
    link = ClaimEvidenceLink(claim.claim_id, evidence.evidence_id)

    stored = durable_storage.provenance.create_bundle(claim, [evidence], [link])

    assert stored == claim
    assert durable_storage.provenance.get_claim(claim.claim_id) == claim
    assert durable_storage.provenance.get_claim_by_invocation(claim.invocation_id) == claim
    assert durable_storage.provenance.list_run_claims(claim.run_id) == [claim]
    assert durable_storage.provenance.list_claim_evidence(claim.claim_id) == [(link, evidence)]


@pytest.mark.postgres
def test_postgres_metadata_round_trips_in_non_utc_session(postgres_storage):
    from axiom.artifacts.metadata import PostgresArtifactMetadataStore
    from axiom.provenance.store import PostgresProvenanceStore

    class NonUtcPool:
        @contextmanager
        def connection(self):
            with postgres_storage.runtime.pool.connection() as conn:
                conn.execute("set local time zone 'Pacific/Honolulu'")
                yield conn

    artifacts = PostgresArtifactMetadataStore(NonUtcPool())
    blob = ArtifactBlob("c" * 64, "blobs/non-utc", 4, "s3")
    assert artifacts.upsert_blob(blob) == blob
    assert artifacts.upsert_blob(blob) == blob
    assert artifacts.get_blob(blob.sha256) == blob
    artifact = ArtifactRecord(
        artifact_id="art-non-utc", blob_sha256=blob.sha256, run_id="run-non-utc",
        thread_id="thread-non-utc", invocation_id="inv-non-utc", tool_name="publish_artifact",
        name="result.txt", media_type="text/plain", size_bytes=4,
    )
    assert artifacts.create_artifact(artifact) == artifact
    assert artifacts.get_artifact(artifact.artifact_id) == artifact
    reuse = ArtifactReuseEntry("non-utc", artifact.artifact_id, "test", "v1")
    artifacts.put_reuse_entry(reuse)
    assert artifacts.get_reuse_entry(reuse.reuse_key) == reuse

    provenance = PostgresProvenanceStore(NonUtcPool())
    claim = ClaimRecord(
        claim_id="claim-non-utc", invocation_id="claim-inv-non-utc", run_id=artifact.run_id,
        thread_id=artifact.thread_id, turn_id="turn-non-utc", text="round trip",
    )
    evidence = EvidenceRecord(
        evidence_id="evidence-non-utc", run_id=claim.run_id, thread_id=claim.thread_id,
        turn_id=claim.turn_id, source_type=EvidenceSourceType.CODE_LOCATION,
        source_id="src/example.py:1", source_run_id=claim.run_id, summary="example",
        locator={"path": "src/example.py", "start_line": 1, "end_line": 1},
    )
    link = ClaimEvidenceLink(claim.claim_id, evidence.evidence_id)
    provenance.create_bundle(claim, [evidence], [link])
    assert provenance.get_claim(claim.claim_id) == claim
    assert provenance.list_claim_evidence(claim.claim_id) == [(link, evidence)]


def test_storage_configuration_reads_environment_without_changing_defaults(tmp_path):
    config = load_config(
        project_root=tmp_path,
        env={
            "AXIOM_STORAGE_BACKEND": "postgres",
            "AXIOM_POSTGRES_DSN": "postgresql://example.invalid/db",
            "AXIOM_POSTGRES_POOL_MAX_SIZE": "7",
        },
    )
    assert config.storage.backend == "postgres"
    assert config.storage.pool_min_size == 1
    assert config.storage.pool_max_size == 7


def test_postgres_connection_failure_is_visible_and_never_falls_back(tmp_path, monkeypatch):
    from axiom.runtime import postgres

    class FailingPoolModule:
        class ConnectionPool:
            def __init__(self, **_kwargs):
                raise OSError("connection refused")

    monkeypatch.setattr(
        postgres.importlib,
        "import_module",
        lambda _name: FailingPoolModule,
    )

    with pytest.raises(postgres.PostgresUnavailableError, match="durable store is unavailable"):
        create_durable_storage(
            StorageConfig(
                backend="postgres",
                postgres_dsn="postgresql://invalid@127.0.0.1:1/invalid",
                pool_min_size=1,
                pool_max_size=1,
                connect_timeout_seconds=0.2,
            ),
            default_sqlite_path=tmp_path / "must-not-exist.db",
        )
    assert not (tmp_path / "must-not-exist.db").exists()


def test_unknown_storage_backend_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported storage backend"):
        create_durable_storage(
            StorageConfig(backend="redis"),
            default_sqlite_path=tmp_path / "must-not-exist.db",
        )
    assert not (tmp_path / "must-not-exist.db").exists()


def test_backend_identity_is_reported(durable_storage):
    assert durable_storage.backend in {"sqlite", "postgres"}
    assert durable_storage.runtime.backend == durable_storage.backend
    assert durable_storage.events.backend == durable_storage.backend
    assert durable_storage.controls.backend == durable_storage.backend
    assert durable_storage.provenance.backend == durable_storage.backend


def test_ephemeral_runtime_contracts_add_no_durable_tables(durable_storage):
    forbidden = (
        "steps",
        "contexts",
        "context_snapshots",
        "completion_decisions",
        "completion_policy",
        "next_actions",
        "step_results",
        "step_transitions",
        "runtime_loops",
        "step_views",
        "step_metrics",
        "evaluation_steps",
    )
    if durable_storage.backend == "sqlite":
        with durable_storage.runtime._connect() as conn:
            rows = conn.execute(
                "select name from sqlite_master where type = 'table' "
                "and name in "
                "('steps', 'contexts', 'context_snapshots', "
                "'completion_decisions', 'completion_policy', 'next_actions', "
                "'step_results', 'step_transitions', 'runtime_loops', "
                "'step_views', 'step_metrics', 'evaluation_steps')"
            ).fetchall()
    else:
        with durable_storage.runtime.pool.connection() as conn:
            rows = [
                row
                for name in forbidden
                if (row := conn.execute("select to_regclass(%s)", (name,)).fetchone())
                and row[0] is not None
            ]
    assert rows == []


def test_checkpoint_create_load_and_list_contract(durable_storage):
    state = Checkpoint.create(thread_id="thread-create", run_id="run-create", input="work")
    asyncio.run(durable_storage.runtime.save(state))

    loaded = asyncio.run(durable_storage.runtime.load(state.run_id))
    listed = asyncio.run(durable_storage.runtime.list(state.thread_id))

    assert loaded is not None and loaded.to_dict() == state.to_dict()
    assert [item.run_id for item in listed] == [state.run_id]
    assert state.sequence == 1


def test_checkpoint_sequence_advances_and_stale_write_is_rejected(durable_storage):
    state = Checkpoint.create(thread_id="thread-cas", run_id="run-cas", input="work")
    asyncio.run(durable_storage.runtime.save(state))
    stale = deepcopy(state)
    state.output_text = "winner"
    asyncio.run(durable_storage.runtime.save(state))

    stale.output_text = "stale"
    with pytest.raises(CheckpointConflictError):
        asyncio.run(durable_storage.runtime.save(stale))

    final = asyncio.run(durable_storage.runtime.load(state.run_id))
    assert final is not None
    assert final.sequence == 2
    assert final.output_text == "winner"


def test_parent_child_lineage_round_trips(durable_storage):
    parent = Checkpoint.create(thread_id="thread-lineage", run_id="parent", input="parent")
    child = Checkpoint.create(
        thread_id=parent.thread_id,
        run_id="child",
        input="child",
        parent_run_id=parent.run_id,
        parent_step_id="step-1",
    )
    asyncio.run(durable_storage.runtime.save(parent))
    asyncio.run(durable_storage.runtime.save(child))

    loaded = asyncio.run(durable_storage.runtime.load(child.run_id))
    assert loaded is not None
    assert loaded.parent_run_id == parent.run_id
    assert loaded.parent_step_id == "step-1"


def test_succeeded_tool_execution_round_trips_reuse_evidence(durable_storage):
    record = _tool_record(
        invocation_id="invocation-success",
        status=ToolExecutionStatus.SUCCEEDED,
        result="durable result",
        attempt=2,
    )
    asyncio.run(durable_storage.runtime.save_tool_execution(record))

    restored = asyncio.run(durable_storage.runtime.load_tool_execution(record.invocation_id))
    assert restored is not None
    assert restored.status == ToolExecutionStatus.SUCCEEDED
    assert restored.result == "durable result"
    assert restored.attempt == 2


def test_unknown_retry_suppression_round_trips(durable_storage):
    record = _tool_record(
        invocation_id="invocation-unknown",
        status=ToolExecutionStatus.UNKNOWN,
        attempt=1,
        retry_state=ToolRetryState.RETRY_SUPPRESSED,
        retry_suppressed_reason="unsafe_unknown_outcome",
    )
    record.last_failure_category = "transient"
    record.last_error_code = "dependency_timeout"
    asyncio.run(durable_storage.runtime.save_tool_execution(record))

    restored = asyncio.run(durable_storage.runtime.load_tool_execution(record.invocation_id))
    assert restored is not None
    assert restored.status == ToolExecutionStatus.UNKNOWN
    assert restored.retry_state == ToolRetryState.RETRY_SUPPRESSED
    assert restored.retry_suppressed_reason == "unsafe_unknown_outcome"
    assert restored.last_failure_category == "transient"
    assert restored.last_error_code == "dependency_timeout"


def test_retry_pending_deadline_round_trips_as_utc_instant(durable_storage):
    deadline = datetime.now(UTC).replace(microsecond=123456) + timedelta(minutes=1)
    record = _tool_record(
        invocation_id="invocation-retry",
        status=ToolExecutionStatus.FAILED,
        attempt=2,
        retry_state=ToolRetryState.RETRY_PENDING,
    )
    record.next_retry_at = deadline.isoformat()
    record.retry_backoff_seconds = 1.75
    asyncio.run(durable_storage.runtime.save_tool_execution(record))

    restored = asyncio.run(durable_storage.runtime.load_tool_execution(record.invocation_id))
    assert restored is not None and restored.next_retry_at is not None
    assert datetime.fromisoformat(restored.next_retry_at) == deadline
    assert restored.retry_backoff_seconds == 1.75


def test_tool_invocation_identity_rejects_different_arguments(durable_storage):
    original = _tool_record(invocation_id="invocation-hash")
    asyncio.run(durable_storage.runtime.save_tool_execution(original))
    conflict = _tool_record(invocation_id=original.invocation_id)
    conflict.arguments_hash = "different"

    with pytest.raises(ValueError, match="different tool arguments"):
        asyncio.run(durable_storage.runtime.save_tool_execution(conflict))


def test_tool_execution_list_is_scoped_and_ordered(durable_storage):
    second = _tool_record(invocation_id="invocation-b", run_id="run-tools")
    first = _tool_record(invocation_id="invocation-a", run_id="run-tools")
    other = _tool_record(invocation_id="invocation-other", run_id="other-run")
    for record in (second, first, other):
        asyncio.run(durable_storage.runtime.save_tool_execution(record))

    records = asyncio.run(durable_storage.runtime.list_tool_executions("run-tools"))
    assert [record.invocation_id for record in records] == ["invocation-a", "invocation-b"]


def test_budget_ledger_compare_and_swap_contract(durable_storage):
    ledger = BudgetLedgerRecord(owner_run_id="budget-owner", version=0, state={"steps": 1})
    asyncio.run(durable_storage.runtime.save_budget_ledger(ledger))
    stale = BudgetLedgerRecord(owner_run_id=ledger.owner_run_id, version=0, state={"steps": 2})

    with pytest.raises(BudgetLedgerConflictError):
        asyncio.run(durable_storage.runtime.save_budget_ledger(stale))

    restored = asyncio.run(durable_storage.runtime.load_budget_ledger(ledger.owner_run_id))
    assert restored is not None
    assert restored.version == 1
    assert restored.state == {"steps": 1}


def test_event_ids_order_replay_and_run_filter_contract(durable_storage):
    thread_id = durable_storage.events.create_thread()
    first_id = durable_storage.events.append_event(
        thread_id, "run.started", {"run_id": "run-events", "value": 1}
    )
    second_id = durable_storage.events.append_event(
        thread_id, "run.completed", {"run_id": "run-events", "value": 2}
    )
    durable_storage.events.append_event(
        thread_id, "run.started", {"run_id": "other-run", "value": 3}
    )

    replay = durable_storage.events.list_events(thread_id, after_id=first_id, run_id="run-events")
    assert second_id > first_id
    assert [event.id for event in replay] == [second_id]
    assert replay[0].payload["value"] == 2


def test_control_operation_idempotency_contract(durable_storage):
    request = {"decision": "approve", "invocation_id": "invocation-control"}
    first, created = durable_storage.controls.begin(
        run_id="run-control",
        idempotency_key="key-control",
        operation=ControlOperationName.APPROVE,
        request=request,
    )
    replay, replay_created = durable_storage.controls.begin(
        run_id="run-control",
        idempotency_key="key-control",
        operation=ControlOperationName.APPROVE,
        request=request,
    )
    completed = durable_storage.controls.complete(first.operation_id, {"status": "ok"})

    assert created is True
    assert replay_created is False
    assert replay.operation_id == first.operation_id
    assert completed.result == {"status": "ok"}


def test_control_operation_rejects_idempotency_key_reuse(durable_storage):
    durable_storage.controls.begin(
        run_id="run-control-conflict",
        idempotency_key="same-key",
        operation=ControlOperationName.RESUME,
        request={},
    )
    with pytest.raises(ApiError) as raised:
        durable_storage.controls.begin(
            run_id="run-control-conflict",
            idempotency_key="same-key",
            operation=ControlOperationName.CANCEL,
            request={},
        )
    assert raised.value.code == "idempotency_key_conflict"


def test_cancelled_ancestor_reconciliation_contract(durable_storage, tmp_path):
    parent = Checkpoint.create(
        thread_id="thread-ancestor", run_id="parent-cancelled", input="parent"
    )
    parent.status = RunStatus.CANCELLED
    child = Checkpoint.create(
        thread_id=parent.thread_id,
        run_id="child-stale",
        input="child",
        parent_run_id=parent.run_id,
    )
    child.status = RunStatus.INTERRUPTED
    child.interrupt = Interrupt(kind="manual", reason="pause")
    asyncio.run(durable_storage.runtime.save(parent))
    asyncio.run(durable_storage.runtime.save(child))
    config = AxiomConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    runtime = DurableAgentRuntime(
        llm_client=_UnexpectedLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=config,
        store=durable_storage.runtime,
    )

    reconciled = asyncio.run(runtime.resume(child.run_id))
    persisted = asyncio.run(durable_storage.runtime.load(child.run_id))
    assert reconciled.status == RunStatus.CANCELLED
    assert persisted is not None and persisted.status == RunStatus.CANCELLED


@pytest.mark.postgres
def test_postgres_concurrent_checkpoint_cas_has_one_winner(postgres_storage):
    durable_storage = postgres_storage
    state = Checkpoint.create(
        thread_id="thread-concurrent-cas", run_id="run-concurrent-cas", input="work"
    )
    asyncio.run(durable_storage.runtime.save(state))
    writer_a = deepcopy(state)
    writer_b = deepcopy(state)
    writer_a.output_text = "writer-a"
    writer_b.output_text = "writer-b"

    async def race():
        return await asyncio.gather(
            durable_storage.runtime.save(writer_a),
            durable_storage.runtime.save(writer_b),
            return_exceptions=True,
        )

    outcomes = asyncio.run(race())
    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, CheckpointConflictError) for outcome in outcomes) == 1
    final = asyncio.run(durable_storage.runtime.load(state.run_id))
    assert final is not None
    assert final.sequence == 2
    assert final.output_text in {"writer-a", "writer-b"}


@pytest.mark.postgres
def test_postgres_concurrent_artifact_blob_upsert_is_safe(postgres_storage):
    blob = ArtifactBlob("b" * 64, f"blobs/sha256/bb/{'b' * 64}", 8, "s3")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _index: postgres_storage.artifacts.upsert_blob(blob), range(2))
        )
    assert results == [blob, blob]
    pool = postgres_storage._close
    with pool.connection() as conn:
        count = conn.execute(
            "select count(*) from artifact_blobs where sha256 = %s",
            (blob.sha256,),
        ).fetchone()[0]
    assert count == 1


@pytest.mark.postgres
def test_postgres_concurrent_child_claims_are_queryable(postgres_storage):
    def create(index: int) -> ClaimRecord:
        claim = ClaimRecord(
            claim_id=f"clm-child-{index}",
            invocation_id=f"child-{index}:claim",
            run_id="run-parent",
            thread_id="thread-claims",
            turn_id="turn-claims",
            text=f"child claim {index}",
        )
        evidence = EvidenceRecord(
            evidence_id=f"evd-child-{index}",
            run_id=claim.run_id,
            thread_id=claim.thread_id,
            turn_id=claim.turn_id,
            source_type=EvidenceSourceType.CODE_LOCATION,
            source_id=f"src/child_{index}.py:1-1",
            source_run_id=f"run-child-{index}",
            summary=f"child {index}",
            locator={"path": f"src/child_{index}.py", "start_line": 1, "end_line": 1},
        )
        link = ClaimEvidenceLink(claim.claim_id, evidence.evidence_id)
        return postgres_storage.provenance.create_bundle(claim, [evidence], [link])

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(create, range(2)))

    assert {claim.claim_id for claim in claims} == {"clm-child-0", "clm-child-1"}
    assert len(postgres_storage.provenance.list_run_claims("run-parent")) == 2


@pytest.mark.postgres
def test_postgres_concurrent_tool_insert_keeps_one_durable_row(postgres_storage):
    durable_storage = postgres_storage
    first = _tool_record(invocation_id="invocation-concurrent")
    second = deepcopy(first)
    first.result = "first"
    second.result = "second"

    async def race():
        await asyncio.gather(
            durable_storage.runtime.save_tool_execution(first),
            durable_storage.runtime.save_tool_execution(second),
        )

    asyncio.run(race())
    pool = durable_storage._close
    with pool.connection() as conn:
        count = conn.execute(
            "select count(*) from tool_executions where invocation_id = %s",
            (first.invocation_id,),
        ).fetchone()[0]
    assert count == 1


@pytest.mark.postgres
def test_postgres_rejects_future_schema_version(postgres_storage):
    durable_storage = postgres_storage
    from axiom.runtime.postgres import PostgresSchemaError, initialize_postgres_schema

    pool = durable_storage._close
    with pool.connection() as conn:
        conn.execute("update axiom_schema_versions set version = 999 where component = 'runtime'")
    with pytest.raises(PostgresSchemaError, match="unsupported PostgreSQL runtime schema"):
        initialize_postgres_schema(pool)


def _tool_record(
    *,
    invocation_id: str,
    run_id: str = "run-tool",
    status: ToolExecutionStatus = ToolExecutionStatus.PENDING,
    result: str | None = None,
    attempt: int = 0,
    retry_state: ToolRetryState = ToolRetryState.NONE,
    retry_suppressed_reason: str | None = None,
) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        invocation_id=invocation_id,
        run_id=run_id,
        tool_call_id="tool-call",
        tool_name="write_tool",
        arguments_hash="arguments-hash",
        status=status,
        result=result,
        attempt=attempt,
        retry_state=retry_state,
        retry_suppressed_reason=retry_suppressed_reason,
    )


class _UnexpectedLlm:
    provider_name = "storage-test"
    model_name = "unexpected"
    max_context_window = 10_000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        raise AssertionError("ancestor reconciliation must not invoke the model")
        yield
