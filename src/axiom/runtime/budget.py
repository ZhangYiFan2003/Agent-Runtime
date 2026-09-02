from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from axiom.config import RunBudgetConfig
from axiom.runtime.checkpoints import BudgetLedgerConflictError, RuntimeStore
from axiom.runtime.models import BudgetLedgerRecord, Checkpoint
from axiom.runtime.observability import root_span_id_for_run
from axiom.runtime.observability_store import ObservabilityStore

BUDGET_LEDGER_SCHEMA_VERSION = 1

_LIMIT_FIELDS = (
    "max_steps",
    "max_model_calls",
    "max_tool_calls",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_wall_time_seconds",
    "max_cost_usd",
)
_DIMENSION_LIMIT = {
    "steps": ("max_steps", "STEP_BUDGET_EXCEEDED"),
    "model_calls": ("max_model_calls", "MODEL_CALL_BUDGET_EXCEEDED"),
    "tool_calls": ("max_tool_calls", "TOOL_CALL_BUDGET_EXCEEDED"),
    "input_tokens": ("max_input_tokens", "INPUT_TOKEN_BUDGET_EXCEEDED"),
    "output_tokens": ("max_output_tokens", "OUTPUT_TOKEN_BUDGET_EXCEEDED"),
    "total_tokens": ("max_total_tokens", "TOTAL_TOKEN_BUDGET_EXCEEDED"),
    "elapsed_seconds": ("max_wall_time_seconds", "WALL_TIME_BUDGET_EXCEEDED"),
    "cost_usd": ("max_cost_usd", "COST_BUDGET_EXCEEDED"),
}


