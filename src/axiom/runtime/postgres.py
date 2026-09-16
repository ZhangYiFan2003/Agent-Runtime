from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from axiom.runtime.checkpoints import BudgetLedgerConflictError, CheckpointConflictError
from axiom.runtime.control_plane import (
    ApiError,
    ControlOperationName,
    ControlOperationRecord,
    ControlOperationStatus,
    operation_request_hash,
)
from axiom.runtime.events import RuntimeEvent
from axiom.runtime.models import BudgetLedgerRecord, Checkpoint, ToolExecutionRecord

POSTGRES_SCHEMA_VERSION = 1


class PostgresDependencyError(RuntimeError):
    """Raised when the optional PostgreSQL driver is not installed."""


class PostgresUnavailableError(RuntimeError):
    """Raised when the configured shared durable store cannot be reached."""


class PostgresSchemaError(RuntimeError):
    """Raised when the durable-store schema is incompatible with this code."""


class PostgresConnectionPool:
    """Small bounded psycopg pool shared by all PostgreSQL repositories."""

    backend = "postgres"

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        connect_timeout_seconds: float = 5.0,
    ) -> None:
        if not dsn.strip():
            raise ValueError("postgres_dsn is required when storage.backend is postgres")
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("invalid PostgreSQL pool size")
        try:
            pool_module = importlib.import_module("psycopg_pool")
        except ImportError as exc:
            raise PostgresDependencyError(
                "PostgreSQL storage requires the 'postgres' optional dependency"
            ) from exc
        pool = None
        try:
            pool = pool_module.ConnectionPool(
                conninfo=dsn,
                min_size=min_size,
                max_size=max_size,
                timeout=connect_timeout_seconds,
                kwargs={"connect_timeout": max(1, int(connect_timeout_seconds))},
                open=True,
            )
            pool.wait(timeout=connect_timeout_seconds)
        except Exception as exc:
            if pool is not None:
                pool.close()
            raise PostgresUnavailableError("PostgreSQL durable store is unavailable") from exc
        self._pool = pool

    def connection(self):
        return self._pool.connection()

    def close(self) -> None:
        self._pool.close()


def initialize_postgres_schema(pool: PostgresConnectionPool) -> None:
    """Create the v1 schema and reject stores created by newer code."""
    with pool.connection() as conn:
        conn.execute(
            """
            create table if not exists axiom_schema_versions (
                component text primary key,
                version integer not null,
                updated_at timestamptz not null
            )
            """
        )
        conn.execute(
            """
            insert into axiom_schema_versions(component, version, updated_at)
            values ('runtime', %s, %s)
            on conflict(component) do nothing
            """,
            (POSTGRES_SCHEMA_VERSION, _now()),
        )
        row = conn.execute(
            "select version from axiom_schema_versions where component = 'runtime'"
        ).fetchone()
        version = int(row[0]) if row else 0
        if version != POSTGRES_SCHEMA_VERSION:
            raise PostgresSchemaError(
                f"unsupported PostgreSQL runtime schema version: {version}"
            )
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)


