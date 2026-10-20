from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from axiom.artifacts.metadata import postgres_artifact_schema
from axiom.config import TrafficGovernanceConfig
from axiom.provenance import postgres_provenance_schema
from axiom.runtime.capacity import AdmissionRejectedError, CapacitySnapshot
from axiom.runtime.checkpoints import (
    _RUN_SUMMARY_FIELDS,
    OCCUPYING_RUN_STATUSES,
    BudgetLedgerConflictError,
    CheckpointConflictError,
    _attach_child_counts,
    _check_submission_fingerprint,
    _check_thread_available,
    _child_counts_sql,
    _completed_payload,
    _raise_submission_rate_limit,
    _run_list_filters,
    _run_list_order,
    _submission_events,
    _submission_messages,
)
from axiom.runtime.control_plane import (
    ApiError,
    ControlOperationName,
    ControlOperationRecord,
    ControlOperationStatus,
    operation_request_hash,
)
from axiom.runtime.events import RuntimeEvent
from axiom.runtime.models import (
    BudgetLedgerRecord,
    Checkpoint,
    RunError,
    RunStatus,
    ToolExecutionRecord,
)
from axiom.runtime.ownership import OwnershipLostError, RunOwnership

POSTGRES_SCHEMA_VERSION = 9
RUN_DELIVERY_EXHAUSTED = "RUN_DELIVERY_EXHAUSTED"


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
    """Create or migrate the schema and reject stores created by newer code."""
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
        row = conn.execute(
            "select version from axiom_schema_versions where component = 'runtime'"
        ).fetchone()
        version = int(row[0]) if row else POSTGRES_SCHEMA_VERSION
        if version not in {1, 2, 3, 4, 5, 6, 7, 8, POSTGRES_SCHEMA_VERSION}:
            raise PostgresSchemaError(f"unsupported PostgreSQL runtime schema version: {version}")
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)
        for statement in _OWNERSHIP_MIGRATION_STATEMENTS:
            conn.execute(statement)
        for statement in postgres_artifact_schema():
            conn.execute(statement)
        for statement in postgres_provenance_schema():
            conn.execute(statement)
        conn.execute(
            """
            insert into axiom_schema_versions(component, version, updated_at)
            values ('runtime', %s, %s)
            on conflict(component) do update set
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (POSTGRES_SCHEMA_VERSION, _now()),
        )


class PostgresRuntimeStore:
    backend = "postgres"

    async def thread_run_head(self, thread_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._thread_run_head, thread_id)

    def _thread_run_head(self, thread_id: str) -> dict[str, Any] | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select run_id, status from runs where thread_id = %s "
                "and parent_run_id is null order by case when status in "
                "('RUNNING','INTERRUPTED','WAITING_APPROVAL','WAITING_CHILD') "
                "then 0 else 1 end, created_at desc, run_id desc limit 1", (thread_id,),
            ).fetchone()
        return {"run_id": _text(row[0]), "status": _text(row[1])} if row else None

    async def list_run_summaries(
        self, *, limit: int = 50,
        position: tuple[str | int | float, str, str] | None = None,
        search: str = "", status: str = "", strategy: str = "", sort: str = "newest",
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_run_summaries, limit, position, search, status, strategy, sort,
        )

    def _list_run_summaries(
        self, limit: int, position: tuple[str | int | float, str, str] | None,
        search: str, status: str, strategy: str, sort: str,
    ) -> list[dict[str, Any]]:
        clauses, params = _run_list_filters(search, status, strategy, "%s")
        sort_expression, direction = _run_list_order(sort, "postgres")
        if position is not None:
            value, created_at, run_id = position
            comparison = ">" if direction == "asc" else "<"
            if sort in {"newest", "oldest"}:
                clauses.append(f"(created_at, run_id) {comparison} (%s, %s)")
                params.extend((created_at, run_id))
            else:
                clauses.append(
                    f"({sort_expression} {comparison} %s or ({sort_expression} = %s "
                    "and (created_at, run_id) < (%s, %s)))"
                )
                params.extend((value, value, created_at, run_id))
        order = (f"{sort_expression} {direction}, "
                 + ("created_at desc, " if sort in {"status", "duration"} else "")
                 + f"run_id {'asc' if sort == 'oldest' else 'desc'}")
        query = (_POSTGRES_RUN_ROWS + "select " + ", ".join(_RUN_SUMMARY_FIELDS)
                 + f", {sort_expression} as _cursor_value from current_runs where "
                 + " and ".join(clauses) + f" order by {order} limit %s")
        with self.pool.connection() as conn:
            rows = conn.execute(query, (*params, limit + 1)).fetchall()
            summaries = [dict(zip((*_RUN_SUMMARY_FIELDS, "_cursor_value"), row, strict=True))
                         for row in rows]
            if summaries:
                ids = [item["run_id"] for item in summaries]
                counts = conn.execute(
                    _POSTGRES_RUN_ROWS + _child_counts_sql(",".join("%s" for _ in ids)), ids,
                ).fetchall()
                _attach_child_counts(summaries, counts)
        for summary in summaries:
            for field in ("created_at", "updated_at", "started_at", "completed_at"):
                summary[field] = _timestamp(summary[field])
            summary["_cursor_value"] = (
                summary["created_at"] if sort in {"newest", "oldest"}
                else float(summary["_cursor_value"])
            )
        return summaries

    def __init__(self, pool: PostgresConnectionPool):
        self.pool = pool
        self._admission_rejections = 0
        self._capacity_blocked_claims = 0
        self._idempotent_submission_replays = 0
        self._idempotency_conflicts = 0
        self._manual_requeues = 0

    async def admit_run(
        self,
        checkpoint: Checkpoint,
        max_queued_runs: int | None = None,
        *,
        max_queued_runs_per_principal: int | None = None,
    ) -> None:
        _require_optional_limit(max_queued_runs, "max_queued_runs")
        await asyncio.to_thread(
            self._admit_run, checkpoint, max_queued_runs, max_queued_runs_per_principal
        )

    async def admit_submission(
        self,
        checkpoint: Checkpoint,
        *,
        idempotency_key: str | None,
        request_fingerprint: str,
        max_queued_runs: int | None = None,
        max_queued_runs_per_principal: int | None = None,
        traffic: TrafficGovernanceConfig | None = None,
        runnable: bool = True,
    ) -> tuple[Checkpoint, bool]:
        _require_optional_limit(max_queued_runs, "max_queued_runs")
        return await asyncio.to_thread(
            self._admit_submission,
            checkpoint,
            idempotency_key,
            request_fingerprint,
            max_queued_runs,
            max_queued_runs_per_principal,
            traffic,
            runnable,
        )

    async def lookup_submission(
        self, thread_id: str, idempotency_key: str, request_fingerprint: str
    ) -> Checkpoint | None:
        return await asyncio.to_thread(
            self._lookup_submission, thread_id, idempotency_key, request_fingerprint
        )

    def _lookup_submission(
        self, thread_id: str, idempotency_key: str, request_fingerprint: str
    ) -> Checkpoint | None:
        with self.pool.connection() as conn:
            return self._lookup_submission_in_transaction(
                conn, thread_id, idempotency_key, request_fingerprint
            )

    def _lookup_submission_in_transaction(
        self, conn, thread_id: str, idempotency_key: str, request_fingerprint: str
    ) -> Checkpoint | None:
        existing = conn.execute(
            "select request_fingerprint, run_id from submission_idempotency "
            "where thread_id = %s and idempotency_key = %s", (thread_id, idempotency_key),
        ).fetchone()
        if existing is None:
            return None
        if _text(existing[0]) != request_fingerprint:
            self._idempotency_conflicts += 1
        _check_submission_fingerprint(_text(existing[0]), request_fingerprint)
        row = conn.execute(
            f"select {_RUN_STATE_SELECT} from runs where run_id = %s", (_text(existing[1]),)
        ).fetchone()
        if row is None:
            raise RuntimeError("submission idempotency mapping references a missing Run")
        self._idempotent_submission_replays += 1
        return _checkpoint_with_delivery(row)

    async def project_completed_turn(self, checkpoint: Checkpoint) -> tuple[int | None, bool]:
        return await asyncio.to_thread(self._project_completed_turn, checkpoint.run_id)

    def _project_completed_turn(self, run_id: str) -> tuple[int | None, bool]:
        with self.pool.connection() as conn:
            _lock_capacity_coordination(conn)
            identity = conn.execute(
                "select thread_id from runs where run_id = %s", (run_id,)
            ).fetchone()
            if identity is None:
                return None, False
            _lock_thread(conn, _text(identity[0]))
            row = conn.execute(
                f"select {_RUN_STATE_SELECT} from runs where run_id = %s", (run_id,)
            ).fetchone()
            return _project_completed_in_transaction(conn, _checkpoint_with_delivery(row))

    async def requeue_delivery_exhausted(self, run_id: str) -> Checkpoint:
        return await asyncio.to_thread(self._requeue_delivery_exhausted, run_id)

    async def save(self, checkpoint: Checkpoint, *, ownership: RunOwnership | None = None) -> None:
        await asyncio.to_thread(self._save, checkpoint, ownership)

    async def load(self, run_id: str) -> Checkpoint | None:
        return await asyncio.to_thread(self._load, run_id)

    async def list(self, thread_id: str) -> list[Checkpoint]:
        return await asyncio.to_thread(self._list, thread_id)

    async def save_tool_execution(
        self, record: ToolExecutionRecord, *, ownership: RunOwnership | None = None
    ) -> None:
        await asyncio.to_thread(self._save_tool_execution, record, ownership)

    async def load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None:
        return await asyncio.to_thread(self._load_tool_execution, invocation_id)

    async def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        return await asyncio.to_thread(self._list_tool_executions, run_id)

    async def load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None:
        return await asyncio.to_thread(self._load_budget_ledger, owner_run_id)

    async def save_budget_ledger(
        self, record: BudgetLedgerRecord, *, ownership: RunOwnership | None = None
    ) -> None:
        await asyncio.to_thread(self._save_budget_ledger, record, ownership)

    async def mark_runnable(self, run_id: str) -> None:
        await asyncio.to_thread(self._mark_runnable, run_id)

    async def claim_run(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: float,
        max_active_runs: int | None = None,
        max_run_delivery_attempts: int | None = None,
        max_active_runs_per_principal: int | None = None,
        aging_interval_seconds: float = 300.0,
        aging_boost_cap: int = 2,
    ) -> RunOwnership | None:
        _require_positive_lease(lease_seconds)
        _require_optional_limit(max_active_runs, "max_active_runs")
        _require_optional_limit(max_run_delivery_attempts, "max_run_delivery_attempts")
        return await asyncio.to_thread(
            self._claim_run,
            run_id,
            worker_id,
            lease_seconds,
            max_active_runs,
            max_run_delivery_attempts,
            max_active_runs_per_principal,
            aging_interval_seconds,
            aging_boost_cap,
        )

    async def claim_next(
        self,
        worker_id: str,
        lease_seconds: float,
        max_active_runs: int | None = None,
        max_run_delivery_attempts: int | None = None,
        max_active_runs_per_principal: int | None = None,
        aging_interval_seconds: float = 300.0,
        aging_boost_cap: int = 2,
    ) -> RunOwnership | None:
        _require_positive_lease(lease_seconds)
        _require_optional_limit(max_active_runs, "max_active_runs")
        _require_optional_limit(max_run_delivery_attempts, "max_run_delivery_attempts")
        return await asyncio.to_thread(
            self._claim_next,
            worker_id,
            lease_seconds,
            max_active_runs,
            max_run_delivery_attempts,
            max_active_runs_per_principal,
            aging_interval_seconds,
            aging_boost_cap,
        )

    async def renew_lease(
        self, ownership: RunOwnership, lease_seconds: float
    ) -> RunOwnership | None:
        _require_positive_lease(lease_seconds)
        return await asyncio.to_thread(self._renew_lease, ownership, lease_seconds)

    async def release_lease(self, ownership: RunOwnership, *, runnable: bool = True) -> bool:
        return await asyncio.to_thread(self._release_lease, ownership, runnable)

    async def get_ownership(self, run_id: str) -> RunOwnership | None:
        return await asyncio.to_thread(self._get_ownership, run_id)

    async def validate_ownership(self, ownership: RunOwnership) -> None:
        await asyncio.to_thread(self._validate_ownership, ownership)

    async def capacity_snapshot(
        self,
        *,
        max_queued_runs: int | None = None,
        max_active_runs: int | None = None,
    ) -> CapacitySnapshot:
        return await asyncio.to_thread(self._capacity_snapshot, max_queued_runs, max_active_runs)

    async def consume_submission_tokens(
        self,
        principal_key: str,
        *,
        global_rate: float | None,
        global_burst: int | None,
        principal_rate: float | None,
        principal_burst: int | None,
    ) -> float | None:
        """Consume one distributed new-root token; return retry seconds if blocked."""
        return await asyncio.to_thread(
            _consume_submission_tokens,
            self,
            principal_key,
            global_rate,
            global_burst,
            principal_rate,
            principal_burst,
        )

    def _admit_run(
        self,
        checkpoint: Checkpoint,
        max_queued_runs: int | None,
        max_queued_runs_per_principal: int | None,
    ) -> None:
        if checkpoint.sequence != 0:
            raise ValueError("only a new Run may pass external admission")
        updated_at = _now()
        with self.pool.connection() as conn:
            _lock_capacity_coordination(conn)
            if checkpoint.parent_run_id is None:
                _lock_thread(conn, checkpoint.thread_id)
                _check_postgres_thread_available(conn, checkpoint.thread_id, checkpoint.run_id)
            if conn.execute(
                "select 1 from runs where run_id = %s", (checkpoint.run_id,)
            ).fetchone():
                raise ValueError(f"run already exists: {checkpoint.run_id}")
            queued = _queued_count(conn)
            if max_queued_runs is not None and queued >= max_queued_runs:
                self._admission_rejections += 1
                raise AdmissionRejectedError(queued_runs=queued, max_queued_runs=max_queued_runs)
            _check_principal_queue_quota(
                conn, checkpoint.principal_key, max_queued_runs_per_principal
            )
            _insert_initial_run(conn, checkpoint, updated_at=updated_at, runnable=True)
        checkpoint.sequence = 1
        checkpoint.updated_at = updated_at

    def _admit_submission(
        self,
        checkpoint: Checkpoint,
        idempotency_key: str | None,
        request_fingerprint: str,
        max_queued_runs: int | None,
        max_queued_runs_per_principal: int | None,
        traffic: TrafficGovernanceConfig | None,
        runnable: bool,
    ) -> tuple[Checkpoint, bool]:
        if checkpoint.sequence != 0 or checkpoint.parent_run_id is not None:
            raise ValueError("only a new root Run may pass submission admission")
        updated_at = _now()
        with self.pool.connection() as conn:
            _lock_capacity_coordination(conn)
            _lock_thread(conn, checkpoint.thread_id)
            if idempotency_key is not None:
                existing = self._lookup_submission_in_transaction(
                    conn, checkpoint.thread_id, idempotency_key, request_fingerprint
                )
                if existing is not None:
                    return existing, False
            _check_postgres_thread_available(conn, checkpoint.thread_id, checkpoint.run_id)
            if conn.execute(
                "select 1 from runs where run_id = %s", (checkpoint.run_id,)
            ).fetchone():
                raise ValueError(f"run already exists: {checkpoint.run_id}")
            queued = _queued_count(conn)
            if max_queued_runs is not None and queued >= max_queued_runs:
                self._admission_rejections += 1
                raise AdmissionRejectedError(queued_runs=queued, max_queued_runs=max_queued_runs)
            _check_principal_queue_quota(
                conn, checkpoint.principal_key, max_queued_runs_per_principal
            )
            if traffic is not None:
                wait = _consume_submission_tokens_in_transaction(
                    conn, checkpoint.principal_key, traffic.global_submission_rate,
                    traffic.global_submission_burst, traffic.principal_submission_rate,
                    traffic.principal_submission_burst,
                )
                if wait is not None:
                    _raise_submission_rate_limit(wait)
            previous = [_checkpoint(row[0]) for row in conn.execute(
                "select state_json from runs where thread_id = %s order by created_at, run_id",
                (checkpoint.thread_id,),
            ).fetchall()]
            for state in previous:
                _project_completed_in_transaction(conn, state)
            admitted = replace(
                checkpoint, messages=_submission_messages(previous, checkpoint.input)
            )
            _insert_initial_run(conn, admitted, updated_at=updated_at, runnable=runnable)
            if idempotency_key is not None:
                conn.execute(
                    """
                    insert into submission_idempotency(
                        thread_id, idempotency_key, request_fingerprint, run_id, created_at
                    ) values (%s, %s, %s, %s, %s)
                    """,
                    (checkpoint.thread_id, idempotency_key, request_fingerprint,
                     checkpoint.run_id, updated_at),
                )
            for event_type, payload in _submission_events(checkpoint):
                _append_submission_event(conn, checkpoint, event_type, payload)
        checkpoint.sequence = 1
        checkpoint.updated_at = updated_at
        checkpoint.messages = admitted.messages
        return checkpoint, True

    def _requeue_delivery_exhausted(self, run_id: str) -> Checkpoint:
        with self.pool.connection() as conn:
            _lock_run_thread(conn, run_id)
            row = conn.execute(
                f"""
                select {_RUN_STATE_SELECT}, current_sequence, fencing_token
                from runs where run_id = %s for update
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError("run not found")
            state = _checkpoint_with_delivery(row[:7])
            if not (
                state.status == RunStatus.FAILED
                and state.error is not None
                and state.error.type == RUN_DELIVERY_EXHAUSTED
                and state.failure_queued_at is not None
            ):
                raise ValueError("run is not in the delivery failure queue")
            now = _timestamp(conn.execute("select current_timestamp").fetchone()[0])
            if now is None:  # pragma: no cover
                raise RuntimeError("PostgreSQL did not return current time")
            state.status = RunStatus.RUNNING
            state.error = None
            state.delivery_attempt = 0
            state.failure_queued_at = None
            state.last_delivery_failure = None
            state.last_delivery_failed_at = None
            state.last_delivery_worker_id = None
            state.last_delivery_fencing_token = None
            next_sequence = int(row[7]) + 1
            payload = state.to_dict()
            payload["sequence"] = next_sequence
            payload["updated_at"] = now
            conn.execute(
                """
                update runs set status = 'RUNNING', current_sequence = %s,
                    state_json = %s::jsonb, updated_at = %s, runnable = true,
                    owner_worker_id = null, lease_until = null,
                    fencing_token = fencing_token + 1, delivery_attempt = 0,
                    failure_queued_at = null, last_delivery_failure = null,
                    last_delivery_failed_at = null, last_delivery_worker_id = null,
                    last_delivery_fencing_token = null
                where run_id = %s
                """,
                (next_sequence, _json(payload), now, run_id),
            )
            conn.execute(
                """
                insert into checkpoints(
                    run_id, sequence, schema_version, thread_id, turn_id,
                    status, state_json, created_at
                ) values (%s, %s, %s, %s, %s, 'RUNNING', %s::jsonb, %s)
                """,
                (
                    run_id,
                    next_sequence,
                    state.schema_version,
                    state.thread_id,
                    state.turn_id,
                    _json(payload),
                    now,
                ),
            )
        state.sequence = next_sequence
        state.updated_at = now
        self._manual_requeues += 1
        return state

    def _save(self, checkpoint: Checkpoint, ownership: RunOwnership | None = None) -> None:
        expected = checkpoint.sequence
        next_sequence = expected + 1
        updated_at = _now()
        state = checkpoint.to_dict()
        state["sequence"] = next_sequence
        state["updated_at"] = updated_at
        payload = _json(state)
        with self.pool.connection() as conn:
            if checkpoint.parent_run_id is None:
                _lock_capacity_coordination(conn)
                _lock_thread(conn, checkpoint.thread_id)
            if ownership is not None:
                _lock_and_validate_ownership(conn, ownership)
            if checkpoint.parent_run_id is None and checkpoint.status in OCCUPYING_RUN_STATUSES:
                _check_postgres_thread_available(conn, checkpoint.thread_id, checkpoint.run_id)
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
            if checkpoint.finished:
                _project_completed_in_transaction(conn, checkpoint)
                conn.execute(
                    """
                    update runs
                    set runnable = false,
                        owner_worker_id = case when run_id = %s then null else owner_worker_id end,
                        lease_until = case when run_id = %s then null else lease_until end
                    where run_id = %s
                    """,
                    (
                        ownership.run_id if ownership else "",
                        ownership.run_id if ownership else "",
                        checkpoint.run_id,
                    ),
                )
                if checkpoint.parent_run_id is not None:
                    conn.execute(
                        """
                        update runs
                        set runnable = true,
                            state_json = jsonb_set(
                                state_json,
                                ARRAY['runnable_since'],
                                to_jsonb(current_timestamp::text),
                                true
                            )
                        where run_id = %s and status = 'WAITING_CHILD'
                          and owner_worker_id is null
                        """,
                        (checkpoint.parent_run_id,),
                    )
        checkpoint.sequence = next_sequence
        checkpoint.updated_at = updated_at

    def _load(self, run_id: str) -> Checkpoint | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"select {_RUN_STATE_SELECT} from runs where run_id = %s", (run_id,)
            ).fetchone()
        return _checkpoint_with_delivery(row) if row else None

    def _list(self, thread_id: str) -> list[Checkpoint]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"select {_RUN_STATE_SELECT} from runs "
                "where thread_id = %s order by created_at, run_id",
                (thread_id,),
            ).fetchall()
        return [_checkpoint_with_delivery(row) for row in rows]

    def _save_tool_execution(
        self, record: ToolExecutionRecord, ownership: RunOwnership | None = None
    ) -> None:
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
            _json(record.artifact_ids),
            updated_at,
        )
        with self.pool.connection() as conn:
            if ownership is not None:
                _lock_and_validate_ownership(conn, ownership)
            row = conn.execute(
                """
                insert into tool_executions(
                    invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                    status, attempt, result, is_error, error, last_failure_category,
                    last_error_code, retry_state, retry_suppressed_reason,
                    next_retry_at, retry_backoff_seconds, started_at, completed_at,
                    artifact_ids, updated_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s::jsonb, %s
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
                    artifact_ids = excluded.artifact_ids,
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

    def _save_budget_ledger(
        self, record: BudgetLedgerRecord, ownership: RunOwnership | None = None
    ) -> None:
        with self.pool.connection() as conn:
            if ownership is not None:
                _lock_and_validate_ownership(conn, ownership)
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
                raise BudgetLedgerConflictError(f"stale budget ledger for {record.owner_run_id}")
        record.version += 1

    def _mark_runnable(self, run_id: str) -> None:
        with self.pool.connection() as conn:
            _lock_run_thread(conn, run_id)
            row = conn.execute(
                f"""
                update runs as candidate
                set runnable = true,
                    state_json = jsonb_set(
                        state_json, ARRAY['runnable_since'], to_jsonb(current_timestamp::text), true
                    )
                where run_id = %s
                  and status in ('RUNNING', 'WAITING_CHILD')
                  and {_DEADLINE_IS_LIVE}
                  and {_ANCESTORS_NOT_CANCELLED}
                returning run_id
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"run is not eligible for distributed execution: {run_id}")

    def _claim_run(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: float,
        max_active_runs: int | None,
        max_run_delivery_attempts: int | None,
        max_active_runs_per_principal: int | None,
        aging_interval_seconds: float,
        aging_boost_cap: int,
    ) -> RunOwnership | None:
        with self.pool.connection() as conn:
            _lock_run_thread(conn, run_id)
            candidate = conn.execute(
                f"""
                select candidate.run_id, candidate.owner_worker_id,
                       candidate.delivery_attempt, candidate.state_json,
                       candidate.current_sequence, candidate.schema_version,
                       candidate.thread_id, candidate.turn_id,
                       candidate.parent_run_id, candidate.fencing_token
                from runs as candidate
                where candidate.run_id = %s and {_RUN_IS_CLAIMABLE}
                  and (%s::integer is null or (
                    select count(*) from runs as active
                    where {_RUN_IS_ACTIVE.replace("candidate.", "active.")}
                      and active.state_json ->> 'principal_key'
                          = candidate.state_json ->> 'principal_key'
                  ) < %s::integer)
                for update
                """,
                (run_id, max_active_runs_per_principal, max_active_runs_per_principal),
            ).fetchone()
            if _delivery_is_exhausted(candidate, max_run_delivery_attempts):
                _mark_delivery_exhausted(
                    conn,
                    candidate,
                    max_run_delivery_attempts=max_run_delivery_attempts,
                )
                return None
            if max_active_runs is not None and _active_count(conn) >= max_active_runs:
                self._capacity_blocked_claims += 1
                return None
            return self._claim_locked_candidate(
                conn,
                candidate,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    def _claim_next(
        self,
        worker_id: str,
        lease_seconds: float,
        max_active_runs: int | None,
        max_run_delivery_attempts: int | None,
        max_active_runs_per_principal: int | None,
        aging_interval_seconds: float,
        aging_boost_cap: int,
    ) -> RunOwnership | None:
        with self.pool.connection() as conn:
            _lock_capacity_coordination(conn)
            candidate = conn.execute(
                f"""
                select candidate.run_id, candidate.owner_worker_id,
                       candidate.delivery_attempt, candidate.state_json,
                       candidate.current_sequence, candidate.schema_version,
                       candidate.thread_id, candidate.turn_id,
                       candidate.parent_run_id, candidate.fencing_token
                from runs as candidate
                where {_RUN_IS_CLAIMABLE}
                  and (%s::integer is null or (
                    select count(*) from runs as active
                    where {_RUN_IS_ACTIVE.replace("candidate.", "active.")}
                      and active.state_json ->> 'principal_key'
                          = candidate.state_json ->> 'principal_key'
                  ) < %s::integer)
                order by
                    (
                      coalesce((candidate.state_json ->> 'base_priority')::integer, 1)
                      + least(
                          %s,
                          floor(extract(epoch from current_timestamp -
                            coalesce((candidate.state_json ->> 'runnable_since')::timestamptz,
                                     candidate.created_at)) / %s)
                        )
                    ) desc,
                    coalesce((candidate.state_json ->> 'runnable_since')::timestamptz,
                             candidate.created_at),
                    candidate.run_id
                limit 1
                """,
                (
                    max_active_runs_per_principal,
                    max_active_runs_per_principal,
                    aging_boost_cap,
                    aging_interval_seconds,
                ),
            ).fetchone()
            if candidate is not None:
                # Select identity first so the Thread is locked before the Run, as in
                # admission and reactivation. Recheck eligibility while locking the Run:
                # lease release/renewal does not take the capacity coordination lock.
                _lock_run_thread(conn, _text(candidate[0]))
                candidate = conn.execute(
                    f"""
                    select candidate.run_id, candidate.owner_worker_id,
                           candidate.delivery_attempt, candidate.state_json,
                           candidate.current_sequence, candidate.schema_version,
                           candidate.thread_id, candidate.turn_id,
                           candidate.parent_run_id, candidate.fencing_token
                    from runs as candidate
                    where candidate.run_id = %s and {_RUN_IS_CLAIMABLE}
                    for update skip locked
                    """,
                    (_text(candidate[0]),),
                ).fetchone()
            if _delivery_is_exhausted(candidate, max_run_delivery_attempts):
                _mark_delivery_exhausted(
                    conn,
                    candidate,
                    max_run_delivery_attempts=max_run_delivery_attempts,
                )
                return None
            if max_active_runs is not None and _active_count(conn) >= max_active_runs:
                self._capacity_blocked_claims += 1
                return None
            return self._claim_locked_candidate(
                conn,
                candidate,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
            )

    def _claim_locked_candidate(
        self,
        conn,
        candidate,
        *,
        worker_id: str,
        lease_seconds: float,
    ) -> RunOwnership | None:
        if candidate is None:
            return None
        previous_worker_id = _optional_text(candidate[1])
        delivery_attempt = int(candidate[2])
        takeover = previous_worker_id is not None
        next_attempt = (
            delivery_attempt + 1 if takeover else 1 if delivery_attempt == 0 else delivery_attempt
        )
        row = conn.execute(
            """
            update runs
            set owner_worker_id = %s,
                lease_until = current_timestamp + %s * interval '1 second',
                fencing_token = fencing_token + 1,
                claimed_at = current_timestamp,
                last_heartbeat_at = current_timestamp,
                delivery_attempt = %s,
                last_delivery_failure = case
                    when %s then 'lease_expired' else last_delivery_failure end,
                last_delivery_failed_at = case
                    when %s then current_timestamp else last_delivery_failed_at end,
                last_delivery_worker_id = case
                    when %s then %s else last_delivery_worker_id end,
                last_delivery_fencing_token = case
                    when %s then %s else last_delivery_fencing_token end
            where run_id = %s
            returning run_id, owner_worker_id, fencing_token, lease_until, delivery_attempt
            """,
            (
                worker_id,
                lease_seconds,
                next_attempt,
                takeover,
                takeover,
                takeover,
                previous_worker_id,
                takeover,
                int(candidate[9]),
                _text(candidate[0]),
            ),
        ).fetchone()
        return _ownership(row, takeover=takeover)

    def _capacity_snapshot(
        self, max_queued_runs: int | None, max_active_runs: int | None
    ) -> CapacitySnapshot:
        with self.pool.connection() as conn:
            row = conn.execute(
                f"""
                select
                    count(*) filter (where {_RUN_IS_QUEUED}),
                    count(*) filter (where {_RUN_IS_ACTIVE}),
                    extract(epoch from current_timestamp - min(created_at)
                        filter (where {_RUN_IS_QUEUED})),
                    count(*) filter (where failure_queued_at is not null),
                    coalesce(avg(delivery_attempt), 0),
                    coalesce(max(delivery_attempt), 0)
                from runs as candidate
                """
            ).fetchone()
        return CapacitySnapshot(
            queued_runs=int(row[0]),
            active_runs=int(row[1]),
            max_queued_runs=max_queued_runs,
            max_active_runs=max_active_runs,
            admission_rejections=self._admission_rejections,
            capacity_blocked_claims=self._capacity_blocked_claims,
            oldest_queued_age_seconds=float(row[2]) if row[2] is not None else None,
            failure_queued_runs=int(row[3]),
            mean_delivery_attempts=float(row[4]),
            max_delivery_attempts=int(row[5]),
            idempotent_submission_replays=self._idempotent_submission_replays,
            idempotency_conflicts=self._idempotency_conflicts,
            manual_requeues=self._manual_requeues,
        )

    def _renew_lease(self, ownership: RunOwnership, lease_seconds: float) -> RunOwnership | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                update runs
                set lease_until = current_timestamp + %s * interval '1 second',
                    last_heartbeat_at = current_timestamp
                where run_id = %s and owner_worker_id = %s and fencing_token = %s
                  and status in ('RUNNING', 'WAITING_CHILD')
                  and lease_until > current_timestamp
                returning run_id, owner_worker_id, fencing_token, lease_until,
                          delivery_attempt
                """,
                (
                    lease_seconds,
                    ownership.run_id,
                    ownership.worker_id,
                    ownership.fencing_token,
                ),
            ).fetchone()
        return _ownership(row) if row else None

    def _release_lease(self, ownership: RunOwnership, runnable: bool) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                update runs
                set owner_worker_id = null, lease_until = null,
                    runnable = case
                        when status = 'RUNNING' then %s
                        else false
                    end,
                    state_json = case when status = 'RUNNING' and %s
                        then jsonb_set(
                            state_json,
                            ARRAY['runnable_since'],
                            to_jsonb(current_timestamp::text),
                            true
                        )
                        else state_json end
                where run_id = %s and owner_worker_id = %s and fencing_token = %s
                returning run_id
                """,
                (
                    runnable,
                    runnable,
                    ownership.run_id,
                    ownership.worker_id,
                    ownership.fencing_token,
                ),
            ).fetchone()
        return row is not None

    def _get_ownership(self, run_id: str) -> RunOwnership | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                select run_id, owner_worker_id, fencing_token, lease_until,
                       delivery_attempt
                from runs where run_id = %s and owner_worker_id is not null
                """,
                (run_id,),
            ).fetchone()
        return _ownership(row) if row else None

    def _validate_ownership(self, ownership: RunOwnership) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                select 1 from runs
                where run_id = %s and owner_worker_id = %s and fencing_token = %s
                  and status in ('RUNNING', 'WAITING_CHILD')
                  and lease_until > current_timestamp
                """,
                (ownership.run_id, ownership.worker_id, ownership.fencing_token),
            ).fetchone()
        if row is None:
            raise OwnershipLostError(
                f"worker {ownership.worker_id} no longer owns run {ownership.run_id}"
            )


