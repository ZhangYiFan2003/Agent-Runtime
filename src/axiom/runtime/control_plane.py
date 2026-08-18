from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from axiom.runtime.models import Checkpoint, RunStatus

CONTROL_OPERATION_SCHEMA_VERSION = 1


class ControlOperationName(StrEnum):
    RESUME = "resume"
    APPROVE = "approve"
    REJECT = "reject"
    CANCEL = "cancel"


class ControlOperationStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(slots=True)
class ApiError(Exception):
    code: str
    message: str
    http_status: int
    run_id: str | None = None
    status: str | None = None
    operation: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        for key, value in (
            ("run_id", self.run_id),
            ("status", self.status),
            ("operation", self.operation),
        ):
            if value is not None:
                error[key] = value
        if self.details:
            error["details"] = self.details
        return {"error": error}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, http_status: int = 409) -> ApiError:
        return cls(
            code=str(data.get("code") or "operation_failed"),
            message=str(data.get("message") or "control operation failed"),
            http_status=int(data.get("http_status") or http_status),
            run_id=_optional_str(data.get("run_id")),
            status=_optional_str(data.get("status")),
            operation=_optional_str(data.get("operation")),
            details=data.get("details") if isinstance(data.get("details"), dict) else {},
        )


@dataclass(slots=True)
class ControlOperationRecord:
    operation_id: str
    idempotency_key: str
    run_id: str
    operation: ControlOperationName
    status: ControlOperationStatus
    request_hash: str
    request: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: _now())
    completed_at: str | None = None
    schema_version: int = CONTROL_OPERATION_SCHEMA_VERSION


class SQLiteControlOperationStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def lookup(self, run_id: str, idempotency_key: str) -> ControlOperationRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                select operation_id, idempotency_key, run_id, operation, status,
                       request_hash, request_json, result_json, error_json,
                       created_at, completed_at, schema_version
                from control_operations
                where run_id = ? and idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
        return _operation_from_row(row) if row else None

    def begin(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        operation: ControlOperationName,
        request: dict[str, Any],
    ) -> tuple[ControlOperationRecord, bool]:
        request_hash = operation_request_hash(operation, request)
        with self._connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                """
                select operation_id, idempotency_key, run_id, operation, status,
                       request_hash, request_json, result_json, error_json,
                       created_at, completed_at, schema_version
                from control_operations
                where run_id = ? and idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if row:
                record = _operation_from_row(row)
                if record.operation != operation or record.request_hash != request_hash:
                    raise ApiError(
                        "idempotency_key_conflict",
                        "idempotency key was already used with a different request",
                        409,
                        run_id=run_id,
                        operation=operation.value,
                    )
                return record, False
            record = ControlOperationRecord(
                operation_id=f"operation_{uuid4().hex}",
                idempotency_key=idempotency_key,
                run_id=run_id,
                operation=operation,
                status=ControlOperationStatus.IN_PROGRESS,
                request_hash=request_hash,
                request=request,
            )
            conn.execute(
                """
                insert into control_operations(
                    operation_id, idempotency_key, run_id, operation, status,
                    request_hash, request_json, result_json, error_json,
                    created_at, completed_at, schema_version
                ) values (?, ?, ?, ?, ?, ?, ?, null, null, ?, null, ?)
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
            )
        return record, True

    def complete(self, operation_id: str, result: dict[str, Any]) -> ControlOperationRecord:
        completed_at = _now()
        with self._connect() as conn:
            conn.execute(
                """
                update control_operations
                set status = ?, result_json = ?, error_json = null, completed_at = ?
                where operation_id = ?
                """,
                (
                    ControlOperationStatus.COMPLETED.value,
                    _json(result),
                    completed_at,
                    operation_id,
                ),
            )
        return self._require_operation(operation_id)

    def fail(self, operation_id: str, error: ApiError) -> ControlOperationRecord:
        completed_at = _now()
        with self._connect() as conn:
            conn.execute(
                """
                update control_operations
                set status = ?, error_json = ?, completed_at = ?
                where operation_id = ?
                """,
                (
                    ControlOperationStatus.FAILED.value,
                    _json({**error.to_dict()["error"], "http_status": error.http_status}),
                    completed_at,
                    operation_id,
                ),
            )
        return self._require_operation(operation_id)

    def find_interrupt_resolution(
        self,
        run_id: str,
        invocation_id: str,
    ) -> ControlOperationRecord | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select operation_id, idempotency_key, run_id, operation, status,
                       request_hash, request_json, result_json, error_json,
                       created_at, completed_at, schema_version
                from control_operations
                where run_id = ? and operation in ('approve', 'reject')
                  and status = 'COMPLETED'
                order by created_at desc
                """,
                (run_id,),
            ).fetchall()
        for row in rows:
            record = _operation_from_row(row)
            if record.request.get("invocation_id") == invocation_id:
                return record
        return None

    def _require_operation(self, operation_id: str) -> ControlOperationRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select operation_id, idempotency_key, run_id, operation, status,
                       request_hash, request_json, result_json, error_json,
                       created_at, completed_at, schema_version
                from control_operations where operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("control operation disappeared")
        return _operation_from_row(row)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists control_operations (
                    operation_id text primary key,
                    idempotency_key text not null,
                    run_id text not null,
                    operation text not null,
                    status text not null,
                    request_hash text not null,
                    request_json text not null,
                    result_json text,
                    error_json text,
                    created_at text not null,
                    completed_at text,
                    schema_version integer not null,
                    unique(run_id, idempotency_key)
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_control_operations_interrupt
                on control_operations(run_id, operation, status)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 30000")
        return conn


def allowed_operations(status: RunStatus) -> frozenset[ControlOperationName]:
    return {
        RunStatus.RUNNING: frozenset({ControlOperationName.RESUME, ControlOperationName.CANCEL}),
        RunStatus.INTERRUPTED: frozenset(
            {ControlOperationName.RESUME, ControlOperationName.CANCEL}
        ),
        RunStatus.WAITING_APPROVAL: frozenset(
            {
                ControlOperationName.APPROVE,
                ControlOperationName.REJECT,
                ControlOperationName.CANCEL,
            }
        ),
        RunStatus.WAITING_CHILD: frozenset(
            {ControlOperationName.RESUME, ControlOperationName.CANCEL}
        ),
        RunStatus.COMPLETED: frozenset(),
        RunStatus.FAILED: frozenset(),
        RunStatus.CANCELLED: frozenset({ControlOperationName.CANCEL}),
    }[status]


def operation_request_hash(
    operation: ControlOperationName,
    request: dict[str, Any],
) -> str:
    canonical = _json({"operation": operation.value, "request": request})
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_view(
    state: Checkpoint,
    *,
    children: list[Checkpoint] | None = None,
    assignment_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    child_states = sorted(children or [], key=lambda item: (item.created_at, item.run_id))
    metadata = assignment_metadata or {}
    pending = pending_interrupts(state, child_states)
    active_children = [item for item in child_states if not item.finished]
    waiting_children = [
        item
        for item in child_states
        if item.status
        in {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_CHILD, RunStatus.INTERRUPTED}
    ]
    return {
        "run_id": state.run_id,
        "thread_id": state.thread_id,
        "turn_id": state.turn_id,
        "run_kind": state.run_kind,
        "execution_strategy": state.execution_strategy,
        "status": state.status.value,
        "waiting_reason": waiting_reason(state),
        "recovery_action": recovery_action(state, child_states),
        "parent_run_id": state.parent_run_id,
        "parent_step_id": state.parent_step_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "started_at": state.created_at,
        "completed_at": state.updated_at if state.finished else None,
        "output": _summary(state.output_text),
        "error": state.error.to_dict() if state.error else None,
        "interrupt": interrupt_summary(state),
        "children_count": len(child_states),
        "active_children_count": len(active_children),
        "waiting_children_count": len(waiting_children),
        "terminal_children_count": sum(item.finished for item in child_states),
        "active_child_run_ids": [item.run_id for item in active_children],
        "pending_interrupts": pending,
        "assignment": metadata.get(state.run_id),
        "allowed_operations": sorted(item.value for item in allowed_operations(state.status)),
    }


def child_view(
    state: Checkpoint,
    *,
    assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "child_run_id": state.run_id,
        "run_kind": state.run_kind,
        "status": state.status.value,
        "parent_run_id": state.parent_run_id,
        "parent_step_id": state.parent_step_id,
        "assignment_id": assignment.get("assignment_id") if assignment else None,
        "worker_role": assignment.get("worker_role") if assignment else None,
        "attempt": assignment.get("attempt") if assignment else None,
        "interrupt": interrupt_summary(state),
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }


def pending_interrupts(
    state: Checkpoint,
    children: list[Checkpoint],
) -> list[dict[str, Any]]:
    candidates = [state, *children]
    return [
        summary for candidate in candidates if (summary := interrupt_summary(candidate)) is not None
    ]


def interrupt_summary(state: Checkpoint) -> dict[str, Any] | None:
    interrupt = state.interrupt
    if interrupt is None:
        return None
    return {
        "run_id": state.run_id,
        "invocation_id": interrupt.invocation_id,
        "interrupt_type": interrupt.kind,
        "tool_name": interrupt.tool_name,
        "reason": interrupt.reason,
        "created_at": interrupt.created_at,
    }


def waiting_reason(state: Checkpoint) -> str | None:
    if state.status == RunStatus.WAITING_APPROVAL:
        return "approval"
    if state.status == RunStatus.WAITING_CHILD:
        return "child"
    if state.status == RunStatus.INTERRUPTED:
        return "manual_interrupt"
    if state.status == RunStatus.RUNNING:
        return "recovery"
    return None


def recovery_action(state: Checkpoint, children: list[Checkpoint]) -> str | None:
    if state.status == RunStatus.WAITING_APPROVAL:
        return "approval_required"
    if state.status == RunStatus.INTERRUPTED:
        return "client_resume"
    if state.status == RunStatus.RUNNING:
        return "client_resume"
    if state.status == RunStatus.WAITING_CHILD:
        if state.execution_strategy == "multi_agent":
            return "reconcile_parent"
        return (
            "resume_parent"
            if children and all(child.finished for child in children)
            else "wait_for_children"
        )
    return None


def _operation_from_row(row: tuple[object, ...]) -> ControlOperationRecord:
    return ControlOperationRecord(
        operation_id=str(row[0]),
        idempotency_key=str(row[1]),
        run_id=str(row[2]),
        operation=ControlOperationName(str(row[3])),
        status=ControlOperationStatus(str(row[4])),
        request_hash=str(row[5]),
        request=_dict_json(row[6]),
        result=_dict_json(row[7]) if row[7] is not None else None,
        error=_dict_json(row[8]) if row[8] is not None else None,
        created_at=str(row[9]),
        completed_at=_optional_str(row[10]),
        schema_version=int(row[11]),
    )


def _dict_json(value: object) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _summary(value: str, limit: int = 4000) -> str:
    text = value.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()