class PostgresRuntimeStore:
    backend = "postgres"

    def __init__(self, pool: PostgresConnectionPool):
        self.pool = pool

    async def save(self, checkpoint: Checkpoint) -> None:
        await asyncio.to_thread(self._save, checkpoint)

    async def load(self, run_id: str) -> Checkpoint | None:
        return await asyncio.to_thread(self._load, run_id)

    async def list(self, thread_id: str) -> list[Checkpoint]:
        return await asyncio.to_thread(self._list, thread_id)

    async def save_tool_execution(self, record: ToolExecutionRecord) -> None:
        await asyncio.to_thread(self._save_tool_execution, record)

    async def load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None:
        return await asyncio.to_thread(self._load_tool_execution, invocation_id)

    async def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        return await asyncio.to_thread(self._list_tool_executions, run_id)

    async def load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None:
        return await asyncio.to_thread(self._load_budget_ledger, owner_run_id)

    async def save_budget_ledger(self, record: BudgetLedgerRecord) -> None:
        await asyncio.to_thread(self._save_budget_ledger, record)

    def _save(self, checkpoint: Checkpoint) -> None:
        expected = checkpoint.sequence
        next_sequence = expected + 1
        updated_at = _now()
        state = checkpoint.to_dict()
        state["sequence"] = next_sequence
        state["updated_at"] = updated_at
        payload = _json(state)
        with self.pool.connection() as conn:
            if expected == 0:
                row = conn.execute(
                    """
                    insert into runs(
                        run_id, thread_id, turn_id, status, current_sequence,
                        schema_version, state_json, parent_run_id, created_at, updated_at
                    ) values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    on conflict(run_id) do nothing
                    returning current_sequence
                    """,
                    (
                        checkpoint.run_id,
                        checkpoint.thread_id,
                        checkpoint.turn_id,
                        checkpoint.status.value,
                        next_sequence,
                        checkpoint.schema_version,
                        payload,
                        checkpoint.parent_run_id,
                        checkpoint.created_at,
                        updated_at,
                    ),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    update runs
                    set thread_id = %s, turn_id = %s, status = %s,
                        current_sequence = %s, schema_version = %s,
                        state_json = %s::jsonb, parent_run_id = %s, updated_at = %s
                    where run_id = %s and current_sequence = %s
                    returning current_sequence
                    """,
                    (
                        checkpoint.thread_id,
                        checkpoint.turn_id,
                        checkpoint.status.value,
                        next_sequence,
                        checkpoint.schema_version,
                        payload,
                        checkpoint.parent_run_id,
                        updated_at,
                        checkpoint.run_id,
                        expected,
                    ),
                ).fetchone()
            if row is None:
                current = conn.execute(
                    "select current_sequence from runs where run_id = %s",
                    (checkpoint.run_id,),
                ).fetchone()
                actual = int(current[0]) if current else 0
                raise CheckpointConflictError(
                    f"stale checkpoint for {checkpoint.run_id}: expected {actual}, got {expected}"
                )
            conn.execute(
                """
                insert into checkpoints(
                    run_id, sequence, schema_version, thread_id, turn_id,
                    status, state_json, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    checkpoint.run_id,
                    next_sequence,
                    checkpoint.schema_version,
                    checkpoint.thread_id,
                    checkpoint.turn_id,
                    checkpoint.status.value,
                    payload,
                    updated_at,
                ),
            )
        checkpoint.sequence = next_sequence
        checkpoint.updated_at = updated_at

    def _load(self, run_id: str) -> Checkpoint | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select state_json from runs where run_id = %s", (run_id,)
            ).fetchone()
        return _checkpoint(row[0]) if row else None

    def _list(self, thread_id: str) -> list[Checkpoint]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "select state_json from runs where thread_id = %s order by created_at, run_id",
                (thread_id,),
            ).fetchall()
        return [_checkpoint(row[0]) for row in rows]

    def _save_tool_execution(self, record: ToolExecutionRecord) -> None:
        updated_at = _now()
        values: Sequence[object] = (
            record.invocation_id,
            record.run_id,
            record.tool_call_id,
            record.tool_name,
            record.arguments_hash,
            record.status.value,
            record.attempt,
            record.result,
            record.is_error,
            record.error,
            record.last_failure_category,
            record.last_error_code,
            record.retry_state.value,
            record.retry_suppressed_reason,
            record.next_retry_at,
            record.retry_backoff_seconds,
            record.started_at,
            record.completed_at,
            updated_at,
        )
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                insert into tool_executions(
                    invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                    status, attempt, result, is_error, error, last_failure_category,
                    last_error_code, retry_state, retry_suppressed_reason,
                    next_retry_at, retry_backoff_seconds, started_at, completed_at, updated_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s
                )
                on conflict(invocation_id) do update set
                    status = excluded.status,
                    attempt = excluded.attempt,
                    result = excluded.result,
                    is_error = excluded.is_error,
                    error = excluded.error,
                    last_failure_category = excluded.last_failure_category,
                    last_error_code = excluded.last_error_code,
                    retry_state = excluded.retry_state,
                    retry_suppressed_reason = excluded.retry_suppressed_reason,
                    next_retry_at = excluded.next_retry_at,
                    retry_backoff_seconds = excluded.retry_backoff_seconds,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                where tool_executions.arguments_hash = excluded.arguments_hash
                returning invocation_id
                """,
                values,
            ).fetchone()
            if row is None:
                raise ValueError("invocation_id reused with different tool arguments")
        record.updated_at = updated_at

    def _load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"{_TOOL_SELECT} where invocation_id = %s", (invocation_id,)
            ).fetchone()
        return _tool_record(row) if row else None

    def _list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"{_TOOL_SELECT} where run_id = %s order by invocation_id", (run_id,)
            ).fetchall()
        return [_tool_record(row) for row in rows]

    def _load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select version, state_json from run_budget_ledgers where owner_run_id = %s",
                (owner_run_id,),
            ).fetchone()
        return (
            BudgetLedgerRecord(owner_run_id=owner_run_id, version=int(row[0]), state=dict(row[1]))
            if row
            else None
        )

    def _save_budget_ledger(self, record: BudgetLedgerRecord) -> None:
        with self.pool.connection() as conn:
            if record.version == 0:
                row = conn.execute(
                    """
                    insert into run_budget_ledgers(owner_run_id, version, state_json)
                    values (%s, 1, %s::jsonb)
                    on conflict(owner_run_id) do nothing
                    returning version
                    """,
                    (record.owner_run_id, _json(record.state)),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    update run_budget_ledgers
                    set version = %s, state_json = %s::jsonb
                    where owner_run_id = %s and version = %s
                    returning version
                    """,
                    (
                        record.version + 1,
                        _json(record.state),
                        record.owner_run_id,
                        record.version,
                    ),
                ).fetchone()
            if row is None:
                raise BudgetLedgerConflictError(
                    f"stale budget ledger for {record.owner_run_id}"
                )
        record.version += 1