class PostgresEventRepository:
    backend = "postgres"

    def __init__(self, pool: PostgresConnectionPool):
        self.pool = pool

    def create_thread(self) -> str:
        thread_id = f"thread_{uuid4().hex}"
        with self.pool.connection() as conn:
            conn.execute("insert into threads(id, created_at) values (%s, %s)", (thread_id, _now()))
        self.append_event(thread_id, "thread.created", {"id": thread_id})
        return thread_id

    def thread_exists(self, thread_id: str) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute("select 1 from threads where id = %s", (thread_id,)).fetchone()
        return row is not None

    def list_threads(self) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("select id from threads order by created_at, id").fetchall()
            return [_text(row[0]) for row in rows]

    def append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> int:
        with self.pool.connection() as conn:
            # Allocate sequence IDs only after this Thread's preceding event writer
            # commits. PostgreSQL sequences alone do not imply commit order.
            if conn.execute(
                "select id from threads where id = %s for update", (thread_id,),
            ).fetchone() is None:
                raise ValueError("thread not found")
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
                thread_id=_text(row[1]),
                type=_text(row[2]),
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

    def page_events(
        self, thread_id: str, *, after_id: int | None = None,
        before_id: int | None = None, run_id: str | None = None,
        limit: int = 200, latest: bool = False,
    ) -> tuple[list[RuntimeEvent], int]:
        clauses = ["e.thread_id = %s"]
        params: list[object] = [thread_id]
        if after_id is not None:
            clauses.append("e.id > %s")
            params.append(after_id)
        if before_id is not None:
            clauses.append("e.id < %s")
            params.append(before_id)
        if run_id is not None:
            clauses.append("e.run_id = %s")
            params.append(run_id)
        return self._event_page(clauses, params, limit, latest or before_id is not None)

    def page_messages(
        self, thread_id: str, *, before_id: int | None = None,
        after_id: int | None = None, limit: int = 50,
    ) -> tuple[list[RuntimeEvent], int]:
        clauses = ["e.thread_id = %s", _POSTGRES_MESSAGE_FILTER]
        params: list[object] = [thread_id]
        if before_id is not None:
            clauses.append("e.id < %s")
            params.append(before_id)
        if after_id is not None:
            clauses.append("e.id > %s")
            params.append(after_id)
        return self._event_page(clauses, params, limit, after_id is None)

    def _event_page(
        self, clauses: list[str], params: list[object], limit: int, descending: bool,
    ) -> tuple[list[RuntimeEvent], int]:
        order = "desc" if descending else "asc"
        with self.pool.connection() as conn:
            rows = conn.execute(
                "select e.id, e.thread_id, e.type, e.payload, e.created_at, "
                "e.turn_id, e.run_id, e.parent_run_id, e.parent_step_id, e.assignment_id "
                "from events e where " + " and ".join(clauses)
                + f" order by e.id {order} limit %s", (*params, limit + 1),
            ).fetchall()
        if descending:
            rows.reverse()
        events = [RuntimeEvent(
            id=int(row[0]), thread_id=_text(row[1]), type=_text(row[2]),
            payload=dict(row[3]), created_at=_timestamp(row[4]) or "",
            turn_id=_optional_text(row[5]), run_id=_optional_text(row[6]),
            parent_run_id=_optional_text(row[7]), parent_step_id=_optional_text(row[8]),
            assignment_id=_optional_text(row[9]),
        ) for row in rows]
        return events, events[-1].id if events else 0

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
       next_retry_at, retry_backoff_seconds, started_at, completed_at,
       artifact_ids, updated_at
