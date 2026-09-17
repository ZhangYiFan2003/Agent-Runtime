from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from axiom.config import AxiomConfig, StorageConfig, config_to_public_dict, load_config
from axiom.runtime.checkpoints import BudgetLedgerConflictError, CheckpointConflictError
from axiom.runtime.control_plane import ApiError, ControlOperationName
from axiom.runtime.durable import DurableAgentRuntime
from axiom.runtime.models import (
    BudgetLedgerRecord,
    Checkpoint,
    Interrupt,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolRetryState,
)
from axiom.runtime.storage import DurableStorage, create_durable_storage
from axiom.tools import ToolRegistry


@pytest.fixture(params=("sqlite", "postgres"))
def durable_storage(request, tmp_path) -> Iterator[DurableStorage]:
    if request.param == "sqlite":
        storage = create_durable_storage(
            StorageConfig(), default_sqlite_path=tmp_path / "runtime.db"
        )
        yield storage
        storage.close()
        return

    dsn = os.environ.get("AXIOM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AXIOM_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    conninfo = pytest.importorskip("psycopg.conninfo")
    schema = f"axiom_test_{uuid4().hex}"
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


def test_storage_configuration_defaults_to_sqlite_and_redacts_dsn(tmp_path):
    config = load_config(project_root=tmp_path, env={})
    assert config.storage.backend == "sqlite"

    config.storage.postgres_dsn = "postgresql://user:secret@example.invalid/db"
    assert config_to_public_dict(config)["storage"]["postgres_dsn"] == "***"


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


def test_postgres_connection_failure_is_visible_and_never_falls_back(
    tmp_path, monkeypatch
):
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

    with pytest.raises(
        postgres.PostgresUnavailableError, match="durable store is unavailable"
    ):
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


def test_ephemeral_runtime_contracts_add_no_durable_tables(durable_storage):
    forbidden = ("steps", "contexts", "context_snapshots")
    if durable_storage.backend == "sqlite":
        with durable_storage.runtime._connect() as conn:
            rows = conn.execute(
                "select name from sqlite_master where type = 'table' "
                "and name in ('steps', 'contexts', 'context_snapshots')"
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

    restored = asyncio.run(
        durable_storage.runtime.load_tool_execution(record.invocation_id)
    )
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

    restored = asyncio.run(
        durable_storage.runtime.load_tool_execution(record.invocation_id)
    )
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

    restored = asyncio.run(
        durable_storage.runtime.load_tool_execution(record.invocation_id)
    )
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

    replay = durable_storage.events.list_events(
        thread_id, after_id=first_id, run_id="run-events"
    )
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
def test_postgres_concurrent_checkpoint_cas_has_one_winner(durable_storage):
    if durable_storage.backend != "postgres":
        pytest.skip("PostgreSQL-only concurrency contract")
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
def test_postgres_concurrent_tool_insert_keeps_one_durable_row(durable_storage):
    if durable_storage.backend != "postgres":
        pytest.skip("PostgreSQL-only concurrency contract")
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
def test_postgres_rejects_future_schema_version(durable_storage):
    if durable_storage.backend != "postgres":
        pytest.skip("PostgreSQL-only schema contract")
    from axiom.runtime.postgres import PostgresSchemaError, initialize_postgres_schema

    pool = durable_storage._close
    with pool.connection() as conn:
        conn.execute(
            "update axiom_schema_versions set version = 999 where component = 'runtime'"
        )
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
