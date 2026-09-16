from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4


@dataclass(slots=True)
class RuntimeEvent:
    id: int
    thread_id: str
    type: str
    payload: dict[str, Any]
    created_at: str
    turn_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    parent_step_id: str | None = None
    assignment_id: str | None = None


class EventRepository(Protocol):
    backend: str

    def create_thread(self) -> str: ...

    def thread_exists(self, thread_id: str) -> bool: ...

    def list_threads(self) -> list[str]: ...

    def append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> int: ...

    def list_events(
        self,
        thread_id: str,
        after_id: int | None = None,
        *,
        run_id: str | None = None,
    ) -> list[RuntimeEvent]: ...

    def database_ok(self) -> bool: ...


class ThreadEventRepository:
    """SQLite thread/event repository retained as the local default."""

    backend = "sqlite"

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def create_thread(self) -> str:
        thread_id = f"thread_{uuid4().hex}"
        now = _now()
        with self._connect() as conn:
            conn.execute("insert into threads(id, created_at) values (?, ?)", (thread_id, now))
        self.append_event(thread_id, "thread.created", {"id": thread_id})
        return thread_id

    def thread_exists(self, thread_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("select 1 from threads where id = ?", (thread_id,)).fetchone()
        return row is not None

    def list_threads(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("select id from threads order by created_at, id").fetchall()
        return [str(row[0]) for row in rows]

    def append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> int:
        if not self.thread_exists(thread_id):
            raise ValueError("thread not found")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                insert into events(
                    thread_id, turn_id, run_id, parent_run_id, parent_step_id,
                    assignment_id, type, payload, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_id,
                    _optional_text(payload.get("turn_id")),
                    _optional_text(payload.get("run_id")),
                    _optional_text(payload.get("parent_run_id")),
                    _optional_text(payload.get("parent_step_id")),
                    _optional_text(payload.get("assignment_id")),
                    event_type,
                    json.dumps(_jsonable(payload), ensure_ascii=False),
                    _now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_events(
        self,
        thread_id: str,
        after_id: int | None = None,
        *,
        run_id: str | None = None,
    ) -> list[RuntimeEvent]:
        if not self.thread_exists(thread_id):
            return []
        clause = "thread_id = ?"
        params: list[object] = [thread_id]
        if after_id is not None:
            clause += " and id > ?"
            params.append(after_id)
        if run_id is not None:
            clause += " and run_id = ?"
            params.append(run_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                select id, thread_id, type, payload, created_at,
                       turn_id, run_id, parent_run_id, parent_step_id, assignment_id
                from events
                where {clause}
                order by id
                """,
                tuple(params),
            ).fetchall()
        return [
            RuntimeEvent(
                id=int(row[0]),
                thread_id=str(row[1]),
                type=str(row[2]),
                payload=_decode_payload(row[3]),
                created_at=str(row[4]),
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
            with self._connect() as conn:
                conn.execute("select 1").fetchone()
            return True
        except sqlite3.DatabaseError:
            return False

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists threads (
                    id text primary key,
                    created_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    thread_id text not null references threads(id) on delete cascade,
                    type text not null,
                    payload text not null,
                    created_at text not null
                )
                """
            )
            for name in (
                "turn_id",
                "run_id",
                "parent_run_id",
                "parent_step_id",
                "assignment_id",
            ):
                _ensure_sqlite_column(conn, "events", name, "text")
            conn.execute("create index if not exists idx_events_thread_id on events(thread_id, id)")
            conn.execute(
                "create index if not exists idx_events_run_id on events(thread_id, run_id, id)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 30000")
        conn.execute("pragma foreign_keys = on")
        return conn


def _ensure_sqlite_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    columns = {str(row[1]) for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {declaration}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return _jsonable(value.value)
    return value


def _decode_payload(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    decoded = json.loads(str(payload))
    return decoded if isinstance(decoded, dict) else {}


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