from tool_executions
"""

_CONTROL_SELECT = """
select operation_id, idempotency_key, run_id, operation, status,
       request_hash, request_json, result_json, error_json,
       created_at, completed_at, schema_version
from control_operations
"""

_RUN_STATE_SELECT = """
state_json, delivery_attempt, failure_queued_at, last_delivery_failure,
last_delivery_failed_at, last_delivery_worker_id, last_delivery_fencing_token
"""

_POSTGRES_RUN_ROWS = """
with current_runs as (
    select run_id, thread_id, turn_id, status, parent_run_id, created_at, updated_at,
        coalesce(state_json ->> 'run_kind', 'agent') run_kind,
        coalesce(state_json ->> 'execution_strategy', 'react') execution_strategy,
        state_json ->> 'parent_step_id' parent_step_id,
        created_at started_at,
        case when status in ('COMPLETED','FAILED','CANCELLED') then updated_at end completed_at
    from runs
)
"""

_POSTGRES_MESSAGE_FILTER = """
e.type in ('user.message', 'assistant.message')
and e.run_id is not null and e.run_id <> ''
and e.turn_id is not null and e.turn_id <> ''
and e.parent_run_id is null and e.payload ->> 'parent_run_id' is null
and (e.payload ->> 'thread_id' is null or e.payload ->> 'thread_id' = e.thread_id)
and (e.payload ->> 'run_id' is null or e.payload ->> 'run_id' = e.run_id)
and (e.payload ->> 'turn_id' is null or e.payload ->> 'turn_id' = e.turn_id)
and jsonb_typeof(e.payload -> 'text') = 'string' and e.payload ->> 'text' <> ''
and exists (
    select 1 from runs r where r.run_id = e.run_id and r.thread_id = e.thread_id
    and r.turn_id = e.turn_id and r.parent_run_id is null
    and (e.type = 'user.message' or r.status = 'COMPLETED')
)
and not exists (
    select 1 from events duplicate where duplicate.thread_id = e.thread_id
    and duplicate.run_id = e.run_id and duplicate.turn_id = e.turn_id
    and duplicate.type = e.type
    and ((duplicate.payload ->> 'text') is distinct from (e.payload ->> 'text')
         or duplicate.id < e.id)
)
and (e.type = 'user.message' or exists (
    select 1 from events u where u.thread_id = e.thread_id and u.run_id = e.run_id
    and u.turn_id = e.turn_id and u.type = 'user.message' and u.id < e.id
    and u.parent_run_id is null and u.payload ->> 'parent_run_id' is null
    and jsonb_typeof(u.payload -> 'text') = 'string' and u.payload ->> 'text' <> ''
    and not exists (
        select 1 from events ambiguous where ambiguous.thread_id = u.thread_id
        and ambiguous.run_id = u.run_id and ambiguous.turn_id = u.turn_id
        and ambiguous.type = 'user.message'
        and (ambiguous.payload ->> 'text') is distinct from (u.payload ->> 'text')
    )
))
"""


def _checkpoint(value: object) -> Checkpoint:
    data = value if isinstance(value, dict) else json.loads(str(value))
    if not isinstance(data, dict):
        raise ValueError("checkpoint payload must be a JSON object")
    return Checkpoint.from_dict(data)


def _checkpoint_with_delivery(row: Sequence[object]) -> Checkpoint:
    state = _checkpoint(row[0])
    state.delivery_attempt = int(row[1])
    state.failure_queued_at = _timestamp(row[2])
    state.last_delivery_failure = _optional_text(row[3])
    state.last_delivery_failed_at = _timestamp(row[4])
    state.last_delivery_worker_id = _optional_text(row[5])
    state.last_delivery_fencing_token = int(row[6]) if row[6] is not None else None
    return state


def _tool_record(row: Sequence[object]) -> ToolExecutionRecord:
    return ToolExecutionRecord.from_dict(
        {
            "invocation_id": _text(row[0]),
            "run_id": _text(row[1]),
            "tool_call_id": _text(row[2]),
            "tool_name": _text(row[3]),
            "arguments_hash": _text(row[4]),
            "status": _text(row[5]),
            "attempt": row[6],
            "result": _optional_text(row[7]),
            "is_error": bool(row[8]),
            "error": _optional_text(row[9]),
            "last_failure_category": _optional_text(row[10]),
            "last_error_code": _optional_text(row[11]),
            "retry_state": _text(row[12]),
            "retry_suppressed_reason": _optional_text(row[13]),
            "next_retry_at": _timestamp(row[14]),
            "retry_backoff_seconds": row[15],
            "started_at": _timestamp(row[16]),
            "completed_at": _timestamp(row[17]),
            "artifact_ids": row[18] if isinstance(row[18], list) else [],
            "updated_at": _timestamp(row[19]),
        }
    )


def _control_record(row: Sequence[object]) -> ControlOperationRecord:
    return ControlOperationRecord(
        operation_id=_text(row[0]),
        idempotency_key=_text(row[1]),
        run_id=_text(row[2]),
        operation=ControlOperationName(_text(row[3])),
        status=ControlOperationStatus(_text(row[4])),
        request=dict(row[6]),
        request_hash=_text(row[5]),
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
    return _text(value)


def _text(value: object) -> str:
    """Normalize PostgreSQL text values across psycopg text/binary protocols."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _optional_text(value: object) -> str | None:
    return _text(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _ownership(row, *, takeover: bool | None = None) -> RunOwnership:
    lease_until = row[3]
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=UTC)
    token = int(row[2])
    return RunOwnership(
        run_id=_text(row[0]),
        worker_id=_text(row[1]),
        fencing_token=token,
        lease_until=lease_until.astimezone(UTC),
        takeover=token > 1 if takeover is None else takeover,
        delivery_attempt=int(row[4]) if len(row) > 4 else 0,
    )


