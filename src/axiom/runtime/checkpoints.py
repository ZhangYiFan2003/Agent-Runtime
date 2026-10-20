from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from axiom.config import TrafficGovernanceConfig
from axiom.runtime.capacity import AdmissionRejectedError, CapacitySnapshot
from axiom.runtime.control_plane import ApiError
from axiom.runtime.events import ThreadEventRepository
from axiom.runtime.models import (
    BudgetLedgerRecord,
    Checkpoint,
    RunState,
    RunStatus,
    ToolExecutionRecord,
)
from axiom.runtime.ownership import RunOwnership
from axiom.types import Message

OCCUPYING_RUN_STATUSES = frozenset({
    RunStatus.RUNNING, RunStatus.INTERRUPTED, RunStatus.WAITING_APPROVAL, RunStatus.WAITING_CHILD,
})


class CheckpointConflictError(RuntimeError):
    """Raised when a stale worker attempts to advance a run."""


class BudgetLedgerConflictError(RuntimeError):
    """Raised when an atomic budget ledger update loses a CAS race."""


class RunStateStore(Protocol):
    """Persistence contract for the current and historical durable state of Runs."""

    async def save(
        self, checkpoint: Checkpoint, *, ownership: RunOwnership | None = None
    ) -> None: ...

    async def load(self, run_id: str) -> Checkpoint | None: ...

    async def list(self, thread_id: str) -> list[Checkpoint]: ...


class ToolExecutionStore(Protocol):
    async def save_tool_execution(
        self, record: ToolExecutionRecord, *, ownership: RunOwnership | None = None
    ) -> None: ...

    async def load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None: ...

    async def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]: ...


CheckpointStore = RunStateStore


class RuntimeStore(RunStateStore, ToolExecutionStore, Protocol):
    backend: str

    async def thread_run_head(self, thread_id: str) -> dict[str, Any] | None: ...

    async def list_run_summaries(
        self, *, limit: int = 50,
        position: tuple[str | int | float, str, str] | None = None,
        search: str = "", status: str = "", strategy: str = "", sort: str = "newest",
    ) -> list[dict[str, Any]]: ...

    async def lookup_submission(
        self, thread_id: str, idempotency_key: str, request_fingerprint: str
    ) -> Checkpoint | None: ...

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
    ) -> tuple[Checkpoint, bool]: ...

    async def project_completed_turn(self, checkpoint: Checkpoint) -> tuple[int | None, bool]: ...

    async def load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None: ...

    async def save_budget_ledger(
        self, record: BudgetLedgerRecord, *, ownership: RunOwnership | None = None
    ) -> None: ...

    async def validate_ownership(self, ownership: RunOwnership) -> None: ...


class DistributedRuntimeStore(RuntimeStore, Protocol):
    async def admit_run(
        self,
        checkpoint: Checkpoint,
        max_queued_runs: int | None = None,
        *,
        max_queued_runs_per_principal: int | None = None,
    ) -> None: ...

    async def requeue_delivery_exhausted(self, run_id: str) -> Checkpoint: ...

    async def mark_runnable(self, run_id: str) -> None: ...

    async def claim_run(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: float,
        max_active_runs: int | None = None,
        max_active_runs_per_principal: int | None = None,
        max_run_delivery_attempts: int | None = None,
    ) -> RunOwnership | None: ...

    async def claim_next(
        self,
        worker_id: str,
        lease_seconds: float,
        max_active_runs: int | None = None,
        max_active_runs_per_principal: int | None = None,
        max_run_delivery_attempts: int | None = None,
    ) -> RunOwnership | None: ...

    async def renew_lease(
        self, ownership: RunOwnership, lease_seconds: float
    ) -> RunOwnership | None: ...

    async def release_lease(
        self, ownership: RunOwnership, *, runnable: bool = True
    ) -> bool: ...

    async def get_ownership(self, run_id: str) -> RunOwnership | None: ...

    async def capacity_snapshot(
        self,
        *,
        max_queued_runs: int | None = None,
        max_active_runs: int | None = None,
    ) -> CapacitySnapshot: ...


async def load_run_state(store: RunStateStore, run_id: str) -> RunState | None:
    """Load recovery authority for a Run without implying a separate domain entity."""
    return await store.load(run_id)


async def advance_run_state(
    store: RunStateStore,
    state: RunState,
    *,
    ownership: RunOwnership | None = None,
) -> None:
    """CAS-advance one Run's versioned durable state."""
    if ownership is None:
        await store.save(state)
    else:
        await store.save(state, ownership=ownership)