class PostgresEventRepository:
    backend = "postgres"

    def __init__(self, pool: PostgresConnectionPool):
        self.pool = pool

    def create_thread(self) -> str:
        thread_id = f"thread_{uuid4().hex}"
        with self.pool.connection() as conn:
            conn.execute(
                "insert into threads(id, created_at) values (%s, %s)", (thread_id, _now())
            )
        self.append_event(thread_id, "thread.created", {"id": thread_id})
        return thread_id

    def thread_exists(self, thread_id: str) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute("select 1 from threads where id = %s", (thread_id,)).fetchone()
        return row is not None

    def list_threads(self) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("select id from threads order by created_at, id").fetchall()
        return [str(row[0]) for row in rows]

    def append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                insert into events(
                    thread_id, turn_id, run_id, parent_run_id, parent_step_id,
                    assignment_id, type, payload, created_at
                )
                select %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                where exists(select 1 from threads where id = %s)
                returning id
                """,
                (
                    thread_id,
                    _optional_text(payload.get("turn_id")),
                    _optional_text(payload.get("run_id")),
                    _optional_text(payload.get("parent_run_id")),
                    _optional_text(payload.get("parent_step_id")),
                    _optional_text(payload.get("assignment_id")),
                    event_type,
                    _json(payload),
                    _now(),
                    thread_id,
                ),
            ).fetchone()
            if row is None:
                raise ValueError("thread not found")
        return int(row[0])

    def list_events(
        self,
        thread_id: str,
        after_id: int | None = None,
        *,
        run_id: str | None = None,
    ) -> list[RuntimeEvent]:
        clauses = ["thread_id = %s"]
        params: list[object] = [thread_id]
        if after_id is not None:
            clauses.append("id > %s")
            params.append(after_id)
        if run_id is not None:
            clauses.append("run_id = %s")
            params.append(run_id)
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                select id, thread_id, type, payload, created_at,
                       turn_id, run_id, parent_run_id, parent_step_id, assignment_id
                from events where
                """
                + " and ".join(clauses)
                + " order by id",
                tuple(params),
            ).fetchall()
        return [
            RuntimeEvent(
                id=int(row[0]),
                thread_id=str(row[1]),
                type=str(row[2]),
                payload=dict(row[3]),
                created_at=_timestamp(row[4]) or "",
                turn_id=_optional_text(row[5]),
                run_id=_optional_text(row[6]),
                parent_run_id=_optional_text(row[7]),
                parent_step_id=_optional_text(row[8]),
                assignment_id=_optional_text(row[9]),
            )
            for row in rows
        ]

    def database_ok(self) -> bool:
        try:
            with self.pool.connection() as conn:
                conn.execute("select 1").fetchone()
            return True
        except Exception:
            return False