def _mark_delivery_exhausted(
    conn,
    candidate,
    *,
    max_run_delivery_attempts: int,
) -> None:
    state = _checkpoint(candidate[3])
    expected_sequence = int(candidate[4])
    now = _timestamp(conn.execute("select current_timestamp").fetchone()[0])
    if now is None:  # pragma: no cover - PostgreSQL current_timestamp is non-null
        raise RuntimeError("PostgreSQL did not return current time")
    state.status = RunStatus.FAILED
    state.delivery_attempt = int(candidate[2])
    state.failure_queued_at = now
    state.last_delivery_failure = "lease_expired"
    state.last_delivery_failed_at = now
    state.last_delivery_worker_id = _optional_text(candidate[1])
    state.last_delivery_fencing_token = int(candidate[9])
    state.error = RunError(
        type=RUN_DELIVERY_EXHAUSTED,
        message="automatic Run delivery allowance exhausted after Worker lease loss",
        step="run_delivery",
        metadata={
            "delivery_attempt": state.delivery_attempt,
            "max_run_delivery_attempts": max_run_delivery_attempts,
            "last_delivery_failure": "lease_expired",
            "last_delivery_worker_id": state.last_delivery_worker_id,
            "last_delivery_fencing_token": state.last_delivery_fencing_token,
        },
    )
    next_sequence = expected_sequence + 1
    payload = state.to_dict()
    payload["sequence"] = next_sequence
    payload["updated_at"] = now
    row = conn.execute(
        """
        update runs
        set status = 'FAILED', current_sequence = %s, state_json = %s::jsonb,
            updated_at = %s, runnable = false, owner_worker_id = null,
            lease_until = null, fencing_token = fencing_token + 1,
            failure_queued_at = %s, last_delivery_failure = 'lease_expired',
            last_delivery_failed_at = %s, last_delivery_worker_id = %s,
            last_delivery_fencing_token = %s
        where run_id = %s and current_sequence = %s
        returning run_id
        """,
        (
            next_sequence,
            _json(payload),
            now,
            now,
            now,
            state.last_delivery_worker_id,
            state.last_delivery_fencing_token,
            state.run_id,
            expected_sequence,
        ),
    ).fetchone()
    if row is None:
        raise CheckpointConflictError(f"stale delivery exhaustion transition for {state.run_id}")
    conn.execute(
        """
        insert into checkpoints(
            run_id, sequence, schema_version, thread_id, turn_id,
            status, state_json, created_at
        ) values (%s, %s, %s, %s, %s, 'FAILED', %s::jsonb, %s)
        """,
        (
            state.run_id,
            next_sequence,
            state.schema_version,
            state.thread_id,
            state.turn_id,
            _json(payload),
            now,
        ),
    )
    if candidate[8] is not None:
        conn.execute(
            """
            update runs
            set runnable = true
            where run_id = %s and status = 'WAITING_CHILD'
              and owner_worker_id is null
            """,
            (candidate[8],),
        )


