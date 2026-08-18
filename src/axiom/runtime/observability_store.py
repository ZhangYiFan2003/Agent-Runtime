from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Protocol

from axiom.runtime.models import Checkpoint, RunStatus
from axiom.runtime.observability import (
    OBSERVABILITY_SCHEMA_VERSION,
    RunMetrics,
    Span,
    SpanStatus,
    SpanType,
    Trace,
    TraceBundle,
    duration_ms,
    json_attributes,
    new_span_id,
    now,
    root_span_id_for_run,
    trace_id_for_run,
)


class ObservabilityStore(Protocol):
    async def save_trace(self, trace: Trace) -> None: ...

    async def load_trace(self, run_id: str) -> Trace | None: ...

    async def save_span(self, span: Span) -> None: ...

    async def load_span(self, span_id: str) -> Span | None: ...

    async def list_spans(self, trace_id: str) -> list[Span]: ...


class MemoryObservabilityStore:
    def __init__(self) -> None:
        self._traces: dict[str, dict[str, object]] = {}
        self._run_to_trace: dict[str, str] = {}
        self._spans: dict[str, dict[str, object]] = {}
        self._lock = asyncio.Lock()

    async def save_trace(self, trace: Trace) -> None:
        async with self._lock:
            current = self._run_to_trace.get(trace.run_id)
            if current and current != trace.trace_id:
                raise ValueError("run_id is already associated with another trace")
            self._run_to_trace[trace.run_id] = trace.trace_id
            self._traces[trace.trace_id] = deepcopy(trace.to_dict())

    async def load_trace(self, run_id: str) -> Trace | None:
        async with self._lock:
            trace_id = self._run_to_trace.get(run_id)
            row = self._traces.get(trace_id or "")
            return Trace.from_dict(deepcopy(row)) if row else None

    async def save_span(self, span: Span) -> None:
        async with self._lock:
            self._spans[span.span_id] = deepcopy(span.to_dict())

    async def load_span(self, span_id: str) -> Span | None:
        async with self._lock:
            row = self._spans.get(span_id)
            return Span.from_dict(deepcopy(row)) if row else None

    async def list_spans(self, trace_id: str) -> list[Span]:
        async with self._lock:
            spans = [
                Span.from_dict(deepcopy(row))
                for row in self._spans.values()
                if row.get("trace_id") == trace_id
            ]
        return sorted(spans, key=lambda span: (span.started_at, span.span_id))


class SQLiteObservabilityStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    async def save_trace(self, trace: Trace) -> None:
        await asyncio.to_thread(self._save_trace, trace)

    async def load_trace(self, run_id: str) -> Trace | None:
        return await asyncio.to_thread(self._load_trace, run_id)

    async def save_span(self, span: Span) -> None:
        await asyncio.to_thread(self._save_span, span)

    async def load_span(self, span_id: str) -> Span | None:
        return await asyncio.to_thread(self._load_span, span_id)

    async def list_spans(self, trace_id: str) -> list[Span]:
        return await asyncio.to_thread(self._list_spans, trace_id)

    def _save_trace(self, trace: Trace) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into traces(
                    trace_id, run_id, thread_id, turn_id, started_at,
                    ended_at, status, schema_version
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(trace_id) do update set
                    ended_at = excluded.ended_at,
                    status = excluded.status,
                    schema_version = excluded.schema_version
                """,
                (
                    trace.trace_id,
                    trace.run_id,
                    trace.thread_id,
                    trace.turn_id,
                    trace.started_at,
                    trace.ended_at,
                    trace.status,
                    trace.schema_version,
                ),
            )

    def _load_trace(self, run_id: str) -> Trace | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select trace_id, run_id, thread_id, turn_id, started_at,
                       ended_at, status, schema_version
                from traces where run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return _trace_from_row(row) if row else None

    def _save_span(self, span: Span) -> None:
        attributes = json.dumps(
            json_attributes(span.attributes), ensure_ascii=False, separators=(",", ":")
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into spans(
                    span_id, trace_id, parent_span_id, span_type, name,
                    started_at, ended_at, status, attributes_json, schema_version
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(span_id) do update set
                    parent_span_id = excluded.parent_span_id,
                    ended_at = excluded.ended_at,
                    status = excluded.status,
                    attributes_json = excluded.attributes_json,
                    schema_version = excluded.schema_version
                """,
                (
                    span.span_id,
                    span.trace_id,
                    span.parent_span_id,
                    span.span_type.value,
                    span.name,
                    span.started_at,
                    span.ended_at,
                    span.status.value,
                    attributes,
                    span.schema_version,
                ),
            )

    def _load_span(self, span_id: str) -> Span | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select span_id, trace_id, parent_span_id, span_type, name,
                       started_at, ended_at, status, attributes_json, schema_version
                from spans where span_id = ?
                """,
                (span_id,),
            ).fetchone()
        return _span_from_row(row) if row else None

    def _list_spans(self, trace_id: str) -> list[Span]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select span_id, trace_id, parent_span_id, span_type, name,
                       started_at, ended_at, status, attributes_json, schema_version
                from spans where trace_id = ? order by started_at, span_id
                """,
                (trace_id,),
            ).fetchall()
        return [_span_from_row(row) for row in rows]

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists observability_schema (
                    component text primary key,
                    version integer not null
                )
                """
            )
            row = conn.execute(
                "select version from observability_schema where component = 'core'"
            ).fetchone()
            if row and int(row[0]) > OBSERVABILITY_SCHEMA_VERSION:
                raise ValueError(f"unsupported observability schema version: {row[0]}")
            conn.execute(
                """
                insert into observability_schema(component, version) values ('core', ?)
                on conflict(component) do update set version = excluded.version
                """,
                (OBSERVABILITY_SCHEMA_VERSION,),
            )
            conn.execute(
                """
                create table if not exists traces (
                    trace_id text primary key,
                    run_id text not null unique,
                    thread_id text not null,
                    turn_id text not null,
                    started_at text not null,
                    ended_at text,
                    status text not null,
                    schema_version integer not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists spans (
                    span_id text primary key,
                    trace_id text not null,
                    parent_span_id text,
                    span_type text not null,
                    name text not null,
                    started_at text not null,
                    ended_at text,
                    status text not null,
                    attributes_json text not null,
                    schema_version integer not null
                )
                """
            )
            conn.execute(
                "create index if not exists idx_spans_trace on spans(trace_id, started_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 30000")
        return conn


class ObservabilityService:
    def __init__(self, store: ObservabilityStore):
        self.store = store

    async def trace(self, run_id: str) -> TraceBundle | None:
        trace = await self.store.load_trace(run_id)
        if trace is None:
            return None
        return TraceBundle(trace=trace, spans=await self.store.list_spans(trace.trace_id))

    async def metrics(self, run_id: str) -> RunMetrics | None:
        bundle = await self.trace(run_id)
        if bundle is None:
            return None
        return RunMetrics.from_trace(bundle.trace, bundle.spans)


class RunTracer:
    def __init__(self, store: ObservabilityStore):
        self.store = store
        self.trace: Trace | None = None
        self.root_span: Span | None = None

    async def start_run(self, state: Checkpoint, *, recovered: bool = False) -> Trace:
        trace = await self.store.load_trace(state.run_id)
        if trace is None:
            trace = Trace(
                trace_id=trace_id_for_run(state.run_id),
                run_id=state.run_id,
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                started_at=state.created_at,
                status=state.status.value,
            )
        else:
            trace.status = state.status.value
            trace.ended_at = None
        await self.store.save_trace(trace)
        self.trace = trace

        root_id = root_span_id_for_run(state.run_id)
        root = await self.store.load_span(root_id)
        if root is None:
            root = Span(
                span_id=root_id,
                trace_id=trace.trace_id,
                span_type=SpanType.AGENT,
                name="run",
                started_at=trace.started_at,
                attributes={
                    "run_id": state.run_id,
                    "thread_id": state.thread_id,
                    "turn_id": state.turn_id,
                    "parent_run_id": state.parent_run_id,
                    "parent_step_id": state.parent_step_id,
                    "run_kind": state.run_kind,
                },
                parent_span_id=state.parent_step_id,
            )
            await self.store.save_span(root)
        else:
            root.attributes.update(
                json_attributes(
                    {
                        "parent_run_id": state.parent_run_id,
                        "parent_step_id": state.parent_step_id,
                        "run_kind": state.run_kind,
                    }
                )
            )
            await self.store.save_span(root)
        self.root_span = root
        if recovered:
            await self._close_stale_spans()
        return trace

    async def start_span(
        self,
        span_type: SpanType,
        name: str,
        *,
        attributes: dict[str, object] | None = None,
        parent_span_id: str | None = None,
        span_id: str | None = None,
        reopen: bool = False,
    ) -> Span:
        trace = self._require_trace()
        identifier = span_id or new_span_id()
        span = await self.store.load_span(identifier)
        if span is None:
            span = Span(
                span_id=identifier,
                trace_id=trace.trace_id,
                parent_span_id=parent_span_id or self.root_span_id,
                span_type=span_type,
                name=name,
                started_at=now(),
                attributes=json_attributes(dict(attributes or {})),
            )
        else:
            span.attributes.update(json_attributes(dict(attributes or {})))
            if reopen:
                span.status = SpanStatus.RUNNING
                span.ended_at = None
        await self.store.save_span(span)
        return span

    async def finish_span(
        self,
        span: Span,
        status: SpanStatus,
        *,
        attributes: dict[str, object] | None = None,
    ) -> Span:
        provided = json_attributes(dict(attributes or {}))
        span.attributes.update(provided)
        span.status = status
        span.ended_at = now()
        if "latency_ms" not in provided:
            span.attributes["latency_ms"] = duration_ms(span.started_at, span.ended_at)
        await self.store.save_span(span)
        return span

    async def annotate_span(self, span_id: str, **attributes: object) -> Span | None:
        span = await self.store.load_span(span_id)
        if span is None:
            return None
        span.attributes.update(json_attributes(attributes))
        await self.store.save_span(span)
        return span

    async def update_run(self, status: RunStatus | str, *, terminal: bool = False) -> None:
        trace = self._require_trace()
        trace.status = status.value if isinstance(status, RunStatus) else str(status)
        if terminal:
            trace.ended_at = now()
        await self.store.save_trace(trace)
        if terminal and self.root_span is not None:
            span_status = {
                RunStatus.COMPLETED.value: SpanStatus.SUCCEEDED,
                RunStatus.CANCELLED.value: SpanStatus.CANCELLED,
            }.get(trace.status, SpanStatus.FAILED)
            await self.finish_span(self.root_span, span_status)

    @property
    def root_span_id(self) -> str:
        if self.root_span is None:
            raise RuntimeError("trace has not been started")
        return self.root_span.span_id

    def _require_trace(self) -> Trace:
        if self.trace is None:
            raise RuntimeError("trace has not been started")
        return self.trace

    async def _close_stale_spans(self) -> None:
        trace = self._require_trace()
        spans = await self.store.list_spans(trace.trace_id)
        for span in spans:
            if span.status != SpanStatus.RUNNING or span.span_id == self.root_span_id:
                continue
            if span.span_type == SpanType.TOOL:
                continue
            await self.finish_span(
                span,
                SpanStatus.INTERRUPTED,
                attributes={"recovered_after_crash": True},
            )


def _trace_from_row(row: tuple[object, ...]) -> Trace:
    return Trace.from_dict(
        {
            "trace_id": row[0],
            "run_id": row[1],
            "thread_id": row[2],
            "turn_id": row[3],
            "started_at": row[4],
            "ended_at": row[5],
            "status": row[6],
            "schema_version": row[7],
        }
    )


def _span_from_row(row: tuple[object, ...]) -> Span:
    try:
        attributes = json.loads(str(row[8]))
    except json.JSONDecodeError:
        attributes = {}
    return Span.from_dict(
        {
            "span_id": row[0],
            "trace_id": row[1],
            "parent_span_id": row[2],
            "span_type": row[3],
            "name": row[4],
            "started_at": row[5],
            "ended_at": row[6],
            "status": row[7],
            "attributes": attributes if isinstance(attributes, dict) else {},
            "schema_version": row[9],
        }
    )
