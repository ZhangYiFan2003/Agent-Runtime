from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from axiom.runtime.models import BudgetLedgerRecord, Checkpoint, ToolExecutionRecord


class CheckpointConflictError(RuntimeError):
    """Raised when a stale worker attempts to advance a run."""


class BudgetLedgerConflictError(RuntimeError):
    """Raised when an atomic budget ledger update loses a CAS race."""


class CheckpointStore(Protocol):
    async def save(self, checkpoint: Checkpoint) -> None: ...

    async def load(self, run_id: str) -> Checkpoint | None: ...

    async def list(self, thread_id: str) -> list[Checkpoint]: ...


class ToolExecutionStore(Protocol):
    async def save_tool_execution(self, record: ToolExecutionRecord) -> None: ...

    async def load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None: ...


class RuntimeStore(CheckpointStore, ToolExecutionStore, Protocol):
    async def load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None: ...

    async def save_budget_ledger(self, record: BudgetLedgerRecord) -> None: ...


class MemoryCheckpointStore:
    def __init__(self) -> None:
        self._checkpoints: dict[str, list[dict[str, object]]] = {}
        self._tool_executions: dict[str, dict[str, object]] = {}
        self._budget_ledgers: dict[str, dict[str, object]] = {}
        self._lock = asyncio.Lock()

    async def save(self, checkpoint: Checkpoint) -> None:
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

    async def save_tool_execution(self, record: ToolExecutionRecord) -> None:
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

    async def save_budget_ledger(self, record: BudgetLedgerRecord) -> None:
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
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

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

    async def load_budget_ledger(self, owner_run_id: str) -> BudgetLedgerRecord | None:
        return await asyncio.to_thread(self._load_budget_ledger, owner_run_id)

    async def save_budget_ledger(self, record: BudgetLedgerRecord) -> None:
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
            next_sequence = latest + 1
            checkpoint.sequence = next_sequence
            checkpoint.updated_at = _now()
            payload = json.dumps(checkpoint.to_dict(), ensure_ascii=False, separators=(",", ":"))
            conn.execute(
                """
                insert into checkpoints(
                    run_id, sequence, schema_version, thread_id, turn_id,
                    status, state_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.run_id,
                    checkpoint.sequence,
                    checkpoint.schema_version,
                    checkpoint.thread_id,
                    checkpoint.turn_id,
                    checkpoint.status.value,
                    payload,
                    checkpoint.updated_at,
                ),
            )

    def _load(self, run_id: str) -> Checkpoint | None:
        with self._connect() as conn:
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
                record.started_at,
                record.completed_at,
                record.updated_at,
            )
            conn.execute(
                """
                insert into tool_executions(
                    invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                    status, attempt, result, is_error, error, started_at,
                    completed_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(invocation_id) do update set
                    status = excluded.status,
                    attempt = excluded.attempt,
                    result = excluded.result,
                    is_error = excluded.is_error,
                    error = excluded.error,
                    started_at = excluded.started_at,
                    completed_at = excluded.completed_at,
                    updated_at = excluded.updated_at
                """,
                values,
            )

    def _load_tool_execution(self, invocation_id: str) -> ToolExecutionRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                       status, attempt, result, is_error, error, started_at,
                       completed_at, updated_at
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
                "started_at": row[10],
                "completed_at": row[11],
                "updated_at": row[12],
            }
        )

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
                    started_at text,
                    completed_at text,
                    updated_at text not null
                )
                """
            )
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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 30000")
        return conn


def _decode_checkpoint(raw: object) -> Checkpoint:
    data = json.loads(str(raw))
    if not isinstance(data, dict):
        raise ValueError("checkpoint payload must be a JSON object")
    return Checkpoint.from_dict(data)


def _now() -> str:
    return datetime.now(UTC).isoformat()