def _delivery_is_exhausted(candidate, max_run_delivery_attempts: int | None) -> bool:
    return bool(
        candidate is not None
        and candidate[1] is not None
        and max_run_delivery_attempts is not None
        and int(candidate[2]) >= max_run_delivery_attempts
    )


def _require_positive_lease(lease_seconds: float) -> None:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")


def _require_positive_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_optional_limit(value: int | None, name: str) -> None:
    if value is not None:
        _require_positive_limit(value, name)


def _lock_capacity_coordination(conn) -> None:
    conn.execute(
        "select singleton from runtime_capacity_coordination where singleton = true for update"
    ).fetchone()


def _lock_thread(conn, thread_id: str) -> None:
    # Direct Runtime callers may create a Run before the API creates its Thread record.
    conn.execute("insert into threads(id, created_at) values (%s, %s) on conflict(id) do nothing",
                 (thread_id, _now()))
    conn.execute("select id from threads where id = %s for update", (thread_id,)).fetchone()


def _check_postgres_thread_available(conn, thread_id: str, run_id: str) -> None:
    states = [_checkpoint(row[0]) for row in conn.execute(
        "select state_json from runs where thread_id = %s and parent_run_id is null "
        "and status in ('RUNNING', 'INTERRUPTED', 'WAITING_APPROVAL', 'WAITING_CHILD') "
        "order by created_at, run_id", (thread_id,),
    ).fetchall()]
    _check_thread_available(states, run_id)


