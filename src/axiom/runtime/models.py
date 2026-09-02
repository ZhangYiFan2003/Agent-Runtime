from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from axiom.types import Message

CHECKPOINT_SCHEMA_VERSION = 1


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_CHILD = "WAITING_CHILD"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ToolExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(slots=True)
class Interrupt:
    kind: str
    reason: str
    invocation_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "invocation_id": self.invocation_id,
            "tool_name": self.tool_name,
            "arguments": _json_value(self.arguments),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Interrupt:
        return cls(
            kind=str(data.get("kind") or "manual"),
            reason=str(data.get("reason") or "interrupted"),
            invocation_id=_optional_str(data.get("invocation_id")),
            tool_name=_optional_str(data.get("tool_name")),
            arguments=_dict(data.get("arguments")),
            created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
        )


@dataclass(slots=True)
class RunError:
    type: str
    message: str
    step: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "step": self.step,
            "metadata": _json_value(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunError:
        return cls(
            type=str(data.get("type") or "RuntimeError"),
            message=str(data.get("message") or "run failed"),
            step=_optional_str(data.get("step")),
            metadata=_dict(data.get("metadata")),
        )


@dataclass(slots=True)
class Checkpoint:
    run_id: str
    thread_id: str
    turn_id: str
    input: str
    messages: list[Message]
    status: RunStatus = RunStatus.RUNNING
    sequence: int = 0
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    agent_turn: int = 0
    step_index: int = 0
    total_tokens: int = 0
    output_text: str = ""
    execution_strategy: str = "react"
    strategy_state: dict[str, Any] = field(default_factory=dict)
    parent_run_id: str | None = None
    parent_step_id: str | None = None
    budget_owner_run_id: str | None = None
    budget_policy: dict[str, Any] = field(default_factory=dict)
    progress_policy: dict[str, Any] = field(default_factory=dict)
    progress_state: dict[str, Any] = field(default_factory=dict)
    run_kind: str = "agent"
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    next_tool_index: int = 0
    decisions: dict[str, str] = field(default_factory=dict)
    interrupt: Interrupt | None = None
    error: RunError | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def create(
        cls,
        *,
        thread_id: str,
        input: str,
        history: list[Message] | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        execution_strategy: str = "react",
        parent_run_id: str | None = None,
        parent_step_id: str | None = None,
        run_kind: str = "agent",
        budget_owner_run_id: str | None = None,
        budget_policy: dict[str, Any] | None = None,
        progress_policy: dict[str, Any] | None = None,
        progress_state: dict[str, Any] | None = None,
    ) -> Checkpoint:
        return cls(
            run_id=run_id or _new_id("run"),
            thread_id=thread_id,
            turn_id=turn_id or _new_id("turn"),
            input=input,
            messages=[*(history or []), Message(role="user", content=input)],
            execution_strategy=execution_strategy,
            parent_run_id=parent_run_id,
            parent_step_id=parent_step_id,
            run_kind=run_kind,
            budget_owner_run_id=budget_owner_run_id,
            budget_policy=dict(budget_policy or {}),
            progress_policy=dict(progress_policy or {}),
            progress_state=dict(progress_state or {}),
        )

    @property
    def finished(self) -> bool:
        return self.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "status": self.status.value,
            "input": self.input,
            "messages": [_message_to_dict(message) for message in self.messages],
            "agent_turn": self.agent_turn,
            "step_index": self.step_index,
            "total_tokens": self.total_tokens,
            "output_text": self.output_text,
            "execution_strategy": self.execution_strategy,
            "strategy_state": _json_value(self.strategy_state),
            "parent_run_id": self.parent_run_id,
            "parent_step_id": self.parent_step_id,
            "budget_owner_run_id": self.budget_owner_run_id,
            "budget_policy": _json_value(self.budget_policy),
            "progress_policy": _json_value(self.progress_policy),
            "progress_state": _json_value(self.progress_state),
            "run_kind": self.run_kind,
            "pending_tool_calls": _json_value(self.pending_tool_calls),
            "next_tool_index": self.next_tool_index,
            "decisions": dict(self.decisions),
            "interrupt": self.interrupt.to_dict() if self.interrupt else None,
            "error": self.error.to_dict() if self.error else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        schema_version = int(data.get("schema_version") or 0)
        if schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(f"unsupported checkpoint schema version: {schema_version}")
        raw_messages = data.get("messages")
        raw_calls = data.get("pending_tool_calls")
        return cls(
            run_id=str(data["run_id"]),
            thread_id=str(data["thread_id"]),
            turn_id=str(data["turn_id"]),
            input=str(data.get("input") or ""),
            messages=[_message_from_dict(item) for item in raw_messages if isinstance(item, dict)]
            if isinstance(raw_messages, list)
            else [],
            status=RunStatus(str(data.get("status") or RunStatus.RUNNING)),
            sequence=int(data.get("sequence") or 0),
            schema_version=schema_version,
            agent_turn=int(data.get("agent_turn") or 0),
            step_index=int(data.get("step_index") or 0),
            total_tokens=int(data.get("total_tokens") or 0),
            output_text=str(data.get("output_text") or ""),
            execution_strategy=str(data.get("execution_strategy") or "react"),
            strategy_state=_dict(data.get("strategy_state")),
            parent_run_id=_optional_str(data.get("parent_run_id")),
            parent_step_id=_optional_str(data.get("parent_step_id")),
            budget_owner_run_id=_optional_str(data.get("budget_owner_run_id")),
            budget_policy=_dict(data.get("budget_policy")),
            progress_policy=_dict(data.get("progress_policy")),
            progress_state=_dict(data.get("progress_state")),
            run_kind=str(data.get("run_kind") or "agent"),
            pending_tool_calls=[item for item in raw_calls if isinstance(item, dict)]
            if isinstance(raw_calls, list)
            else [],
            next_tool_index=int(data.get("next_tool_index") or 0),
            decisions={str(key): str(value) for key, value in _dict(data.get("decisions")).items()},
            interrupt=Interrupt.from_dict(data["interrupt"])
            if isinstance(data.get("interrupt"), dict)
            else None,
            error=RunError.from_dict(data["error"])
            if isinstance(data.get("error"), dict)
            else None,
            created_at=str(data.get("created_at") or datetime.now(UTC).isoformat()),
            updated_at=str(data.get("updated_at") or datetime.now(UTC).isoformat()),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "status": self.status.value,
            "sequence": self.sequence,
            "agent_turn": self.agent_turn,
            "step_index": self.step_index,
            "total_tokens": self.total_tokens,
            "execution_strategy": self.execution_strategy,
            "parent_run_id": self.parent_run_id,
            "parent_step_id": self.parent_step_id,
            "budget_owner_run_id": self.budget_owner_run_id,
            "budget_policy": _json_value(self.budget_policy),
            "progress_policy": _json_value(self.progress_policy),
            "progress_state": _json_value(self.progress_state),
            "run_kind": self.run_kind,
            "interrupt": self.interrupt.to_dict() if self.interrupt else None,
            "error": self.error.to_dict() if self.error else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ToolExecutionRecord:
    invocation_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    arguments_hash: str
    status: ToolExecutionStatus
    attempt: int = 0
    result: str | None = None
    is_error: bool = False
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments_hash": self.arguments_hash,
            "status": self.status.value,
            "attempt": self.attempt,
            "result": self.result,
            "is_error": self.is_error,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolExecutionRecord:
        return cls(
            invocation_id=str(data["invocation_id"]),
            run_id=str(data["run_id"]),
            tool_call_id=str(data.get("tool_call_id") or ""),
            tool_name=str(data["tool_name"]),
            arguments_hash=str(data["arguments_hash"]),
            status=ToolExecutionStatus(str(data["status"])),
            attempt=int(data.get("attempt") or 0),
            result=_optional_str(data.get("result")),
            is_error=bool(data.get("is_error")),
            error=_optional_str(data.get("error")),
            started_at=_optional_str(data.get("started_at")),
            completed_at=_optional_str(data.get("completed_at")),
            updated_at=str(data.get("updated_at") or datetime.now(UTC).isoformat()),
        )


@dataclass(slots=True)
class BudgetLedgerRecord:
    owner_run_id: str
    version: int
    state: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_run_id": self.owner_run_id,
            "version": self.version,
            "state": _json_value(self.state),
        }


def _message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": _json_value(message.content),
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": _json_value(message.tool_calls),
    }


def _message_from_dict(data: dict[str, Any]) -> Message:
    role = str(data.get("role") or "user")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"invalid persisted message role: {role}")
    content = data.get("content", "")
    if not isinstance(content, (str, list)):
        raise ValueError("persisted message content must be a string or list")
    raw_calls = data.get("tool_calls")
    return Message(
        role=role,  # type: ignore[arg-type]
        content=content,
        name=_optional_str(data.get("name")),
        tool_call_id=_optional_str(data.get("tool_call_id")),
        tool_calls=[item for item in raw_calls if isinstance(item, dict)]
        if isinstance(raw_calls, list)
        else [],
    )


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise TypeError(f"value is not checkpoint-serializable: {type(value).__name__}")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"