class MemoryCheckpointStore:
    backend = "memory"

    def __init__(self) -> None:
        self._checkpoints: dict[str, list[dict[str, object]]] = {}
        self._tool_executions: dict[str, dict[str, object]] = {}
        self._budget_ledgers: dict[str, dict[str, object]] = {}
        self._lock = asyncio.Lock()

    async def save(
        self, checkpoint: Checkpoint, *, ownership: RunOwnership | None = None
    ) -> None:
        if ownership is not None:
            raise ValueError("distributed ownership requires PostgreSQL storage")
        async with self._lock:
            rows = self._checkpoints.setdefault(checkpoint.run_id, [])
            latest = int(rows[-1]["sequence"]) if rows else 0
            if latest != checkpoint.sequence:
                raise CheckpointConflictError(
                    f"stale checkpoint for {checkpoint.run_id}: expected {latest}, "
                    f"got {checkpoint.sequence}"
                )
            checkpoint.sequence += 1
            checkpoint.updated_at = _now()
            rows.append(deepcopy(checkpoint.to_dict()))

    async def load(self, run_id: str) -> Checkpoint | None:
        async with self._lock:
            rows = self._checkpoints.get(run_id)
            return Checkpoint.from_dict(deepcopy(rows[-1])) if rows else None

    async def list(self, thread_id: str) -> list[Checkpoint]:
        async with self._lock:
            checkpoints = [
                Checkpoint.from_dict(deepcopy(rows[-1]))
                for rows in self._checkpoints.values()
                if rows and rows[-1].get("thread_id") == thread_id
            ]
        return sorted(checkpoints, key=lambda checkpoint: checkpoint.created_at)

    async def save_tool_execution(
        self, record: ToolExecutionRecord, *, ownership: RunOwnership | None = None
    ) -> None:
        if ownership is not None:
            raise ValueError("distributed ownership requires PostgreSQL storage")
        async with self._lock:
            current = self._tool_executions.get(record.invocation_id)
            if current and current.get("arguments_hash") != record.arguments_hash:
                raise ValueError("invocation_id reused with different tool arguments")
            record.updated_at = _now()
            self._tool_executions[record.invocation_id] = deepcopy(record.to_dict())

    async def load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None:
        async with self._lock:
            row = self._tool_executions.get(invocation_id)
            return ToolExecutionRecord.from_dict(deepcopy(row)) if row else None

    async def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        async with self._lock:
            rows = [
                ToolExecutionRecord.from_dict(deepcopy(row))
                for row in self._tool_executions.values()
                if row.get("run_id") == run_id
            ]
        return sorted(rows, key=lambda record: record.invocation_id)

    async def load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None:
        async with self._lock:
            row = self._budget_ledgers.get(owner_run_id)
            if row is None:
                return None
            return BudgetLedgerRecord(
                owner_run_id=owner_run_id,
                version=int(row["version"]),
                state=deepcopy(row["state"]),
            )

    async def save_budget_ledger(
        self, record: BudgetLedgerRecord, *, ownership: RunOwnership | None = None
    ) -> None:
        if ownership is not None:
            raise ValueError("distributed ownership requires PostgreSQL storage")
        async with self._lock:
            current = self._budget_ledgers.get(record.owner_run_id)
            current_version = int(current["version"]) if current else 0
            if current_version != record.version:
                raise BudgetLedgerConflictError(
                    f"stale budget ledger for {record.owner_run_id}: "
                    f"expected {current_version}, got {record.version}"
                )
            record.version += 1
            self._budget_ledgers[record.owner_run_id] = {
                "version": record.version,
                "state": deepcopy(record.state),
            }