def _lock_run_thread(conn, run_id: str) -> None:
    _lock_capacity_coordination(conn)
    identity = conn.execute(
        "select thread_id, parent_run_id from runs where run_id = %s", (run_id,)
    ).fetchone()
    if identity is not None and identity[1] is None:
        _lock_thread(conn, _text(identity[0]))
        _check_postgres_thread_available(conn, _text(identity[0]), run_id)


def _append_submission_event(conn, state: Checkpoint, event_type: str, payload: dict) -> int:
    payload = {**payload, "thread_id": state.thread_id,
               "run_id": state.run_id, "turn_id": state.turn_id}
    row = conn.execute(
        "insert into events(thread_id, run_id, turn_id, type, payload, created_at) "
        "values (%s, %s, %s, %s, %s::jsonb, %s) returning id",
        (state.thread_id, state.run_id, state.turn_id, event_type, _json(payload), _now()),
    ).fetchone()
    return int(row[0])


def _project_completed_in_transaction(conn, state: Checkpoint) -> tuple[int | None, bool]:
    if state.status != RunStatus.COMPLETED or state.parent_run_id is not None:
        return None, False
    identity = (state.thread_id, state.run_id, state.turn_id)
    if conn.execute(
        "select 1 from events where thread_id = %s and run_id = %s and turn_id = %s "
        "and type = 'user.message'", identity,
    ).fetchone() is None:
        return None, False
    row = conn.execute(
        "select id from events where thread_id = %s and run_id = %s and turn_id = %s "
        "and type = 'assistant.message' order by id limit 1", identity,
    ).fetchone()
    created = row is None
    event_id = int(row[0]) if row else _append_submission_event(
        conn, state, "assistant.message", {"text": state.output_text}
    )
    if conn.execute(
        "select 1 from events where thread_id = %s and run_id = %s and turn_id = %s "
        "and type = 'turn.completed'", identity,
    ).fetchone() is None:
        _append_submission_event(conn, state, "turn.completed", _completed_payload(state))
    return event_id, created