class PostgresControlOperationStore:
    backend = "postgres"

    def __init__(self, pool: PostgresConnectionPool):
        self.pool = pool

    def lookup(self, run_id: str, idempotency_key: str) -> ControlOperationRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"{_CONTROL_SELECT} where run_id = %s and idempotency_key = %s",
                (run_id, idempotency_key),
            ).fetchone()
        return _control_record(row) if row else None

    def begin(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        operation: ControlOperationName,
        request: dict[str, Any],
    ) -> tuple[ControlOperationRecord, bool]:
        request_hash = operation_request_hash(operation, request)
        record = ControlOperationRecord(
            operation_id=f"operation_{uuid4().hex}",
            idempotency_key=idempotency_key,
            run_id=run_id,
            operation=operation,
            status=ControlOperationStatus.IN_PROGRESS,
            request_hash=request_hash,
            request=request,
        )
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                insert into control_operations(
                    operation_id, idempotency_key, run_id, operation, status,
                    request_hash, request_json, created_at, schema_version
                ) values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                on conflict(run_id, idempotency_key) do nothing
                returning operation_id
                """,
                (
                    record.operation_id,
                    record.idempotency_key,
                    record.run_id,
                    record.operation.value,
                    record.status.value,
                    record.request_hash,
                    _json(record.request),
                    record.created_at,
                    record.schema_version,
                ),
            ).fetchone()
            if row is not None:
                return record, True
            existing_row = conn.execute(
                f"{_CONTROL_SELECT} where run_id = %s and idempotency_key = %s",
                (run_id, idempotency_key),
            ).fetchone()
        existing = _control_record(existing_row)
        if existing.operation != operation or existing.request_hash != request_hash:
            raise ApiError(
                "idempotency_key_conflict",
                "idempotency key was already used with a different request",
                409,
                run_id=run_id,
                operation=operation.value,
            )
        return existing, False

    def complete(self, operation_id: str, result: dict[str, Any]) -> ControlOperationRecord:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                update control_operations
                set status = %s, result_json = %s::jsonb,
                    error_json = null, completed_at = %s
                where operation_id = %s
                returning operation_id
                """,
                (ControlOperationStatus.COMPLETED.value, _json(result), _now(), operation_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("control operation disappeared")
        return self._require_operation(operation_id)

    def fail(self, operation_id: str, error: ApiError) -> ControlOperationRecord:
        payload = {**error.to_dict()["error"], "http_status": error.http_status}
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                update control_operations
                set status = %s, error_json = %s::jsonb, completed_at = %s
                where operation_id = %s
                returning operation_id
                """,
                (ControlOperationStatus.FAILED.value, _json(payload), _now(), operation_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("control operation disappeared")
        return self._require_operation(operation_id)

    def find_interrupt_resolution(
        self, run_id: str, invocation_id: str
    ) -> ControlOperationRecord | None:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"""{_CONTROL_SELECT}
                where run_id = %s and operation in ('approve', 'reject')
                  and status = 'COMPLETED'
                order by created_at desc""",
                (run_id,),
            ).fetchall()
        for row in rows:
            record = _control_record(row)
            if record.request.get("invocation_id") == invocation_id:
                return record
        return None

    def _require_operation(self, operation_id: str) -> ControlOperationRecord:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"{_CONTROL_SELECT} where operation_id = %s", (operation_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("control operation disappeared")
        return _control_record(row)


_TOOL_SELECT = """
select invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
       status, attempt, result, is_error, error, last_failure_category,
       last_error_code, retry_state, retry_suppressed_reason,
       next_retry_at, retry_backoff_seconds, started_at, completed_at, updated_at
from tool_executions
"""

_CONTROL_SELECT = """
select operation_id, idempotency_key, run_id, operation, status,
       request_hash, request_json, result_json, error_json,
       created_at, completed_at, schema_version
from control_operations
"""


def _checkpoint(value: object) -> Checkpoint:
    data = value if isinstance(value, dict) else json.loads(str(value))
    if not isinstance(data, dict):
        raise ValueError("checkpoint payload must be a JSON object")
    return Checkpoint.from_dict(data)


def _tool_record(row: Sequence[object]) -> ToolExecutionRecord:
    return ToolExecutionRecord.from_dict(
        {
            "invocation_id": row[0],
            "run_id": row[1],
            "tool_call_id": row[2],
            "tool_name": row[3],
            "arguments_hash": row[4],
            "status": row[5],
            "attempt": row[6],
            "result": row[7],
            "is_error": bool(row[8]),
            "error": row[9],
            "last_failure_category": row[10],
            "last_error_code": row[11],
            "retry_state": row[12],
            "retry_suppressed_reason": row[13],
            "next_retry_at": _timestamp(row[14]),
            "retry_backoff_seconds": row[15],
            "started_at": _timestamp(row[16]),
            "completed_at": _timestamp(row[17]),
            "updated_at": _timestamp(row[18]),
        }
    )


def _control_record(row: Sequence[object]) -> ControlOperationRecord:
    return ControlOperationRecord(
        operation_id=str(row[0]),
        idempotency_key=str(row[1]),
        run_id=str(row[2]),
        operation=ControlOperationName(str(row[3])),
        status=ControlOperationStatus(str(row[4])),
        request=dict(row[6]),
        request_hash=str(row[5]),
        result=dict(row[7]) if row[7] is not None else None,
        error=dict(row[8]) if row[8] is not None else None,
        created_at=_timestamp(row[9]) or "",
        completed_at=_timestamp(row[10]),
        schema_version=int(row[11]),
    )


def _json(value: object) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, separators=(",", ":"))


def _json_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return _json_value(value.value)
    return value


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


_SCHEMA_STATEMENTS = (
    """
    create table if not exists runs (
        run_id text primary key,
        thread_id text not null,
        turn_id text not null,
        status text not null,
        current_sequence bigint not null,
        schema_version integer not null,
        state_json jsonb not null,
        parent_run_id text,
        created_at timestamptz not null,
        updated_at timestamptz not null
    )
    """,
    "create index if not exists idx_runs_thread on runs(thread_id, created_at, run_id)",
    "create index if not exists idx_runs_parent on runs(parent_run_id)",
    """
    create table if not exists checkpoints (
        run_id text not null references runs(run_id) on delete cascade,
        sequence bigint not null,
        schema_version integer not null,
        thread_id text not null,
        turn_id text not null,
        status text not null,
        state_json jsonb not null,
        created_at timestamptz not null,
        primary key(run_id, sequence)
    )
    """,
    "create index if not exists idx_checkpoints_thread on checkpoints(thread_id, run_id, sequence)",
    """
    create table if not exists tool_executions (
        invocation_id text primary key,
        run_id text not null,
        tool_call_id text not null,
        tool_name text not null,
        arguments_hash text not null,
        status text not null,
        attempt integer not null,
        result text,
        is_error boolean not null,
        error text,
        last_failure_category text,
        last_error_code text,
        retry_state text not null default 'NONE',
        retry_suppressed_reason text,
        next_retry_at timestamptz,
        retry_backoff_seconds double precision not null default 0,
        started_at timestamptz,
        completed_at timestamptz,
        updated_at timestamptz not null
    )
    """,
    "create index if not exists idx_tool_executions_run on tool_executions(run_id, invocation_id)",
    """
    create table if not exists run_budget_ledgers (
        owner_run_id text primary key,
        version bigint not null,
        state_json jsonb not null
    )
    """,
    """
    create table if not exists threads (
        id text primary key,
        created_at timestamptz not null
    )
    """,
    """
    create table if not exists events (
        id bigint generated by default as identity primary key,
        thread_id text not null references threads(id) on delete cascade,
        turn_id text,
        run_id text,
        parent_run_id text,
        parent_step_id text,
        assignment_id text,
        type text not null,
        payload jsonb not null,
        created_at timestamptz not null
    )
    """,
    "create index if not exists idx_events_thread_id on events(thread_id, id)",
    "create index if not exists idx_events_run_id on events(thread_id, run_id, id)",
    """
    create table if not exists control_operations (
        operation_id text primary key,
        idempotency_key text not null,
        run_id text not null,
        operation text not null,
        status text not null,
        request_hash text not null,
        request_json jsonb not null,
        result_json jsonb,
        error_json jsonb,
        created_at timestamptz not null,
        completed_at timestamptz,
        schema_version integer not null,
        unique(run_id, idempotency_key)
    )
    """,
    """
    create index if not exists idx_control_operations_interrupt
    on control_operations(run_id, operation, status)
    """,
)