class SQLiteCheckpointStore:
    backend = "sqlite"

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        ThreadEventRepository(self.db_path)

    async def thread_run_head(self, thread_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._thread_run_head, thread_id)

    def _thread_run_head(self, thread_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                _SQLITE_RUN_ROWS + "select run_id, status from current_runs "
                "where thread_id = ? and parent_run_id is null "
                "order by case when status in "
                "('RUNNING','INTERRUPTED','WAITING_APPROVAL','WAITING_CHILD') "
                "then 0 else 1 end, created_at desc, run_id desc limit 1", (thread_id,),
            ).fetchone()
        return {"run_id": str(row[0]), "status": str(row[1])} if row else None

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
        clauses, params = _run_list_filters(search, status, strategy, "?")
        sort_expression, direction = _run_list_order(sort, "sqlite")
        if position is not None:
            value, created_at, run_id = position
            comparison = ">" if direction == "asc" else "<"
            if sort in {"newest", "oldest"}:
                clauses.append(f"(created_at, run_id) {comparison} (?, ?)")
                params.extend((created_at, run_id))
            else:
                clauses.append(
                    f"({sort_expression} {comparison} ? or ({sort_expression} = ? "
                    "and (created_at, run_id) < (?, ?)))"
                )
                params.extend((value, value, created_at, run_id))
        order = (f"{sort_expression} {direction}, "
                 + ("created_at desc, " if sort in {"status", "duration"} else "")
                 + f"run_id {'asc' if sort == 'oldest' else 'desc'}")
        query = (_SQLITE_RUN_ROWS + "select " + ", ".join(_RUN_SUMMARY_FIELDS)
                 + f", {sort_expression} as _cursor_value from current_runs where "
                 + " and ".join(clauses) + f" order by {order} limit ?")
        with self._connect() as conn:
            conn.execute("begin")
            rows = conn.execute(query, (*params, limit + 1)).fetchall()
            summaries = [dict(zip((*_RUN_SUMMARY_FIELDS, "_cursor_value"), row, strict=True))
                         for row in rows]
            if summaries:
                ids = [item["run_id"] for item in summaries]
                placeholders = ",".join("?" for _ in ids)
                counts = conn.execute(
                    _SQLITE_RUN_ROWS + _child_counts_sql(placeholders), ids,
                ).fetchall()
                _attach_child_counts(summaries, counts)
        return summaries

    async def lookup_submission(
        self, thread_id: str, idempotency_key: str, request_fingerprint: str
    ) -> Checkpoint | None:
        return await asyncio.to_thread(
            self._lookup_submission, thread_id, idempotency_key, request_fingerprint
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
        return await asyncio.to_thread(
            self._admit_submission, checkpoint, idempotency_key, request_fingerprint,
            max_queued_runs, max_queued_runs_per_principal, traffic,
        )

    async def project_completed_turn(self, checkpoint: Checkpoint) -> tuple[int | None, bool]:
        return await asyncio.to_thread(self._project_completed_turn, checkpoint.run_id)

    def _lookup_submission(
        self, thread_id: str, idempotency_key: str, request_fingerprint: str
    ) -> Checkpoint | None:
        with self._connect() as conn:
            return self._lookup_submission_in_transaction(
                conn, thread_id, idempotency_key, request_fingerprint
            )

    def _lookup_submission_in_transaction(
        self, conn: sqlite3.Connection, thread_id: str, idempotency_key: str,
        request_fingerprint: str,
    ) -> Checkpoint | None:
        row = conn.execute(
            "select request_fingerprint, run_id from submission_idempotency "
            "where thread_id = ? and idempotency_key = ?",
            (thread_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        _check_submission_fingerprint(str(row[0]), request_fingerprint)
        state = self._load_in_transaction(conn, str(row[1]))
        if state is None:
            raise RuntimeError("submission idempotency mapping references a missing Run")
        return state

    def _admit_submission(
        self, checkpoint: Checkpoint, idempotency_key: str | None, request_fingerprint: str,
        max_queued_runs: int | None, max_queued_runs_per_principal: int | None,
        traffic: TrafficGovernanceConfig | None,
    ) -> tuple[Checkpoint, bool]:
        if checkpoint.sequence != 0 or checkpoint.parent_run_id is not None:
            raise ValueError("only a new root Run may pass submission admission")
        with self._connect() as conn:
            conn.execute("begin immediate")
            if idempotency_key is not None:
                existing = self._lookup_submission_in_transaction(
                    conn, checkpoint.thread_id, idempotency_key, request_fingerprint
                )
                if existing is not None:
                    return existing, False
            previous = self._list_in_transaction(conn, checkpoint.thread_id)
            _check_thread_available(previous, checkpoint.run_id)
            all_states = [
                _decode_checkpoint(row[0]) for row in conn.execute(
                    "select c.state_json from checkpoints c join "
                    "(select run_id, max(sequence) sequence from checkpoints "
                    "group by run_id) latest "
                    "on latest.run_id = c.run_id and latest.sequence = c.sequence"
                ).fetchall()
            ]
            queued = sum(state.status == RunStatus.RUNNING for state in all_states)
            if max_queued_runs is not None and queued >= max_queued_runs:
                raise AdmissionRejectedError(queued_runs=queued, max_queued_runs=max_queued_runs)
            principal_queued = sum(
                state.status == RunStatus.RUNNING and state.parent_run_id is None
                and state.principal_key == checkpoint.principal_key for state in all_states
            )
            if (max_queued_runs_per_principal is not None
                    and principal_queued >= max_queued_runs_per_principal):
                raise ApiError(
                    "PRINCIPAL_QUEUE_QUOTA_EXCEEDED", "principal queued Run quota exceeded", 429,
                    details={"queued_runs": principal_queued,
                             "max_queued_runs_per_principal": max_queued_runs_per_principal},
                )
            self._consume_submission_in_transaction(conn, checkpoint.principal_key, traffic)
            for state in previous:
                self._project_completed_in_transaction(conn, state)
            admitted = replace(
                checkpoint, messages=_submission_messages(previous, checkpoint.input)
            )
            now = _now()
            conn.execute("insert or ignore into threads(id, created_at) values (?, ?)",
                         (checkpoint.thread_id, now))
            self._write_checkpoint(conn, admitted, sequence=1, updated_at=now)
            if idempotency_key is not None:
                conn.execute(
                    "insert into submission_idempotency(thread_id, idempotency_key, "
                    "request_fingerprint, run_id, created_at) values (?, ?, ?, ?, ?)",
                    (checkpoint.thread_id, idempotency_key, request_fingerprint,
                     checkpoint.run_id, now),
                )
            for event_type, payload in _submission_events(checkpoint):
                self._append_event(conn, checkpoint, event_type, payload)
        checkpoint.sequence = 1
        checkpoint.updated_at = now
        checkpoint.messages = admitted.messages
        return checkpoint, True

    @staticmethod
    def _consume_submission_in_transaction(
        conn: sqlite3.Connection, principal_key: str, traffic: TrafficGovernanceConfig | None,
    ) -> None:
        now = time.time()
        states = []
        for scope, scope_key, rate, burst in _submission_buckets(principal_key, traffic):
            row = conn.execute(
                "select tokens, last_refill_at from traffic_rate_buckets "
                "where scope = ? and scope_key = ?", (scope, scope_key),
            ).fetchone()
            tokens = (
                min(float(burst), float(row[0]) + max(0.0, now - float(row[1])) * rate)
                if row else float(burst)
            )
            if tokens < 1:
                _raise_submission_rate_limit((1 - tokens) / rate)
            states.append((scope, scope_key, tokens))
        for scope, scope_key, tokens in states:
            conn.execute(
                "insert into traffic_rate_buckets(scope, scope_key, tokens, last_refill_at) "
                "values (?, ?, ?, ?) on conflict(scope, scope_key) do update "
                "set tokens = excluded.tokens, last_refill_at = excluded.last_refill_at",
                (scope, scope_key, tokens - 1, now),
            )

    def _project_completed_turn(self, run_id: str) -> tuple[int | None, bool]:
        with self._connect() as conn:
            conn.execute("begin immediate")
            state = self._load_in_transaction(conn, run_id)
            return self._project_completed_in_transaction(conn, state) if state else (None, False)

    def _project_completed_in_transaction(
        self, conn: sqlite3.Connection, state: Checkpoint,
    ) -> tuple[int | None, bool]:
        if state.status != RunStatus.COMPLETED or state.parent_run_id is not None:
            return None, False
        if conn.execute(
            "select 1 from events where thread_id = ? and run_id = ? and turn_id = ? "
            "and type = 'user.message'", (state.thread_id, state.run_id, state.turn_id),
        ).fetchone() is None:
            return None, False
        row = conn.execute(
            "select id from events where thread_id = ? and run_id = ? and turn_id = ? "
            "and type = 'assistant.message' order by id limit 1",
            (state.thread_id, state.run_id, state.turn_id),
        ).fetchone()
        created = row is None
        event_id = int(row[0]) if row else self._append_event(
            conn, state, "assistant.message", {"text": state.output_text}
        )
        if conn.execute(
            "select 1 from events where thread_id = ? and run_id = ? and turn_id = ? "
            "and type = 'turn.completed'", (state.thread_id, state.run_id, state.turn_id),
        ).fetchone() is None:
            self._append_event(conn, state, "turn.completed", _completed_payload(state))
        return event_id, created

    @staticmethod
    def _append_event(
        conn: sqlite3.Connection, state: Checkpoint, event_type: str, payload: dict,
    ) -> int:
        payload = {**payload, "thread_id": state.thread_id,
                   "run_id": state.run_id, "turn_id": state.turn_id}
        cursor = conn.execute(
            "insert into events(thread_id, run_id, turn_id, type, payload, created_at) "
            "values (?, ?, ?, ?, ?, ?)",
            (state.thread_id, state.run_id, state.turn_id, event_type,
             json.dumps(payload, ensure_ascii=False), _now()),
        )
        return int(cursor.lastrowid)

    async def save(
        self, checkpoint: Checkpoint, *, ownership: RunOwnership | None = None
    ) -> None:
        if ownership is not None:
            raise ValueError("distributed ownership requires PostgreSQL storage")
        await asyncio.to_thread(self._save, checkpoint)

    async def load(self, run_id: str) -> Checkpoint | None:
        return await asyncio.to_thread(self._load, run_id)

    async def list(self, thread_id: str) -> list[Checkpoint]:
        return await asyncio.to_thread(self._list, thread_id)

    async def save_tool_execution(
        self, record: ToolExecutionRecord, *, ownership: RunOwnership | None = None
    ) -> None:
        if ownership is not None:
            raise ValueError("distributed ownership requires PostgreSQL storage")
        await asyncio.to_thread(self._save_tool_execution, record)

    async def load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None:
        return await asyncio.to_thread(self._load_tool_execution, invocation_id)

    async def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        return await asyncio.to_thread(self._list_tool_executions, run_id)

    async def load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None:
        return await asyncio.to_thread(self._load_budget_ledger, owner_run_id)

    async def save_budget_ledger(
        self, record: BudgetLedgerRecord, *, ownership: RunOwnership | None = None
    ) -> None:
        if ownership is not None:
            raise ValueError("distributed ownership requires PostgreSQL storage")
        await asyncio.to_thread(self._save_budget_ledger, record)

    def _save(self, checkpoint: Checkpoint) -> None:
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select max(sequence) from checkpoints where run_id = ?",
                (checkpoint.run_id,),
            ).fetchone()
            latest = int(row[0]) if row and row[0] is not None else 0
            if latest != checkpoint.sequence:
                raise CheckpointConflictError(
                    f"stale checkpoint for {checkpoint.run_id}: expected {latest}, "
                    f"got {checkpoint.sequence}"
                )
            if checkpoint.parent_run_id is None and checkpoint.status in OCCUPYING_RUN_STATUSES:
                _check_thread_available(
                    self._list_in_transaction(conn, checkpoint.thread_id), checkpoint.run_id
                )
            next_sequence = latest + 1
            now = _now()
            self._write_checkpoint(conn, checkpoint, sequence=next_sequence, updated_at=now)
            self._project_completed_in_transaction(conn, checkpoint)
        checkpoint.sequence = next_sequence
        checkpoint.updated_at = now

    @staticmethod
    def _write_checkpoint(
        conn: sqlite3.Connection, checkpoint: Checkpoint, *, sequence: int, updated_at: str,
    ) -> None:
        state = checkpoint.to_dict()
        state.update(sequence=sequence, updated_at=updated_at)
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        conn.execute(
                """
                insert into checkpoints(
                    run_id, sequence, schema_version, thread_id, turn_id,
                    status, state_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.run_id,
                    sequence,
                    checkpoint.schema_version,
                    checkpoint.thread_id,
                    checkpoint.turn_id,
                    checkpoint.status.value,
                    payload,
                    updated_at,
                ),
            )

    def _load(self, run_id: str) -> Checkpoint | None:
        with self._connect() as conn:
            return self._load_in_transaction(conn, run_id)

    @staticmethod
    def _load_in_transaction(conn: sqlite3.Connection, run_id: str) -> Checkpoint | None:
        row = conn.execute(
                """
                select state_json from checkpoints
                where run_id = ? order by sequence desc limit 1
                """,
                (run_id,),
            ).fetchone()
        return _decode_checkpoint(row[0]) if row else None

    def _list(self, thread_id: str) -> list[Checkpoint]:
        with self._connect() as conn:
            return self._list_in_transaction(conn, thread_id)

    @staticmethod
    def _list_in_transaction(conn: sqlite3.Connection, thread_id: str) -> list[Checkpoint]:
        rows = conn.execute(
                """
                select checkpoint.state_json
                from checkpoints as checkpoint
                join (
                    select run_id, max(sequence) as sequence
                    from checkpoints where thread_id = ? group by run_id
                ) as latest
                  on latest.run_id = checkpoint.run_id
                 and latest.sequence = checkpoint.sequence
                order by checkpoint.created_at
                """,
                (thread_id,),
            ).fetchall()
        return [_decode_checkpoint(row[0]) for row in rows]

    def _save_tool_execution(self, record: ToolExecutionRecord) -> None:
        record.updated_at = _now()
        with self._connect() as conn:
            conn.execute("begin immediate")
            current = conn.execute(
                "select arguments_hash from tool_executions where invocation_id = ?",
                (record.invocation_id,),
            ).fetchone()
            if current and str(current[0]) != record.arguments_hash:
                raise ValueError("invocation_id reused with different tool arguments")
            values: Sequence[object] = (
                record.invocation_id,
                record.run_id,
                record.tool_call_id,
                record.tool_name,
                record.arguments_hash,
                record.status.value,
                record.attempt,
                record.result,
                int(record.is_error),
                record.error,
                record.last_failure_category,
                record.last_error_code,
                record.retry_state.value,
                record.retry_suppressed_reason,
                record.next_retry_at,
                record.retry_backoff_seconds,
                record.started_at,
                record.completed_at,
                json.dumps(record.artifact_ids, separators=(",", ":")),
                record.updated_at,
            )
            conn.execute(
                """
                insert into tool_executions(
                    invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                    status, attempt, result, is_error, error, last_failure_category,
                    last_error_code, retry_state, retry_suppressed_reason,
                    next_retry_at, retry_backoff_seconds, started_at, completed_at,
                    artifact_ids_json, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    artifact_ids_json = excluded.artifact_ids_json,
                    updated_at = excluded.updated_at
                """,
                values,
            )

    def _load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                       status, attempt, result, is_error, error, last_failure_category,
                       last_error_code, retry_state, retry_suppressed_reason,
                       next_retry_at, retry_backoff_seconds, started_at, completed_at,
                       artifact_ids_json, updated_at
                from tool_executions where invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
        if not row:
            return None
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
                "next_retry_at": row[14],
                "retry_backoff_seconds": row[15],
                "started_at": row[16],
                "completed_at": row[17],
                "artifact_ids": json.loads(str(row[18] or "[]")),
                "updated_at": row[19],
            }
        )

    def _list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                       status, attempt, result, is_error, error, last_failure_category,
                       last_error_code, retry_state, retry_suppressed_reason,
                       next_retry_at, retry_backoff_seconds, started_at, completed_at,
                       artifact_ids_json, updated_at
                from tool_executions where run_id = ? order by invocation_id
                """,
                (run_id,),
            ).fetchall()
        return [
            ToolExecutionRecord.from_dict(
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
                    "next_retry_at": row[14],
                    "retry_backoff_seconds": row[15],
                    "started_at": row[16],
                    "completed_at": row[17],
                    "artifact_ids": json.loads(str(row[18] or "[]")),
                    "updated_at": row[19],
                }
            )
            for row in rows
        ]

    def _load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "select version, state_json from run_budget_ledgers where owner_run_id = ?",
                (owner_run_id,),
            ).fetchone()
        if not row:
            return None
        state = json.loads(str(row[1]))
        if not isinstance(state, dict):
            raise ValueError("budget ledger state must be a JSON object")
        return BudgetLedgerRecord(owner_run_id=owner_run_id, version=int(row[0]), state=state)

    def _save_budget_ledger(self, record: BudgetLedgerRecord) -> None:
        payload = json.dumps(record.state, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute("begin immediate")
            if record.version == 0:
                try:
                    conn.execute(
                        "insert into run_budget_ledgers(owner_run_id, version, state_json) "
                        "values (?, 1, ?)",
                        (record.owner_run_id, payload),
                    )
                except sqlite3.IntegrityError as exc:
                    raise BudgetLedgerConflictError(
                        f"budget ledger already exists: {record.owner_run_id}"
                    ) from exc
            else:
                cursor = conn.execute(
                    "update run_budget_ledgers set version = ?, state_json = ? "
                    "where owner_run_id = ? and version = ?",
                    (record.version + 1, payload, record.owner_run_id, record.version),
                )
                if cursor.rowcount != 1:
                    raise BudgetLedgerConflictError(
                        f"stale budget ledger for {record.owner_run_id}"
                    )
            record.version += 1

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists checkpoints (
                    run_id text not null,
                    sequence integer not null,
                    schema_version integer not null,
                    thread_id text not null,
                    turn_id text not null,
                    status text not null,
                    state_json text not null,
                    created_at text not null,
                    primary key(run_id, sequence)
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_checkpoints_thread
                on checkpoints(thread_id, run_id, sequence)
                """
            )
            conn.execute(
                "create index if not exists idx_checkpoints_run_created on checkpoints "
                "(json_extract(state_json, '$.created_at'), run_id, sequence)"
            )
            conn.execute(
                "create index if not exists idx_checkpoints_parent on checkpoints "
                "(json_extract(state_json, '$.parent_run_id'), run_id, sequence)"
            )
            conn.execute(
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
                    is_error integer not null,
                    error text,
                    last_failure_category text,
                    last_error_code text,
                    retry_state text not null default 'NONE',
                    retry_suppressed_reason text,
                    next_retry_at text,
                    retry_backoff_seconds real not null default 0,
                    started_at text,
                    completed_at text,
                    artifact_ids_json text not null default '[]',
                    updated_at text not null
                )
                """
            )
            self._migrate_tool_execution_retry_columns(conn)
            conn.execute(
                """
                create index if not exists idx_tool_executions_run
                on tool_executions(run_id, invocation_id)
                """
            )
            conn.execute(
                """
                create table if not exists run_budget_ledgers (
                    owner_run_id text primary key,
                    version integer not null,
                    state_json text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists submission_idempotency (
                    thread_id text not null,
                    idempotency_key text not null,
                    request_fingerprint text not null,
                    run_id text not null unique,
                    created_at text not null,
                    primary key(thread_id, idempotency_key)
                )
                """
            )
            conn.execute(
                """
                create table if not exists traffic_rate_buckets (
                    scope text not null,
                    scope_key text not null,
                    tokens real not null,
                    last_refill_at real not null,
                    primary key(scope, scope_key)
                )
                """
            )

    @staticmethod
    def _migrate_tool_execution_retry_columns(conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in conn.execute("pragma table_info(tool_executions)").fetchall()
        }
        additions = {
            "last_failure_category": "text",
            "last_error_code": "text",
            "retry_state": "text not null default 'NONE'",
            "retry_suppressed_reason": "text",
            "next_retry_at": "text",
            "retry_backoff_seconds": "real not null default 0",
            "artifact_ids_json": "text not null default '[]'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"alter table tool_executions add column {name} {declaration}")
        conn.execute(
            """
            update tool_executions
            set retry_state = 'RETRY_SUPPRESSED',
                retry_suppressed_reason = case
                    when status = 'UNKNOWN' then 'unknown_outcome'
                    else 'legacy_failure'
                end
            where retry_state = 'NONE'
              and attempt > 0
              and status in ('FAILED', 'UNKNOWN')
            """
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 30000")
        return conn


_RUN_SUMMARY_FIELDS = (
    "run_id", "thread_id", "turn_id", "run_kind", "execution_strategy", "status",
    "parent_run_id", "parent_step_id", "created_at", "updated_at", "started_at", "completed_at",
)

# These extract only list scalars in SQL. Checkpoint JSON remains recovery authority;
# no second Run projection or application-side historical state decoding is needed.
_SQLITE_RUN_ROWS = """
with current_runs as (
    select c.run_id, c.thread_id, c.turn_id, c.status,
        coalesce(json_extract(c.state_json, '$.run_kind'), 'agent') run_kind,
        coalesce(json_extract(c.state_json, '$.execution_strategy'), 'react') execution_strategy,
        json_extract(c.state_json, '$.parent_run_id') parent_run_id,
        json_extract(c.state_json, '$.parent_step_id') parent_step_id,
        json_extract(c.state_json, '$.created_at') created_at,
        json_extract(c.state_json, '$.updated_at') updated_at,
        json_extract(c.state_json, '$.created_at') started_at,
        case when c.status in ('COMPLETED','FAILED','CANCELLED')
            then json_extract(c.state_json, '$.updated_at') end completed_at
    from checkpoints c where c.sequence = (
        select max(sequence) from checkpoints latest where latest.run_id = c.run_id
    )
)
"""


def _run_list_filters(
    search: str, status: str, strategy: str, placeholder: str,
) -> tuple[list[str], list[object]]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if search.strip():
        # Literal substring search, including '%' and '_', matches the original UI.
        query = search.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        fields = ("run_id", "thread_id", "turn_id", "execution_strategy", "run_kind")
        clauses.append("(" + " or ".join(
            f"lower({field}) like {placeholder} escape '\\'" for field in fields
        ) + ")")
        params.extend([f"%{query}%"] * len(fields))
    if status:
        clauses.append(f"status = {placeholder}")
        params.append(status)
    if strategy:
        clauses.append(f"execution_strategy = {placeholder}")
        params.append(strategy)
    return clauses, params


def _run_list_order(sort: str, backend: str) -> tuple[str, str]:
    if sort in {"newest", "oldest"}:
        return "created_at", "asc" if sort == "oldest" else "desc"
    if sort == "status":
        return ("case status when 'RUNNING' then 0 "
                "when 'WAITING_APPROVAL' then 1 when 'WAITING_CHILD' then 1 "
                "when 'FAILED' then 2 when 'INTERRUPTED' then 3 when 'CANCELLED' then 3 "
                "when 'COMPLETED' then 4 else 5 end", "asc")
    if sort == "duration":
        if backend == "sqlite":
            return "max(0, (julianday(updated_at) - julianday(created_at)) * 86400000)", "desc"
        return "greatest(0, extract(epoch from (updated_at - created_at)) * 1000)", "desc"
    raise ValueError("invalid Run list sort")


def _child_counts_sql(placeholders: str) -> str:
    return (
        "select parent_run_id, count(*), "
        "sum(case when status not in ('COMPLETED','FAILED','CANCELLED') then 1 else 0 end), "
        "sum(case when status in ('WAITING_APPROVAL','WAITING_CHILD','INTERRUPTED') "
        "then 1 else 0 end), "
        "sum(case when status in ('COMPLETED','FAILED','CANCELLED') then 1 else 0 end) "
        f"from current_runs where parent_run_id in ({placeholders}) group by parent_run_id"
    )


def _attach_child_counts(summaries: list[dict[str, Any]], counts: Sequence[Sequence]) -> None:
    by_id = {str(row[0]): row[1:] for row in counts}
    fields = ("children_count", "active_children_count", "waiting_children_count",
              "terminal_children_count")
    for summary in summaries:
        summary.update(zip(fields, by_id.get(summary["run_id"], (0, 0, 0, 0)), strict=True))


def _decode_checkpoint(raw: object) -> Checkpoint:
    data = json.loads(str(raw))
    if not isinstance(data, dict):
        raise ValueError("checkpoint payload must be a JSON object")
    return Checkpoint.from_dict(data)


def _check_submission_fingerprint(existing: str, requested: str) -> None:
    if existing != requested:
        raise ApiError("IDEMPOTENCY_KEY_CONFLICT",
                       "idempotency key was already used with a different submission", 409)


def _check_thread_available(states: list[Checkpoint], run_id: str) -> None:
    active = next((state for state in states if state.parent_run_id is None
                   and state.run_id != run_id and state.status in OCCUPYING_RUN_STATUSES), None)
    if active is not None:
        raise ApiError("THREAD_BUSY", "thread already has an active root Run", 409,
                       run_id=active.run_id, details={"active_run_id": active.run_id})


def _submission_messages(states: list[Checkpoint], current_input: str) -> list[Message]:
    messages = []
    for state in sorted(states, key=lambda state: (state.created_at, state.run_id)):
        if state.parent_run_id is None and state.status == RunStatus.COMPLETED:
            messages.extend([Message(role="user", content=state.input),
                             Message(role="assistant", content=state.output_text)])
    messages.append(Message(role="user", content=current_input))
    return messages


def _submission_events(state: Checkpoint) -> list[tuple[str, dict]]:
    return [
        ("turn.started", {"message_chars": len(state.input)}),
        ("user.message", {"text": state.input}),
        ("run.admitted", {"status": state.status.value}),
    ]


def _completed_payload(state: Checkpoint) -> dict:
    return {"status": state.status.value, "total_turns": state.agent_turn,
            "total_tokens": state.total_tokens}


def _submission_buckets(
    principal_key: str, traffic: TrafficGovernanceConfig | None,
) -> list[tuple[str, str, float, int]]:
    if traffic is None:
        return []
    buckets = []
    if traffic.global_submission_rate is not None and traffic.global_submission_burst is not None:
        buckets.append(("global", "root_submission", traffic.global_submission_rate,
                        traffic.global_submission_burst))
    if (traffic.principal_submission_rate is not None
            and traffic.principal_submission_burst is not None):
        buckets.append(("principal", principal_key, traffic.principal_submission_rate,
                        traffic.principal_submission_burst))
    return buckets


def _raise_submission_rate_limit(wait: float) -> None:
    raise ApiError("RATE_LIMITED", "new Root Run submission rate limit exceeded", 429,
                   details={"retry_after_seconds": max(1, int(wait + 0.999))})


def _now() -> str:
    return datetime.now(UTC).isoformat()