@dataclass(frozen=True, slots=True)
class RunBudgetPolicy:
    max_steps: int | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_wall_time_seconds: float | None = None
    max_cost_usd: Decimal | None = None
    soft_limit_ratio: float = 0.80

    def __post_init__(self) -> None:
        for name in _LIMIT_FIELDS[:-2]:
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ValueError(f"{name} must be a positive integer or null")
        if self.max_wall_time_seconds is not None and self.max_wall_time_seconds <= 0:
            raise ValueError("max_wall_time_seconds must be positive or null")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be positive or null")
        if not 0 < self.soft_limit_ratio < 1:
            raise ValueError("soft_limit_ratio must be greater than 0 and less than 1")

    @classmethod
    def from_config(cls, config: RunBudgetConfig) -> RunBudgetPolicy:
        return cls(
            max_steps=config.max_steps,
            max_model_calls=config.max_model_calls,
            max_tool_calls=config.max_tool_calls,
            max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens,
            max_total_tokens=config.max_total_tokens,
            max_wall_time_seconds=config.max_wall_time_seconds,
            max_cost_usd=_optional_decimal(config.max_cost_usd),
            soft_limit_ratio=config.soft_limit_ratio,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunBudgetPolicy:
        return cls(
            max_steps=_optional_int(data.get("max_steps")),
            max_model_calls=_optional_int(data.get("max_model_calls")),
            max_tool_calls=_optional_int(data.get("max_tool_calls")),
            max_input_tokens=_optional_int(data.get("max_input_tokens")),
            max_output_tokens=_optional_int(data.get("max_output_tokens")),
            max_total_tokens=_optional_int(data.get("max_total_tokens")),
            max_wall_time_seconds=_optional_float(data.get("max_wall_time_seconds")),
            max_cost_usd=_optional_decimal(data.get("max_cost_usd")),
            soft_limit_ratio=float(data.get("soft_limit_ratio", 0.80)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_model_calls": self.max_model_calls,
            "max_tool_calls": self.max_tool_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_wall_time_seconds": self.max_wall_time_seconds,
            "max_cost_usd": _decimal_text(self.max_cost_usd),
            "soft_limit_ratio": self.soft_limit_ratio,
        }


@dataclass(frozen=True, slots=True)
class ModelPricing:
    provider: str
    model: str
    input_per_million_usd: Decimal
    output_per_million_usd: Decimal
    cached_input_per_million_usd: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("pricing provider and model are required")
        for name in (
            "input_per_million_usd",
            "output_per_million_usd",
            "cached_input_per_million_usd",
        ):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")

    def cost(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
    ) -> Decimal:
        cached = min(max(0, cached_input_tokens), max(0, input_tokens))
        regular = max(0, input_tokens) - cached
        cached_rate = self.cached_input_per_million_usd
        if cached_rate is None:
            cached_rate = self.input_per_million_usd
        million = Decimal(1_000_000)
        return (
            Decimal(regular) * self.input_per_million_usd
            + Decimal(cached) * cached_rate
            + Decimal(max(0, output_tokens)) * self.output_per_million_usd
        ) / million

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_per_million_usd": str(self.input_per_million_usd),
            "output_per_million_usd": str(self.output_per_million_usd),
            "cached_input_per_million_usd": _decimal_text(self.cached_input_per_million_usd),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelPricing:
        return cls(
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            input_per_million_usd=_required_decimal(
                data.get("input_per_million_usd"), "input_per_million_usd"
            ),
            output_per_million_usd=_required_decimal(
                data.get("output_per_million_usd"), "output_per_million_usd"
            ),
            cached_input_per_million_usd=_optional_decimal(
                data.get("cached_input_per_million_usd")
            ),
        )


class ModelPricingRegistry:
    def __init__(self, entries: list[ModelPricing] | None = None) -> None:
        self._entries = {
            (entry.provider.casefold(), entry.model.casefold()): entry for entry in entries or []
        }

    @classmethod
    def from_config(cls, values: dict[str, dict[str, str | float]]) -> ModelPricingRegistry:
        entries = []
        for key, raw in values.items():
            if not isinstance(raw, dict):
                raise ValueError(f"model pricing entry {key!r} must be an object")
            provider, separator, model = key.partition("/")
            if not separator:
                raise ValueError("model pricing keys must use provider/model")
            entries.append(ModelPricing.from_dict({"provider": provider, "model": model, **raw}))
        return cls(entries)

    def resolve(self, provider: str, model: str) -> ModelPricing | None:
        return self._entries.get((provider.casefold(), model.casefold()))


@dataclass(frozen=True, slots=True)
class RunBudgetUsage:
    steps: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Decimal | None = None
    cost_known: bool = False
    elapsed_seconds: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": _decimal_text(self.cost_usd),
            "cost_known": self.cost_known,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


@dataclass(frozen=True, slots=True)
class RunBudgetState:
    owner_run_id: str
    run_id: str
    policy: RunBudgetPolicy
    local_usage: RunBudgetUsage
    aggregate_usage: RunBudgetUsage
    remaining: dict[str, int | float | str | None]
    aggregate_remaining: dict[str, int | float | str | None]
    soft_limit_reached: bool
    soft_dimensions: tuple[str, ...]
    hard_limit_reached: bool
    exceeded_dimension: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_run_id": self.owner_run_id,
            "run_id": self.run_id,
            "policy": self.policy.to_dict(),
            "local_usage": self.local_usage.to_dict(),
            "aggregate_usage": self.aggregate_usage.to_dict(),
            "remaining": dict(self.remaining),
            "aggregate_remaining": dict(self.aggregate_remaining),
            "soft_limit_reached": self.soft_limit_reached,
            "soft_dimensions": list(self.soft_dimensions),
            "hard_limit_reached": self.hard_limit_reached,
            "exceeded_dimension": self.exceeded_dimension,
        }

    def observability_attributes(self) -> dict[str, object]:
        attributes: dict[str, object] = {
            "budget.owner_run_id": self.owner_run_id,
            "budget.parent_run_id": self.owner_run_id if self.owner_run_id != self.run_id else None,
            "budget.steps_used": self.local_usage.steps,
            "budget.model_calls_used": self.local_usage.model_calls,
            "budget.tool_calls_used": self.local_usage.tool_calls,
            "budget.input_tokens_used": self.local_usage.input_tokens,
            "budget.output_tokens_used": self.local_usage.output_tokens,
            "budget.total_tokens_used": self.local_usage.total_tokens,
            "budget.cached_input_tokens_used": self.local_usage.cached_input_tokens,
            "budget.reasoning_tokens_used": self.local_usage.reasoning_tokens,
            "budget.cost_usd": _decimal_text(self.local_usage.cost_usd),
            "budget.cost_known": self.local_usage.cost_known,
            "budget.elapsed_seconds": self.local_usage.elapsed_seconds,
            "budget.aggregate_steps_used": self.aggregate_usage.steps,
            "budget.aggregate_model_calls_used": self.aggregate_usage.model_calls,
            "budget.aggregate_tool_calls_used": self.aggregate_usage.tool_calls,
            "budget.aggregate_input_tokens_used": self.aggregate_usage.input_tokens,
            "budget.aggregate_output_tokens_used": self.aggregate_usage.output_tokens,
            "budget.aggregate_total_tokens_used": self.aggregate_usage.total_tokens,
            "budget.aggregate_cached_input_tokens_used": (self.aggregate_usage.cached_input_tokens),
            "budget.aggregate_reasoning_tokens_used": (self.aggregate_usage.reasoning_tokens),
            "budget.aggregate_cost_usd": _decimal_text(self.aggregate_usage.cost_usd),
            "budget.aggregate_cost_known": self.aggregate_usage.cost_known,
            "budget.aggregate_elapsed_seconds": self.aggregate_usage.elapsed_seconds,
            "budget.soft_limit_reached": self.soft_limit_reached,
            "budget.soft_dimensions": list(self.soft_dimensions),
            "budget.hard_limit_reached": self.hard_limit_reached,
            "budget.exceeded_dimension": self.exceeded_dimension,
        }
        for field_name in _LIMIT_FIELDS:
            value = getattr(self.policy, field_name)
            attributes[f"budget.{field_name}"] = (
                _decimal_text(value) if isinstance(value, Decimal) else value
            )
        attributes["budget.soft_limit_ratio"] = self.policy.soft_limit_ratio
        for dimension, value in self.remaining.items():
            attributes[f"budget.remaining_{dimension}"] = value
        for dimension, value in self.aggregate_remaining.items():
            attributes[f"budget.aggregate_remaining_{dimension}"] = value
        return attributes


class BudgetExceededError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        dimension: str,
        limit: int | float | Decimal,
        used: int | float | Decimal,
        run_id: str,
    ) -> None:
        self.code = code
        self.dimension = dimension
        self.limit = limit
        self.used = used
        self.run_id = run_id
        self.remaining = max(Decimal(0), Decimal(str(limit)) - Decimal(str(used)))
        super().__init__(
            f"{dimension} budget exhausted: used={used}, limit={limit}, run_id={run_id}"
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "limit": str(self.limit) if isinstance(self.limit, Decimal) else self.limit,
            "used": str(self.used) if isinstance(self.used, Decimal) else self.used,
            "remaining": str(self.remaining),
            "run_id": self.run_id,
        }