def _queued_count(conn) -> int:
    row = conn.execute(f"select count(*) from runs as candidate where {_RUN_IS_QUEUED}").fetchone()
    return int(row[0])


def _active_count(conn) -> int:
    row = conn.execute(f"select count(*) from runs as candidate where {_RUN_IS_ACTIVE}").fetchone()
    return int(row[0])


def _check_principal_queue_quota(conn, principal_key: str, limit: int | None) -> None:
    if limit is None:
        return
    row = conn.execute(
        f"""
        select count(*) from runs as candidate
        where {_RUN_IS_QUEUED}
          and candidate.parent_run_id is null
          and candidate.state_json ->> 'principal_key' = %s
        """,
        (principal_key,),
    ).fetchone()
    if int(row[0]) >= limit:
        raise ApiError(
            "PRINCIPAL_QUEUE_QUOTA_EXCEEDED",
            "principal queued Run quota exceeded",
            429,
            details={"queued_runs": int(row[0]), "max_queued_runs_per_principal": limit},
        )


def _consume_bucket(
    conn, scope: str, scope_key: str, rate: float, burst: int
) -> tuple[float, float]:
    conn.execute(
        """
        insert into traffic_rate_buckets(scope, scope_key, tokens, last_refill_at)
        values (%s, %s, %s, current_timestamp)
        on conflict(scope, scope_key) do nothing
        """,
        (scope, scope_key, float(burst)),
    )
    row = conn.execute(
        """
        select tokens, extract(epoch from current_timestamp - last_refill_at)
        from traffic_rate_buckets
        where scope = %s and scope_key = %s
        for update
        """,
        (scope, scope_key),
    ).fetchone()
    tokens = min(float(burst), float(row[0]) + max(0.0, float(row[1])) * rate)
    return tokens, rate


def _consume_submission_tokens(
    self,
    principal_key: str,
    global_rate: float | None,
    global_burst: int | None,
    principal_rate: float | None,
    principal_burst: int | None,
) -> float | None:
    with self.pool.connection() as conn:
        return _consume_submission_tokens_in_transaction(
            conn, principal_key, global_rate, global_burst, principal_rate, principal_burst
        )


