from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

OBSERVABILITY_SCHEMA_VERSION = 1


class SpanType(StrEnum):
    AGENT = "agent"
    LLM = "llm"
    TOOL = "tool"
    CHECKPOINT = "checkpoint"
    INTERRUPT = "interrupt"
    POLICY = "policy"


class SpanStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class Trace:
    trace_id: str
    run_id: str
    thread_id: str
    turn_id: str
    started_at: str
    status: str
    ended_at: str | None = None
    schema_version: int = OBSERVABILITY_SCHEMA_VERSION

    @property
    def total_latency_ms(self) -> float | None:
        return duration_ms(self.started_at, self.ended_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "total_latency_ms": self.total_latency_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trace:
        return cls(
            trace_id=str(data["trace_id"]),
            run_id=str(data["run_id"]),
            thread_id=str(data["thread_id"]),
            turn_id=str(data["turn_id"]),
            started_at=str(data["started_at"]),
            ended_at=_optional_str(data.get("ended_at")),
            status=str(data.get("status") or "RUNNING"),
            schema_version=int(data.get("schema_version") or OBSERVABILITY_SCHEMA_VERSION),
        )


@dataclass(slots=True)
class Span:
    span_id: str
    trace_id: str
    span_type: SpanType
    name: str
    started_at: str
    status: SpanStatus = SpanStatus.RUNNING
    parent_span_id: str | None = None
    ended_at: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    schema_version: int = OBSERVABILITY_SCHEMA_VERSION

    @property
    def latency_ms(self) -> float | None:
        value = self.attributes.get("latency_ms")
        if isinstance(value, (int, float)):
            return float(value)
        return duration_ms(self.started_at, self.ended_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "span_type": self.span_type.value,
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "attributes": json_attributes(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Span:
        return cls(
            span_id=str(data["span_id"]),
            trace_id=str(data["trace_id"]),
            parent_span_id=_optional_str(data.get("parent_span_id")),
            span_type=SpanType(str(data["span_type"])),
            name=str(data["name"]),
            started_at=str(data["started_at"]),
            ended_at=_optional_str(data.get("ended_at")),
            status=SpanStatus(str(data.get("status") or SpanStatus.RUNNING)),
            attributes=_dict(data.get("attributes")),
            schema_version=int(data.get("schema_version") or OBSERVABILITY_SCHEMA_VERSION),
        )


@dataclass(frozen=True, slots=True)
class RunMetrics:
    trace_id: str
    run_id: str
    status: str
    duration_ms: float | None
    step_count: int
    llm_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tool_successes: int
    tool_failures: int
    tool_success_rate: float | None
    checkpoint_count: int
    interrupt_count: int
    resume_count: int
    retry_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "step_count": self.step_count,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "tool_successes": self.tool_successes,
            "tool_failures": self.tool_failures,
            "tool_success_rate": self.tool_success_rate,
            "checkpoint_count": self.checkpoint_count,
            "interrupt_count": self.interrupt_count,
            "resume_count": self.resume_count,
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_trace(cls, trace: Trace, spans: list[Span]) -> RunMetrics:
        llm = [span for span in spans if span.span_type == SpanType.LLM]
        tools = [span for span in spans if span.span_type == SpanType.TOOL]
        steps = [
            span for span in spans if span.span_type == SpanType.AGENT and span.name == "agent.step"
        ]
        successes = sum(span.status == SpanStatus.SUCCEEDED for span in tools)
        failures = sum(span.status == SpanStatus.FAILED for span in tools)
        decided_tools = successes + failures
        prompt_tokens = sum(_int_attribute(span, "prompt_tokens") for span in llm)
        completion_tokens = sum(_int_attribute(span, "completion_tokens") for span in llm)
        return cls(
            trace_id=trace.trace_id,
            run_id=trace.run_id,
            status=trace.status,
            duration_ms=trace.total_latency_ms,
            step_count=len(steps),
            llm_calls=len(llm),
            tool_calls=len(tools),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            tool_successes=successes,
            tool_failures=failures,
            tool_success_rate=round(successes / decided_tools, 4) if decided_tools else None,
            checkpoint_count=sum(
                span.span_type == SpanType.CHECKPOINT and span.status == SpanStatus.SUCCEEDED
                for span in spans
            ),
            interrupt_count=sum(span.span_type == SpanType.INTERRUPT for span in spans),
            resume_count=sum(
                span.span_type == SpanType.AGENT and span.name == "resume" for span in spans
            ),
            retry_count=(
                sum(_int_attribute(span, "retry_count") > 0 for span in llm)
                + sum(_int_attribute(span, "retry_count") for span in tools)
            ),
        )


@dataclass(frozen=True, slots=True)
class TraceBundle:
    trace: Trace
    spans: list[Span]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace": self.trace.to_dict(),
            "spans": [span.to_dict() for span in self.spans],
        }


def new_span_id() -> str:
    return f"span_{uuid4().hex}"


def trace_id_for_run(run_id: str) -> str:
    return f"trace_{hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:32]}"


def root_span_id_for_run(run_id: str) -> str:
    return f"span_run_{hashlib.sha256(run_id.encode('utf-8')).hexdigest()[:32]}"


def tool_span_id(invocation_id: str) -> str:
    value = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:32]
    return f"span_tool_{value}"


def now() -> str:
    return datetime.now(UTC).isoformat()


def duration_ms(started_at: str, ended_at: str | None) -> float | None:
    if ended_at is None:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except ValueError:
        return None
    return round(max(0.0, (end - start).total_seconds() * 1000), 3)


def json_attributes(value: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise TypeError(f"span attribute is not JSON-compatible: {type(value).__name__}")


def _int_attribute(span: Span, key: str) -> int:
    value = span.attributes.get(key)
    return int(value) if isinstance(value, (int, float)) else 0


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