class BudgetManager:
    """Atomic, restart-safe ledger shared by a root Run and all descendants."""

    def __init__(
        self,
        store: RuntimeStore,
        *,
        policy: RunBudgetPolicy,
        provider: str,
        model: str,
        pricing_registry: ModelPricingRegistry | None = None,
        observability_store: ObservabilityStore | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.provider = provider
        self.model = model
        self.pricing_registry = pricing_registry or ModelPricingRegistry()
        self.pricing = self.pricing_registry.resolve(provider, model)
        self.observability_store = observability_store
        self._lock = asyncio.Lock()
        self._elapsed_anchors: dict[str, tuple[float, float]] = {}
        if policy.max_cost_usd is not None and self.pricing is None:
            raise ValueError(f"max_cost_usd requires configured pricing for {provider}/{model}")

    async def initialize(self, state: Checkpoint) -> RunBudgetState:
        owner = state.budget_owner_run_id or state.run_id
        state.budget_owner_run_id = owner
        if not state.budget_policy:
            state.budget_policy = self.policy.to_dict()
        policy = RunBudgetPolicy.from_dict(state.budget_policy)

        def mutate(data: dict[str, Any]) -> None:
            runs = data.setdefault("runs", {})
            runs.setdefault(
                state.run_id,
                {
                    "policy": policy.to_dict(),
                    "created_at": state.created_at,
                    "pricing": self.pricing.to_dict() if self.pricing else None,
                    "usage": _empty_usage(self.pricing is not None),
                    "soft_dimensions": [],
                    "hard_dimension": None,
                },
            )

        record = await self._mutate(owner, mutate)
        snapshot = self._snapshot(record, state.run_id)
        await self._publish(snapshot)
        return snapshot

    async def consume_step(self, state: Checkpoint, operation_id: str) -> RunBudgetState:
        return await self._consume_discrete(state, operation_id, "steps")

    async def consume_tool_call(self, state: Checkpoint, operation_id: str) -> RunBudgetState:
        return await self._consume_discrete(state, operation_id, "tool_calls")

    async def reserve_model_call(
        self,
        state: Checkpoint,
        operation_id: str,
        *,
        estimated_input_tokens: int = 0,
    ) -> RunBudgetState:
        owner = state.budget_owner_run_id or state.run_id

        def mutate(data: dict[str, Any]) -> None:
            operations = data.setdefault("operations", {})
            if operation_id in operations:
                return
            self._preflight(data, state.run_id, "model_calls", 1)
            estimate = max(0, int(estimated_input_tokens))
            self._preflight(data, state.run_id, "input_tokens", estimate)
            self._preflight(data, state.run_id, "output_tokens", 0)
            self._preflight(data, state.run_id, "total_tokens", estimate)
            run = _run_entry(data, state.run_id)
            pricing = _pricing_from_entry(run)
            reserved_cost = (
                pricing.cost(input_tokens=estimate, output_tokens=0)
                if pricing is not None
                else None
            )
            if reserved_cost is not None:
                self._preflight(data, state.run_id, "cost_usd", reserved_cost)
            run["usage"]["model_calls"] += 1
            operations[operation_id] = {
                "run_id": state.run_id,
                "kind": "model",
                "status": "reserved",
                "reserved_input_tokens": estimate,
                "reserved_cost_usd": _decimal_text(reserved_cost),
            }
            self._mark_soft(data, state.run_id)

        record = await self._mutate(owner, mutate)
        snapshot = self._snapshot(record, state.run_id)
        await self._publish(snapshot)
        return snapshot

    async def complete_model_call(
        self,
        state: Checkpoint,
        operation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> RunBudgetState:
        owner = state.budget_owner_run_id or state.run_id
        violation: list[BudgetExceededError] = []

        def mutate(data: dict[str, Any]) -> None:
            operations = data.setdefault("operations", {})
            operation = operations.get(operation_id)
            if operation is None:
                raise RuntimeError(f"model budget reservation not found: {operation_id}")
            if operation.get("status") == "completed":
                return
            run = _run_entry(data, state.run_id)
            pricing = _pricing_from_entry(run)
            actual_input = max(0, int(input_tokens))
            actual_output = max(0, int(output_tokens))
            cached = min(max(0, int(cached_input_tokens)), actual_input)
            run["usage"]["input_tokens"] += actual_input
            run["usage"]["output_tokens"] += actual_output
            run["usage"]["cached_input_tokens"] += cached
            run["usage"]["reasoning_tokens"] += max(0, int(reasoning_tokens))
            if pricing is not None:
                current = Decimal(str(run["usage"].get("cost_usd") or "0"))
                run["usage"]["cost_usd"] = str(
                    current
                    + pricing.cost(
                        input_tokens=actual_input,
                        output_tokens=actual_output,
                        cached_input_tokens=cached,
                    )
                )
                run["usage"]["cost_known"] = True
            operation.update(
                {
                    "status": "completed",
                    "input_tokens": actual_input,
                    "output_tokens": actual_output,
                    "cached_input_tokens": cached,
                    "reasoning_tokens": max(0, int(reasoning_tokens)),
                }
            )
            error = self._first_violation(data, state.run_id)
            if error is not None:
                run["hard_dimension"] = error.dimension
                self._mark_owner_hard_if_exhausted(data, error.dimension)
                violation.append(error)
            self._mark_soft(data, state.run_id)

        record = await self._mutate(owner, mutate)
        snapshot = self._snapshot(record, state.run_id)
        await self._publish(snapshot)
        if violation:
            raise violation[0]
        return snapshot

    async def preflight_child(self, state: Checkpoint) -> RunBudgetState:
        snapshot = await self.snapshot(state)
        for dimension, (limit_name, code) in _DIMENSION_LIMIT.items():
            limit = getattr(snapshot.policy, limit_name)
            if limit is None:
                continue
            used = _usage_dimension(snapshot.aggregate_usage, dimension)
            if Decimal(str(used)) >= Decimal(str(limit)):
                raise BudgetExceededError(
                    code=code,
                    dimension=dimension,
                    limit=limit,
                    used=used,
                    run_id=state.run_id,
                )
        return snapshot

    async def snapshot(self, state: Checkpoint) -> RunBudgetState:
        owner = state.budget_owner_run_id or state.run_id
        record = await self.store.load_budget_ledger(owner)
        if record is None or state.run_id not in record.state.get("runs", {}):
            return await self.initialize(state)
        snapshot = self._snapshot(record, state.run_id)
        await self._publish(snapshot)
        return snapshot

    async def record_hard_limit(
        self, state: Checkpoint, error: BudgetExceededError
    ) -> RunBudgetState:
        """Persist a hard-limit decision that happened during a preflight check."""

        owner = state.budget_owner_run_id or state.run_id

        def mutate(data: dict[str, Any]) -> None:
            run = _run_entry(data, state.run_id)
            run["hard_dimension"] = error.dimension
            self._mark_owner_hard_if_exhausted(data, error.dimension)

        record = await self._mutate(owner, mutate)
        snapshot = self._snapshot(record, state.run_id)
        await self._publish(snapshot)
        return snapshot

    async def _consume_discrete(
        self,
        state: Checkpoint,
        operation_id: str,
        dimension: str,
    ) -> RunBudgetState:
        owner = state.budget_owner_run_id or state.run_id

        def mutate(data: dict[str, Any]) -> None:
            operations = data.setdefault("operations", {})
            if operation_id in operations:
                return
            self._preflight(data, state.run_id, dimension, 1)
            run = _run_entry(data, state.run_id)
            run["usage"][dimension] += 1
            operations[operation_id] = {
                "run_id": state.run_id,
                "kind": dimension,
                "status": "completed",
            }
            self._mark_soft(data, state.run_id)

        record = await self._mutate(owner, mutate)
        snapshot = self._snapshot(record, state.run_id)
        await self._publish(snapshot)
        return snapshot

    def _preflight(
        self,
        data: dict[str, Any],
        run_id: str,
        dimension: str,
        amount: int | Decimal,
    ) -> None:
        self._check_wall_time(data, run_id)
        local = _usage(data, run_id, include_reservations=True)
        aggregate = _aggregate_usage(data, include_reservations=True)
        local_policy = RunBudgetPolicy.from_dict(_run_entry(data, run_id)["policy"])
        owner_policy = RunBudgetPolicy.from_dict(
            _run_entry(data, str(data["owner_run_id"]))["policy"]
        )
        for usage, policy in ((local, local_policy), (aggregate, owner_policy)):
            limit_name, code = _DIMENSION_LIMIT[dimension]
            limit = getattr(policy, limit_name)
            if limit is None:
                continue
            used = _usage_dimension(usage, dimension)
            projected = Decimal(str(used)) + Decimal(amount)
            exhausted = (
                projected >= Decimal(str(limit)) if amount == 0 else projected > Decimal(str(limit))
            )
            if exhausted:
                raise BudgetExceededError(
                    code=code,
                    dimension=dimension,
                    limit=limit,
                    used=used,
                    run_id=run_id,
                )

    def _check_wall_time(self, data: dict[str, Any], run_id: str) -> None:
        for target in (run_id, str(data["owner_run_id"])):
            run = _run_entry(data, target)
            policy = RunBudgetPolicy.from_dict(run["policy"])
            if policy.max_wall_time_seconds is None:
                continue
            elapsed = self._elapsed_for(target, run["created_at"])
            if elapsed >= policy.max_wall_time_seconds:
                raise BudgetExceededError(
                    code="WALL_TIME_BUDGET_EXCEEDED",
                    dimension="elapsed_seconds",
                    limit=policy.max_wall_time_seconds,
                    used=elapsed,
                    run_id=run_id,
                )

    def _first_violation(self, data: dict[str, Any], run_id: str) -> BudgetExceededError | None:
        for usage, policy in (
            (
                _usage(data, run_id),
                RunBudgetPolicy.from_dict(_run_entry(data, run_id)["policy"]),
            ),
            (
                _aggregate_usage(data),
                RunBudgetPolicy.from_dict(_run_entry(data, str(data["owner_run_id"]))["policy"]),
            ),
        ):
            for dimension, (limit_name, code) in _DIMENSION_LIMIT.items():
                if dimension == "elapsed_seconds":
                    continue
                limit = getattr(policy, limit_name)
                if limit is None:
                    continue
                used = _usage_dimension(usage, dimension)
                if Decimal(str(used)) > Decimal(str(limit)):
                    return BudgetExceededError(
                        code=code,
                        dimension=dimension,
                        limit=limit,
                        used=used,
                        run_id=run_id,
                    )
        return None

    def _mark_soft(self, data: dict[str, Any], run_id: str) -> None:
        owner_run_id = str(data["owner_run_id"])
        for target in {run_id, owner_run_id}:
            run = _run_entry(data, target)
            usage = (
                _aggregate_usage(data, include_reservations=True)
                if target == owner_run_id
                else _usage(data, target, include_reservations=True)
            )
            policy = RunBudgetPolicy.from_dict(run["policy"])
            dimensions = []
            for dimension, (limit_name, _code) in _DIMENSION_LIMIT.items():
                limit = getattr(policy, limit_name)
                if limit is None:
                    continue
                used = _usage_dimension(usage, dimension)
                if Decimal(str(used)) >= Decimal(str(limit)) * Decimal(
                    str(policy.soft_limit_ratio)
                ):
                    dimensions.append(dimension)
            run["soft_dimensions"] = sorted(set(dimensions))

    def _mark_owner_hard_if_exhausted(self, data: dict[str, Any], dimension: str) -> None:
        owner = _run_entry(data, str(data["owner_run_id"]))
        policy = RunBudgetPolicy.from_dict(owner["policy"])
        limit_name, _code = _DIMENSION_LIMIT[dimension]
        limit = getattr(policy, limit_name)
        if limit is None:
            return
        used = _usage_dimension(_aggregate_usage(data), dimension)
        if Decimal(str(used)) >= Decimal(str(limit)):
            owner["hard_dimension"] = dimension

    async def _mutate(
        self,
        owner_run_id: str,
        mutate: Callable[[dict[str, Any]], None],
    ) -> BudgetLedgerRecord:
        async with self._lock:
            for _attempt in range(32):
                record = await self.store.load_budget_ledger(owner_run_id)
                if record is None:
                    record = BudgetLedgerRecord(
                        owner_run_id=owner_run_id,
                        version=0,
                        state={
                            "schema_version": BUDGET_LEDGER_SCHEMA_VERSION,
                            "owner_run_id": owner_run_id,
                            "runs": {},
                            "operations": {},
                        },
                    )
                mutate(record.state)
                try:
                    await self.store.save_budget_ledger(record)
                except BudgetLedgerConflictError:
                    continue
                return record
        raise BudgetLedgerConflictError(
            f"could not update budget ledger for {owner_run_id} after concurrent writes"
        )

    def _snapshot(self, record: BudgetLedgerRecord, run_id: str) -> RunBudgetState:
        run = _run_entry(record.state, run_id)
        policy = RunBudgetPolicy.from_dict(run["policy"])
        local = _usage(record.state, run_id)
        aggregate = _aggregate_usage(record.state)
        local = replace(
            local,
            elapsed_seconds=self._elapsed_for(run_id, run["created_at"]),
        )
        owner = _run_entry(record.state, record.owner_run_id)
        aggregate = replace(
            aggregate,
            elapsed_seconds=self._elapsed_for(record.owner_run_id, owner["created_at"]),
        )
        remaining: dict[str, int | float | str | None] = {}
        for dimension, (limit_name, _code) in _DIMENSION_LIMIT.items():
            limit = getattr(policy, limit_name)
            if limit is None:
                remaining[dimension] = None
                continue
            used = _usage_dimension(local, dimension)
            value = max(Decimal(0), Decimal(str(limit)) - Decimal(str(used)))
            remaining[dimension] = str(value) if isinstance(limit, Decimal) else float(value)
        owner_policy = RunBudgetPolicy.from_dict(
            _run_entry(record.state, record.owner_run_id)["policy"]
        )
        aggregate_remaining: dict[str, int | float | str | None] = {}
        for dimension, (limit_name, _code) in _DIMENSION_LIMIT.items():
            limit = getattr(owner_policy, limit_name)
            if limit is None:
                aggregate_remaining[dimension] = None
                continue
            used = _usage_dimension(aggregate, dimension)
            value = max(Decimal(0), Decimal(str(limit)) - Decimal(str(used)))
            aggregate_remaining[dimension] = (
                str(value) if isinstance(limit, Decimal) else float(value)
            )
        return RunBudgetState(
            owner_run_id=record.owner_run_id,
            run_id=run_id,
            policy=policy,
            local_usage=local,
            aggregate_usage=aggregate,
            remaining=remaining,
            aggregate_remaining=aggregate_remaining,
            soft_limit_reached=bool(run.get("soft_dimensions")),
            soft_dimensions=tuple(str(item) for item in run.get("soft_dimensions", [])),
            hard_limit_reached=bool(run.get("hard_dimension")),
            exceeded_dimension=str(run.get("hard_dimension"))
            if run.get("hard_dimension")
            else None,
        )

    def _elapsed_for(self, run_id: str, created_at: str) -> float:
        current = time.monotonic()
        anchor = self._elapsed_anchors.get(run_id)
        if anchor is None:
            anchor = (current, _elapsed(created_at))
            self._elapsed_anchors[run_id] = anchor
        return max(0.0, anchor[1] + current - anchor[0])

    async def _publish(self, snapshot: RunBudgetState) -> None:
        if self.observability_store is None:
            return
        snapshots = {snapshot.run_id: snapshot}
        if snapshot.owner_run_id != snapshot.run_id:
            record = await self.store.load_budget_ledger(snapshot.owner_run_id)
            if record is not None:
                snapshots[snapshot.owner_run_id] = self._snapshot(record, snapshot.owner_run_id)
        for run_id, run_snapshot in snapshots.items():
            span = await self.observability_store.load_span(root_span_id_for_run(run_id))
            if span is None:
                continue
            span.attributes.update(run_snapshot.observability_attributes())
            await self.observability_store.save_span(span)


def _empty_usage(cost_known: bool) -> dict[str, Any]:
    return {
        "steps": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "cost_usd": "0" if cost_known else None,
        "cost_known": cost_known,
    }


def _run_entry(data: dict[str, Any], run_id: str) -> dict[str, Any]:
    runs = data.get("runs")
    if not isinstance(runs, dict) or not isinstance(runs.get(run_id), dict):
        raise RuntimeError(f"budget ledger has no run entry for {run_id}")
    return runs[run_id]


def _usage(
    data: dict[str, Any], run_id: str, *, include_reservations: bool = False
) -> RunBudgetUsage:
    run = _run_entry(data, run_id)
    raw = run["usage"]
    reserved_input = 0
    reserved_cost = Decimal(0)
    if include_reservations:
        reserved_input = sum(
            int(operation.get("reserved_input_tokens") or 0)
            for operation in data.get("operations", {}).values()
            if operation.get("run_id") == run_id and operation.get("status") == "reserved"
        )
        reserved_cost = sum(
            (
                Decimal(str(operation.get("reserved_cost_usd") or "0"))
                for operation in data.get("operations", {}).values()
                if operation.get("run_id") == run_id and operation.get("status") == "reserved"
            ),
            Decimal(0),
        )
    base_cost = _optional_decimal(raw.get("cost_usd"))
    return RunBudgetUsage(
        steps=int(raw.get("steps") or 0),
        model_calls=int(raw.get("model_calls") or 0),
        tool_calls=int(raw.get("tool_calls") or 0),
        input_tokens=int(raw.get("input_tokens") or 0) + reserved_input,
        output_tokens=int(raw.get("output_tokens") or 0),
        cached_input_tokens=int(raw.get("cached_input_tokens") or 0),
        reasoning_tokens=int(raw.get("reasoning_tokens") or 0),
        cost_usd=(base_cost or Decimal(0)) + reserved_cost if bool(raw.get("cost_known")) else None,
        cost_known=bool(raw.get("cost_known")),
        elapsed_seconds=_elapsed(run["created_at"]),
    )


def _aggregate_usage(data: dict[str, Any], *, include_reservations: bool = False) -> RunBudgetUsage:
    usages = [
        _usage(data, str(run_id), include_reservations=include_reservations)
        for run_id in data.get("runs", {})
    ]
    cost_known = bool(usages) and all(item.cost_known for item in usages)
    cost = sum((item.cost_usd or Decimal(0) for item in usages), Decimal(0))
    owner = _run_entry(data, str(data["owner_run_id"]))
    return RunBudgetUsage(
        steps=sum(item.steps for item in usages),
        model_calls=sum(item.model_calls for item in usages),
        tool_calls=sum(item.tool_calls for item in usages),
        input_tokens=sum(item.input_tokens for item in usages),
        output_tokens=sum(item.output_tokens for item in usages),
        cached_input_tokens=sum(item.cached_input_tokens for item in usages),
        reasoning_tokens=sum(item.reasoning_tokens for item in usages),
        cost_usd=cost if cost_known else None,
        cost_known=cost_known,
        elapsed_seconds=_elapsed(owner["created_at"]),
    )


def _usage_dimension(usage: RunBudgetUsage, dimension: str) -> int | float | Decimal:
    if dimension == "total_tokens":
        return usage.total_tokens
    value = getattr(usage, dimension)
    if dimension == "cost_usd":
        return value or Decimal(0)
    return value


def _pricing_from_entry(run: dict[str, Any]) -> ModelPricing | None:
    raw = run.get("pricing")
    return ModelPricing.from_dict(raw) if isinstance(raw, dict) else None


def _elapsed(created_at: str) -> float:
    try:
        created = datetime.fromisoformat(created_at)
        current = datetime.now(created.tzinfo)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (current - created).total_seconds())


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal value: {value}") from exc


def _required_decimal(value: Any, field_name: str) -> Decimal:
    result = _optional_decimal(value)
    if result is None:
        raise ValueError(f"{field_name} is required")
    return result


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")
