from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.context import ContextManager, RuntimeContextSummary
from axiom.types import Message

CONTEXT_RETENTION_SCHEMA_VERSION = 1
RETENTION_CATEGORIES = (
    "objective",
    "constraints",
    "open_tasks",
    "decisions",
    "artifact_references",
    "critical_evidence",
    "pending_protocol_state",
)

ContextOutcomeProbe = Callable[[list[Message]], str | Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ContextRetentionCase:
    id: str
    messages: tuple[Message, ...]
    must_preserve: dict[str, tuple[str, ...]]
    objective: str = ""
    must_drop: tuple[str, ...] = ()
    previous_summary: RuntimeContextSummary | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextRetentionCase:
        case_id = str(data.get("id") or "").strip()
        if not case_id:
            raise ValueError("context retention case id is required")
        raw_messages = data.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ValueError(f'context retention case "{case_id}" messages are required')
        if any(not isinstance(item, dict) for item in raw_messages):
            raise ValueError("every context retention message must be an object")
        raw_preserve = data.get("must_preserve")
        if not isinstance(raw_preserve, dict) or not raw_preserve:
            raise ValueError(f'context retention case "{case_id}" must_preserve is required')
        unknown = set(raw_preserve) - set(RETENTION_CATEGORIES)
        if unknown:
            raise ValueError(f"unknown retention categories: {', '.join(sorted(unknown))}")
        must_preserve = {
            category: _strings(values, field=f"must_preserve.{category}")
            for category, values in raw_preserve.items()
        }
        if not any(must_preserve.values()):
            raise ValueError(f'context retention case "{case_id}" has no required items')
        return cls(
            id=case_id,
            messages=tuple(
                _message_from_dict(item) for item in raw_messages if isinstance(item, dict)
            ),
            must_preserve=must_preserve,
            objective=str(data.get("objective") or ""),
            must_drop=tuple(_strings(data.get("must_drop", []), field="must_drop")),
            previous_summary=(
                RuntimeContextSummary.from_dict(data["previous_summary"])
                if isinstance(data.get("previous_summary"), dict)
                else None
            ),
            metadata=_dict(data.get("metadata")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "objective": self.objective,
            "messages": [_message_to_dict(message) for message in self.messages],
            "must_preserve": {
                category: list(values) for category, values in self.must_preserve.items()
            },
            "must_drop": list(self.must_drop),
            "previous_summary": self.previous_summary.to_dict() if self.previous_summary else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextRetentionDataset:
    name: str
    version: str
    cases: tuple[ContextRetentionCase, ...]
    schema_version: int = CONTEXT_RETENTION_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextRetentionDataset:
        schema_version = int(data.get("schema_version") or CONTEXT_RETENTION_SCHEMA_VERSION)
        if schema_version != CONTEXT_RETENTION_SCHEMA_VERSION:
            raise ValueError(f"unsupported context retention schema version: {schema_version}")
        name = str(data.get("name") or "").strip()
        version = str(data.get("version") or "").strip()
        raw_cases = data.get("cases")
        if not name or not version:
            raise ValueError("context retention dataset name and version are required")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("context retention dataset cases are required")
        cases = tuple(
            ContextRetentionCase.from_dict(item) for item in raw_cases if isinstance(item, dict)
        )
        if len(cases) != len(raw_cases):
            raise ValueError("every context retention case must be an object")
        ids = [case.id for case in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("context retention case ids must be unique")
        return cls(name=name, version=version, cases=cases, schema_version=schema_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "version": self.version,
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True, slots=True)
class RetainedItemResult:
    category: str
    item: str
    retained: bool
    loss_stage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "item": self.item,
            "retained": self.retained,
            "loss_stage": self.loss_stage,
        }


@dataclass(frozen=True, slots=True)
class ContextRetentionCaseResult:
    case_id: str
    before_tokens: int
    after_tokens: int
    compression_ratio: float | None
    required_items: int
    retained_items: int
    required_state_retention_rate: float
    protocol_valid: bool
    items: tuple[RetainedItemResult, ...]
    retained_noise: tuple[str, ...] = ()
    full_context_outcome: str | None = None
    compressed_context_outcome: str | None = None
    outcome_matches: bool | None = None

    @property
    def passed(self) -> bool:
        return self.required_state_retention_rate == 1.0 and self.protocol_valid

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "compression_ratio": self.compression_ratio,
            "required_items": self.required_items,
            "retained_items": self.retained_items,
            "required_state_retention_rate": self.required_state_retention_rate,
            "protocol_valid": self.protocol_valid,
            "passed": self.passed,
            "lost_items": [item.to_dict() for item in self.items if not item.retained],
            "items": [item.to_dict() for item in self.items],
            "retained_noise": list(self.retained_noise),
            "full_context_outcome": self.full_context_outcome,
            "compressed_context_outcome": self.compressed_context_outcome,
            "outcome_matches": self.outcome_matches,
        }


@dataclass(frozen=True, slots=True)
class ContextRetentionSuiteResult:
    dataset: str
    dataset_version: str
    results: tuple[ContextRetentionCaseResult, ...]
    required_items: int
    retained_items: int
    required_state_retention_rate: float
    protocol_integrity_rate: float
    average_compression_ratio: float | None

    @classmethod
    def create(
        cls,
        dataset: ContextRetentionDataset,
        results: list[ContextRetentionCaseResult],
    ) -> ContextRetentionSuiteResult:
        required = sum(result.required_items for result in results)
        retained = sum(result.retained_items for result in results)
        ratios = [result.compression_ratio for result in results if result.compression_ratio]
        return cls(
            dataset=dataset.name,
            dataset_version=dataset.version,
            results=tuple(results),
            required_items=required,
            retained_items=retained,
            required_state_retention_rate=round(retained / required, 4) if required else 1.0,
            protocol_integrity_rate=(
                round(sum(result.protocol_valid for result in results) / len(results), 4)
                if results
                else 1.0
            ),
            average_compression_ratio=(
                round(sum(ratios) / len(ratios), 4) if ratios else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTEXT_RETENTION_SCHEMA_VERSION,
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "required_items": self.required_items,
            "retained_items": self.retained_items,
            "required_state_retention_rate": self.required_state_retention_rate,
            "protocol_integrity_rate": self.protocol_integrity_rate,
            "average_compression_ratio": self.average_compression_ratio,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True, slots=True)
class ContextRetentionComparison:
    hard_regressions: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.hard_regressions

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "PASS" if self.passed else "FAIL",
            "hard_regressions": list(self.hard_regressions),
            "warnings": list(self.warnings),
        }


class ContextRetentionEvaluator:
    def __init__(
        self,
        manager: ContextManager,
        *,
        system_prompt: str = "context retention evaluation",
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self.manager = manager
        self.system_prompt = system_prompt
        self.tools = list(tools or [])

    async def evaluate_case(
        self,
        case: ContextRetentionCase,
        *,
        outcome_probe: ContextOutcomeProbe | None = None,
    ) -> ContextRetentionCaseResult:
        projection = await self.manager.prepare(
            list(case.messages),
            system_prompt=self.system_prompt,
            tools=self.tools,
            objective=case.objective,
            previous_summary=case.previous_summary,
        )
        searchable = _searchable_text(projection.messages).casefold()
        items = tuple(
            RetainedItemResult(
                category=category,
                item=item,
                retained=item.casefold() in searchable,
                loss_stage=None if item.casefold() in searchable else "lost_after_compaction",
            )
            for category in RETENTION_CATEGORIES
            for item in case.must_preserve.get(category, ())
        )
        retained = sum(item.retained for item in items)
        full_outcome: str | None = None
        compressed_outcome: str | None = None
        outcome_matches: bool | None = None
        if outcome_probe is not None:
            full_outcome = await _probe(outcome_probe, list(case.messages))
            compressed_outcome = await _probe(outcome_probe, projection.messages)
            outcome_matches = full_outcome == compressed_outcome
        return ContextRetentionCaseResult(
            case_id=case.id,
            before_tokens=projection.estimated_tokens_before,
            after_tokens=projection.estimated_tokens_after,
            compression_ratio=projection.compression_ratio,
            required_items=len(items),
            retained_items=retained,
            required_state_retention_rate=(
                round(retained / len(items), 4) if items else 1.0
            ),
            protocol_valid=_protocol_valid(projection.messages),
            items=items,
            retained_noise=tuple(item for item in case.must_drop if item.casefold() in searchable),
            full_context_outcome=full_outcome,
            compressed_context_outcome=compressed_outcome,
            outcome_matches=outcome_matches,
        )

    async def evaluate_dataset(
        self,
        dataset: ContextRetentionDataset,
        *,
        outcome_probe: ContextOutcomeProbe | None = None,
    ) -> ContextRetentionSuiteResult:
        results = [
            await self.evaluate_case(case, outcome_probe=outcome_probe)
            for case in dataset.cases
        ]
        return ContextRetentionSuiteResult.create(dataset, results)


def compare_context_retention(
    baseline: ContextRetentionSuiteResult,
    candidate: ContextRetentionSuiteResult,
) -> ContextRetentionComparison:
    old = {result.case_id: result for result in baseline.results}
    new = {result.case_id: result for result in candidate.results}
    regressions: list[str] = []
    warnings: list[str] = []
    for case_id in sorted(old.keys() & new.keys()):
        before, after = old[case_id], new[case_id]
        if after.required_state_retention_rate < before.required_state_retention_rate:
            regressions.append(
                f"{case_id}: required-state retention decreased from "
                f"{before.required_state_retention_rate:.4f} to "
                f"{after.required_state_retention_rate:.4f}"
            )
        if before.protocol_valid and not after.protocol_valid:
            regressions.append(f"{case_id}: protocol integrity regressed")
        if (
            before.outcome_matches is True
            and after.outcome_matches is False
            and not any(item.startswith(f"{case_id}:") for item in regressions)
        ):
            regressions.append(f"{case_id}: compressed outcome regressed")
        if (
            before.compression_ratio is not None
            and after.compression_ratio is not None
            and after.compression_ratio > before.compression_ratio
        ):
            warnings.append(
                f"{case_id}: compression ratio worsened from "
                f"{before.compression_ratio:.4f} to {after.compression_ratio:.4f}"
            )
    for case_id in sorted(new.keys() - old.keys()):
        if not new[case_id].passed:
            regressions.append(f"{case_id}: new retention case failed")
    return ContextRetentionComparison(tuple(regressions), tuple(warnings))


def load_context_retention_dataset(path: str | Path) -> ContextRetentionDataset:
    source = Path(path).expanduser()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"context retention dataset not found: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"invalid context retention dataset JSON at line {exc.lineno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("context retention dataset must be a JSON object")
    return ContextRetentionDataset.from_dict(data)


async def _probe(probe: ContextOutcomeProbe, messages: list[Message]) -> str:
    value = probe(messages)
    return str(await value) if inspect.isawaitable(value) else str(value)


def _searchable_text(messages: list[Message]) -> str:
    rows: list[str] = []
    for message in messages:
        rows.append(
            message.content
            if isinstance(message.content, str)
            else json.dumps(message.content)
        )
        if message.name:
            rows.append(message.name)
        if message.tool_call_id:
            rows.append(message.tool_call_id)
        if message.tool_calls:
            rows.append(json.dumps(message.tool_calls, sort_keys=True))
    return "\n".join(rows)


def _protocol_valid(messages: list[Message]) -> bool:
    calls: dict[str, int] = {}
    results: dict[str, int] = {}
    for index, message in enumerate(messages):
        for call in message.tool_calls:
            call_id = str(call.get("id") or "")
            if not call_id or call_id in calls:
                return False
            calls[call_id] = index
        if message.role == "tool":
            call_id = message.tool_call_id or ""
            if not call_id or call_id in results:
                return False
            results[call_id] = index
    return all(call_id in calls and calls[call_id] < index for call_id, index in results.items())


def _message_from_dict(data: dict[str, Any]) -> Message:
    role = str(data.get("role") or "user")
    if role not in {"system", "user", "assistant", "tool"}:
        raise ValueError(f"invalid context retention message role: {role}")
    raw_calls = data.get("tool_calls") or []
    content = data.get("content", "")
    repeat = int(data.get("repeat") or 1)
    if repeat < 1 or repeat > 100:
        raise ValueError("context retention message repeat must be in [1, 100]")
    if isinstance(content, str) and repeat > 1:
        content = " ".join(content for _ in range(repeat))
    return Message(
        role=role,  # type: ignore[arg-type]
        content=content,
        name=str(data["name"]) if data.get("name") is not None else None,
        tool_call_id=(
            str(data["tool_call_id"]) if data.get("tool_call_id") is not None else None
        ),
        tool_calls=[item for item in raw_calls if isinstance(item, dict)],
    )


def _message_to_dict(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
    }


def _strings(value: Any, *, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(value)
    else:
        raise ValueError(f"{field} must be a string or list of strings")
    if any(not item for item in values):
        raise ValueError(f"{field} items must not be empty")
    return values


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