def _consume_submission_tokens_in_transaction(
    conn,
    principal_key: str,
    global_rate: float | None,
    global_burst: int | None,
    principal_rate: float | None,
    principal_burst: int | None,
) -> float | None:
    buckets: list[tuple[str, str, float, int]] = []
    if global_rate is not None and global_burst is not None:
        buckets.append(("global", "root_submission", global_rate, global_burst))
    if principal_rate is not None and principal_burst is not None:
        buckets.append(("principal", principal_key, principal_rate, principal_burst))
    if not buckets:
        return None
    states = [_consume_bucket(conn, *bucket) for bucket in buckets]
    if any(tokens < 1.0 for tokens, _ in states):
        waits = [
            max(0.0, (1.0 - tokens) / bucket[2])
            for (tokens, _), bucket in zip(states, buckets, strict=True)
        ]
        return max(waits)
    for (scope, scope_key, _rate, _burst), (tokens, _rate_value) in zip(
        buckets, states, strict=True
    ):
        conn.execute(
                """
                update traffic_rate_buckets
                set tokens = %s - 1, last_refill_at = current_timestamp
                where scope = %s and scope_key = %s
                """,
                (tokens, scope, scope_key),
        )
    return None


def _insert_initial_run(conn, checkpoint: Checkpoint, *, updated_at: str, runnable: bool) -> None:
    state = checkpoint.to_dict()
    state["sequence"] = 1
    state["updated_at"] = updated_at
    payload = _json(state)
    conn.execute(
        """
        insert into runs(
            run_id, thread_id, turn_id, status, current_sequence,
            schema_version, state_json, parent_run_id, created_at, updated_at, runnable
        ) values (%s, %s, %s, %s, 1, %s, %s::jsonb, %s, %s, %s, %s)
        """,
        (
            checkpoint.run_id,
            checkpoint.thread_id,
            checkpoint.turn_id,
            checkpoint.status.value,
            checkpoint.schema_version,
            payload,
            checkpoint.parent_run_id,
            checkpoint.created_at,
            updated_at,
            runnable,
        ),
    )
    conn.execute(
        """
        insert into checkpoints(
            run_id, sequence, schema_version, thread_id, turn_id,
            status, state_json, created_at
        ) values (%s, 1, %s, %s, %s, %s, %s::jsonb, %s)
        """,
        (
            checkpoint.run_id,
            checkpoint.schema_version,
            checkpoint.thread_id,
            checkpoint.turn_id,
            checkpoint.status.value,
            payload,
            updated_at,
        ),
    )


def _lock_and_validate_ownership(conn, ownership: RunOwnership) -> None:
    row = conn.execute(
        """
        select 1 from runs
        where run_id = %s and owner_worker_id = %s and fencing_token = %s
          and status in ('RUNNING', 'WAITING_CHILD')
          and lease_until > current_timestamp
        for update
        """,
        (ownership.run_id, ownership.worker_id, ownership.fencing_token),
    ).fetchone()
    if row is None:
        raise OwnershipLostError(
            f"worker {ownership.worker_id} no longer owns run {ownership.run_id}"
        )


_DEADLINE_IS_LIVE = """
(
    coalesce(
        (candidate.state_json -> 'budget_policy' ->> 'max_wall_time_seconds')::double precision,
        0
    ) <= 0
    or candidate.created_at
       + coalesce(
            (candidate.state_json -> 'budget_policy' ->> 'max_wall_time_seconds')::double precision,
            0
         )
         * interval '1 second' > current_timestamp
)
"""

_ANCESTORS_NOT_CANCELLED = """
not exists (
    with recursive ancestors(run_id, parent_run_id, status) as (
        select parent.run_id, parent.parent_run_id, parent.status
        from runs as parent
        where parent.run_id = candidate.parent_run_id
        union all
        select parent.run_id, parent.parent_run_id, parent.status
        from runs as parent
        join ancestors on parent.run_id = ancestors.parent_run_id
    )
    select 1 from ancestors where status = 'CANCELLED'
)
"""

_RUN_IS_CLAIMABLE = f"""
candidate.runnable
and candidate.status in ('RUNNING', 'WAITING_CHILD')
and (candidate.owner_worker_id is null or candidate.lease_until <= current_timestamp)
and {_DEADLINE_IS_LIVE}
and {_ANCESTORS_NOT_CANCELLED}
"""

_RUN_IS_QUEUED = _RUN_IS_CLAIMABLE

_RUN_IS_ACTIVE = """
candidate.status in ('RUNNING', 'WAITING_CHILD')
and candidate.owner_worker_id is not null
and candidate.lease_until > current_timestamp
"""


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
        updated_at timestamptz not null,
        runnable boolean not null default false,
        owner_worker_id text,
        lease_until timestamptz,
        fencing_token bigint not null default 0,
        claimed_at timestamptz,
        last_heartbeat_at timestamptz,
        delivery_attempt integer not null default 0,
        last_delivery_failure text,
        last_delivery_failed_at timestamptz,
        last_delivery_worker_id text,
        last_delivery_fencing_token bigint,
        failure_queued_at timestamptz
    )
    """,
    "create index if not exists idx_runs_thread on runs(thread_id, created_at, run_id)",
    "create index if not exists idx_runs_created on runs(created_at, run_id)",
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
    create table if not exists submission_idempotency (
        thread_id text not null,
        idempotency_key text not null,
        request_fingerprint text not null,
        run_id text not null references runs(run_id) on delete cascade,
        created_at timestamptz not null,
        primary key(thread_id, idempotency_key),
        unique(run_id)
    )
    """,
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
        artifact_ids jsonb not null default '[]'::jsonb,
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
    "create index if not exists idx_events_messages on events(thread_id, id) "
    "where type in ('user.message', 'assistant.message')",
    "create index if not exists idx_events_message_identity "
    "on events(thread_id, run_id, turn_id, type, id) "
    "where type in ('user.message', 'assistant.message')",
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
    create table if not exists runtime_capacity_coordination (
        singleton boolean primary key default true check(singleton),
        created_at timestamptz not null default current_timestamp
    )
    """,
    """
    insert into runtime_capacity_coordination(singleton)
    values (true)
    on conflict(singleton) do nothing
    """,
    """
    create table if not exists traffic_rate_buckets (
        scope text not null,
        scope_key text not null,
        tokens double precision not null,
        last_refill_at timestamptz not null,
        primary key(scope, scope_key)
    )
    """,
    """
    create index if not exists idx_control_operations_interrupt
    on control_operations(run_id, operation, status)
    """,
)

_OWNERSHIP_MIGRATION_STATEMENTS = (
    "alter table tool_executions add column if not exists "
    "artifact_ids jsonb not null default '[]'::jsonb",
    "alter table runs add column if not exists runnable boolean not null default false",
    "alter table runs add column if not exists owner_worker_id text",
    "alter table runs add column if not exists lease_until timestamptz",
    "alter table runs add column if not exists fencing_token bigint not null default 0",
    "alter table runs add column if not exists claimed_at timestamptz",
    "alter table runs add column if not exists last_heartbeat_at timestamptz",
    "alter table runs add column if not exists delivery_attempt integer not null default 0",
    "alter table runs add column if not exists last_delivery_failure text",
    "alter table runs add column if not exists last_delivery_failed_at timestamptz",
    "alter table runs add column if not exists last_delivery_worker_id text",
    "alter table runs add column if not exists last_delivery_fencing_token bigint",
    "alter table runs add column if not exists failure_queued_at timestamptz",
    """
    create index if not exists idx_runs_claimable
    on runs(runnable, status, lease_until, created_at, run_id)
    """,
    "create index if not exists idx_runs_failure_queue on runs(failure_queued_at, run_id)",
)
