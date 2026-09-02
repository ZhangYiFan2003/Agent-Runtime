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
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: str | None = None
    cost_known: bool = False
    elapsed_seconds: float | None = None
    budget_policy: dict[str, Any] = field(default_factory=dict)
    budget_usage: dict[str, Any] = field(default_factory=dict)
    aggregate_budget_usage: dict[str, Any] = field(default_factory=dict)
    budget_remaining: dict[str, Any] = field(default_factory=dict)
    aggregate_budget_remaining: dict[str, Any] = field(default_factory=dict)
    budget_utilization: dict[str, float] = field(default_factory=dict)
    aggregate_budget_utilization: dict[str, float] = field(default_factory=dict)
    budget_soft_limit_reached: bool = False
    budget_hard_limit_reached: bool = False
    budget_exceeded_dimension: str | None = None

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
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": self.cost_usd,
            "cost_known": self.cost_known,
            "elapsed_seconds": self.elapsed_seconds,
            "budget_policy": dict(self.budget_policy),
            "budget_usage": dict(self.budget_usage),
            "aggregate_budget_usage": dict(self.aggregate_budget_usage),
            "budget_remaining": dict(self.budget_remaining),
            "aggregate_budget_remaining": dict(self.aggregate_budget_remaining),
            "budget_utilization": dict(self.budget_utilization),
            "aggregate_budget_utilization": dict(self.aggregate_budget_utilization),
            "budget_soft_limit_reached": self.budget_soft_limit_reached,
            "budget_hard_limit_reached": self.budget_hard_limit_reached,
            "budget_exceeded_dimension": self.budget_exceeded_dimension,
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
        root = next(
            (
                span
                for span in spans
                if span.span_type == SpanType.AGENT and span.parent_span_id is None
            ),
            None,
        )
        budget = root.attributes if root is not None else {}
        budget_policy = {
            key.removeprefix("budget."): value
            for key, value in budget.items()
            if key.startswith("budget.max_") or key == "budget.soft_limit_ratio"
        }
        budget_usage = _budget_attributes(budget, "budget.", aggregate=False)
        aggregate_usage = _budget_attributes(budget, "budget.aggregate_", aggregate=True)
        remaining = {
            key.removeprefix("budget.remaining_"): value
            for key, value in budget.items()
            if key.startswith("budget.remaining_")
        }
        aggregate_remaining = {
            key.removeprefix("budget.aggregate_remaining_"): value
            for key, value in budget.items()
            if key.startswith("budget.aggregate_remaining_")
        }
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
            cached_input_tokens=int(budget.get("budget.cached_input_tokens_used") or 0),
            reasoning_tokens=int(budget.get("budget.reasoning_tokens_used") or 0),
            cost_usd=_optional_string(budget.get("budget.cost_usd")),
            cost_known=bool(budget.get("budget.cost_known")),
            elapsed_seconds=_optional_number(budget.get("budget.elapsed_seconds")),
            budget_policy=budget_policy,
            budget_usage=budget_usage,
            aggregate_budget_usage=aggregate_usage,
            budget_remaining=remaining,
            aggregate_budget_remaining=aggregate_remaining,
            budget_utilization=_budget_utilization(budget_policy, budget_usage),
            aggregate_budget_utilization=_budget_utilization(budget_policy, aggregate_usage),
            budget_soft_limit_reached=bool(budget.get("budget.soft_limit_reached")),
            budget_hard_limit_reached=bool(budget.get("budget.hard_limit_reached")),
            budget_exceeded_dimension=_optional_string(budget.get("budget.exceeded_dimension")),
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


def _budget_attributes(
    attributes: dict[str, Any], prefix: str, *, aggregate: bool
) -> dict[str, Any]:
    names = (
        "steps_used",
        "model_calls_used",
        "tool_calls_used",
        "input_tokens_used",
        "output_tokens_used",
        "total_tokens_used",
        "cost_usd",
        "cost_known",
    )
    values: dict[str, Any] = {}
    for name in names:
        key = f"{prefix}{name}"
        if key in attributes:
            values[name.removesuffix("_used")] = attributes[key]
    aggregate_elapsed = attributes.get("budget.aggregate_elapsed_seconds")
    if aggregate and isinstance(aggregate_elapsed, (int, float)):
        values["elapsed_seconds"] = aggregate_elapsed
    if not aggregate and "budget.elapsed_seconds" in attributes:
        values["elapsed_seconds"] = attributes["budget.elapsed_seconds"]
    return values


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _budget_utilization(policy: dict[str, Any], usage: dict[str, Any]) -> dict[str, float]:
    utilization: dict[str, float] = {}
    for dimension in ("steps", "model_calls", "tool_calls", "input_tokens", "output_tokens"):
        limit = policy.get(f"max_{dimension}")
        used = usage.get(dimension)
        if isinstance(limit, (int, float)) and limit > 0 and isinstance(used, (int, float)):
            utilization[dimension] = round(float(used) / float(limit), 6)
    total_limit = policy.get("max_total_tokens")
    total_used = usage.get("total_tokens")
    if (
        isinstance(total_limit, (int, float))
        and total_limit > 0
        and isinstance(total_used, (int, float))
    ):
        utilization["total_tokens"] = round(float(total_used) / float(total_limit), 6)
    wall_limit = policy.get("max_wall_time_seconds")
    elapsed = usage.get("elapsed_seconds")
    if (
        isinstance(wall_limit, (int, float))
        and wall_limit > 0
        and isinstance(elapsed, (int, float))
    ):
        utilization["elapsed_seconds"] = round(float(elapsed) / float(wall_limit), 6)
    cost_limit = policy.get("max_cost_usd")
    cost_used = usage.get("cost_usd")
    try:
        if cost_limit is not None and cost_used is not None and float(cost_limit) > 0:
            utilization["cost_usd"] = round(float(cost_used) / float(cost_limit), 6)
    except (TypeError, ValueError):
        pass
    return utilization


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None
