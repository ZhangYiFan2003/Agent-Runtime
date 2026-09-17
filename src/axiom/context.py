from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from axiom.config import AxiomConfig
from axiom.memory.summarizer import (
    ConversationSummarizer,
    DeterministicConversationSummarizer,
    SummaryPolicy,
    segment_runtime_messages,
)
from axiom.types import Message

if TYPE_CHECKING:
    from axiom.runtime.steps import StepContext

DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW = 64_000
MINIMUM_TRUSTED_MODEL_CONTEXT_WINDOW = 8_192
DEFAULT_HIGH_WATERMARK_RATIO = 0.80
DEFAULT_TARGET_AFTER_COMPACTION_RATIO = 0.60
DEFAULT_RECENT_MESSAGE_RESERVE = 6
DEFAULT_MAX_TOOL_RESULT_CHARS = 2_000
CONTEXT_BUDGET_EXCEEDED = "CONTEXT_BUDGET_EXCEEDED"
_SUMMARY_STATE_KEY = "_live_context_summary"
_COMPACTION_COUNT_KEY = "_live_context_compaction_count"


class TokenEstimator(Protocol):
    def estimate_text(self, text: str) -> int: ...

    def estimate_messages(
        self,
        messages: list[Message],
        *,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class ApproximateTokenEstimator:
    """Deterministic local estimator; provider tokenizers can replace it later."""

    chars_per_token: int = 4
    message_overhead_tokens: int = 4

    def estimate_text(self, text: str) -> int:
        if not text:
            return 0
        divisor = max(1, self.chars_per_token)
        return max(1, (len(text) + divisor - 1) // divisor, len(text.splitlines()))

    def estimate_messages(
        self,
        messages: list[Message],
        *,
        system_prompt: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> int:
        total = self.estimate_text(system_prompt)
        for message in messages:
            total += self.message_overhead_tokens
            total += self.estimate_text(_content_text(message.content))
            if message.name:
                total += self.estimate_text(message.name)
            if message.tool_call_id:
                total += self.estimate_text(message.tool_call_id)
            if message.tool_calls:
                total += self.estimate_text(_json_text(message.tool_calls))
        if tools:
            total += self.estimate_text(_json_text(tools))
        return total


@dataclass(frozen=True, slots=True)
class ContextBudgetPolicy:
    model_context_window: int = DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW
    reserved_output_tokens: int = 8_192
    high_watermark_ratio: float = DEFAULT_HIGH_WATERMARK_RATIO
    target_after_compaction_ratio: float = DEFAULT_TARGET_AFTER_COMPACTION_RATIO
    recent_message_reserve: int = DEFAULT_RECENT_MESSAGE_RESERVE
    hard_input_limit: int | None = None
    max_tool_result_chars: int = DEFAULT_MAX_TOOL_RESULT_CHARS
    compaction_enabled: bool = True

    def normalized(self) -> ContextBudgetPolicy:
        window = int(self.model_context_window or DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW)
        if window <= 0:
            window = DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW
        reserve = int(self.reserved_output_tokens)
        if reserve < 0 or reserve >= window:
            raise ValueError("reserved_output_tokens must be >= 0 and below model_context_window")
        high = float(self.high_watermark_ratio)
        target = float(self.target_after_compaction_ratio)
        if not 0 < high <= 1:
            raise ValueError("high_watermark_ratio must be in (0, 1]")
        if not 0 < target < high:
            raise ValueError("target_after_compaction_ratio must be in (0, high_watermark_ratio)")
        recent = int(self.recent_message_reserve)
        if recent < 0:
            raise ValueError("recent_message_reserve must be >= 0")
        usable = window - reserve
        hard = usable if self.hard_input_limit is None else int(self.hard_input_limit)
        if hard <= 0 or hard > usable:
            raise ValueError("hard_input_limit must be in (0, usable_input]")
        tool_chars = int(self.max_tool_result_chars)
        if tool_chars < 200:
            raise ValueError("max_tool_result_chars must be at least 200")
        return replace(
            self,
            model_context_window=window,
            reserved_output_tokens=reserve,
            high_watermark_ratio=high,
            target_after_compaction_ratio=target,
            recent_message_reserve=recent,
            hard_input_limit=hard,
            max_tool_result_chars=tool_chars,
        )

    @property
    def usable_input(self) -> int:
        return self.model_context_window - self.reserved_output_tokens

    @property
    def high_watermark_tokens(self) -> int:
        return int(self.usable_input * self.high_watermark_ratio)

    @property
    def target_after_compaction_tokens(self) -> int:
        return int(self.usable_input * self.target_after_compaction_ratio)


@dataclass(slots=True)
class RuntimeContextSummary:
    objective: str = ""
    constraints: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    completed_work: list[str] = field(default_factory=list)
    open_work: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    important_evidence: list[str] = field(default_factory=list)
    known_failures: list[str] = field(default_factory=list)
    narrative: str = ""
    covered_message_count: int = 0
    source_fingerprint: str = ""

    def to_text(self) -> str:
        rows = ["[live runtime context summary]"]
        rows.append(f"objective: {self.objective or 'not recorded'}")
        for key in (
            "constraints",
            "decisions",
            "completed_work",
            "open_work",
            "modified_files",
            "important_evidence",
            "known_failures",
        ):
            values = getattr(self, key)
            rows.append(f"{key}:")
            rows.extend(f"- {value}" for value in values) if values else rows.append(
                "- none recorded"
            )
        if self.narrative:
            rows.extend(("map_reduce_summary:", self.narrative))
        rows.append("[end live runtime context summary]")
        return "\n".join(rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "completed_work": list(self.completed_work),
            "open_work": list(self.open_work),
            "modified_files": list(self.modified_files),
            "important_evidence": list(self.important_evidence),
            "known_failures": list(self.known_failures),
            "narrative": self.narrative,
            "covered_message_count": self.covered_message_count,
            "source_fingerprint": self.source_fingerprint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeContextSummary:
        return cls(
            objective=str(data.get("objective") or ""),
            constraints=_strings(data.get("constraints")),
            decisions=_strings(data.get("decisions")),
            completed_work=_strings(data.get("completed_work")),
            open_work=_strings(data.get("open_work")),
            modified_files=_strings(data.get("modified_files")),
            important_evidence=_strings(data.get("important_evidence")),
            known_failures=_strings(data.get("known_failures")),
            narrative=str(data.get("narrative") or ""),
            covered_message_count=max(0, int(data.get("covered_message_count") or 0)),
            source_fingerprint=str(data.get("source_fingerprint") or ""),
        )


@dataclass(frozen=True, slots=True)
class ContextBuildResult:
    """Ephemeral desired context selected before token-window enforcement."""

    messages: tuple[Message, ...]
    system_prompt: str
    tools: tuple[dict[str, Any], ...]
    objective: str
    previous_summary: RuntimeContextSummary | None
    required_message_indexes: frozenset[int]
    source_metadata: tuple[tuple[str, str, str], ...]
    run_id: str | None = None
    step_index: int | None = None
    strategy: str | None = None


class ContextBuilder:
    """Select and assemble desired model context without applying token pressure."""

    def build(
        self,
        messages: list[Message],
        *,
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
        objective: str = "",
        previous_summary: RuntimeContextSummary | None = None,
        step_context: StepContext | None = None,
    ) -> ContextBuildResult:
        raw = tuple(_copy_message(message) for message in messages)
        previous = self._valid_previous_summary(list(raw), previous_summary)
        required = self._required_message_indexes(list(raw), objective)
        sources: list[tuple[str, str, str]] = [("system", "system_prompt", "required")]
        if tools:
            sources.append(("tools", "tool_definitions", "required"))
        if previous is not None:
            sources.append(("summary", "live_runtime_summary", "high"))
        sources.extend(
            (
                "message",
                str(index),
                "required" if index in required else "normal",
            )
            for index in range(len(raw))
        )
        return ContextBuildResult(
            messages=raw,
            system_prompt=system_prompt,
            tools=tuple(tools or ()),
            objective=objective,
            previous_summary=previous,
            required_message_indexes=frozenset(required),
            source_metadata=tuple(sources),
            run_id=step_context.run_id if step_context is not None else None,
            step_index=step_context.step_index if step_context is not None else None,
            strategy=step_context.strategy if step_context is not None else None,
        )

    def _valid_previous_summary(
        self,
        messages: list[Message],
        previous: RuntimeContextSummary | None,
    ) -> RuntimeContextSummary | None:
        if previous is None or previous.covered_message_count <= 0:
            return None
        if previous.covered_message_count > len(messages):
            return None
        fingerprint = _message_fingerprint(messages[: previous.covered_message_count])
        return previous if fingerprint == previous.source_fingerprint else None

    def _required_message_indexes(
        self,
        messages: list[Message],
        objective: str,
    ) -> set[int]:
        required: set[int] = set()
        objective_index = _latest_user_index(messages)
        if objective_index is not None:
            required.add(objective_index)
        if objective:
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if message.role == "user" and _content_text(message.content) == objective:
                    required.add(index)
                    break
        for unit in _message_units(messages):
            if unit.pending_protocol:
                required.update(range(unit.start, unit.end))
        return required


@dataclass(frozen=True, slots=True)
class ContextCompactionResult:
    messages: list[Message]
    compacted: bool
    trigger_reason: str | None
    estimated_tokens_before: int
    estimated_tokens_after: int
    compression_ratio: float | None
    compacted_message_count: int
    preserved_message_count: int
    evicted_messages: int
    tool_results_projected: int
    summary: RuntimeContextSummary | None = None
    summary_reused: bool = False

    def observability_attributes(self, *, compaction_count: int) -> dict[str, object]:
        return {
            "context.estimated_tokens_before": self.estimated_tokens_before,
            "context.estimated_tokens_after": self.estimated_tokens_after,
            "context.compaction_triggered": self.compacted,
            "context.compaction_count": compaction_count,
            "context.compression_ratio": self.compression_ratio,
            "context.evicted_messages": self.evicted_messages,
            "context.preserved_messages": self.preserved_message_count,
            "context.tool_results_projected": self.tool_results_projected,
            "context.trigger_reason": self.trigger_reason,
        }


class ContextBudgetExceededError(RuntimeError):
    code = CONTEXT_BUDGET_EXCEEDED

    def __init__(self, estimated_tokens: int, hard_input_limit: int):
        self.estimated_tokens = estimated_tokens
        self.hard_input_limit = hard_input_limit
        super().__init__(
            f"{self.code}: estimated input {estimated_tokens} exceeds hard limit {hard_input_limit}"
        )


class ContextBudget:
    """Fit desired context into one model request's input window."""

    def __init__(
        self,
        policy: ContextBudgetPolicy,
        estimated_input_tokens: int = 0,
        *,
        estimator: TokenEstimator | None = None,
        summarizer: ConversationSummarizer | None = None,
        summary_policy: SummaryPolicy | None = None,
    ) -> None:
        self.policy = policy.normalized()
        self.estimator = estimator or ApproximateTokenEstimator()
        self.summarizer = summarizer or DeterministicConversationSummarizer()
        self.summary_policy = (summary_policy or SummaryPolicy()).normalized()
        self.estimated_input_tokens = max(0, int(estimated_input_tokens))

    @property
    def above_high_watermark(self) -> bool:
        return self.estimated_input_tokens >= self.policy.high_watermark_tokens

    @property
    def exceeds_hard_limit(self) -> bool:
        return self.estimated_input_tokens > int(self.policy.hard_input_limit or 0)

    async def fit(
        self,
        desired: ContextBuildResult,
    ) -> ContextCompactionResult:
        raw = [_copy_message(message) for message in desired.messages]
        previous = desired.previous_summary
        candidate = self._with_previous_summary(raw, previous)
        before = self.estimator.estimate_messages(
            candidate,
            system_prompt=desired.system_prompt,
            tools=list(desired.tools),
        )
        candidate, projected = self._project_oversized_historical_tools(candidate)
        projected_tokens = self.estimator.estimate_messages(
            candidate,
            system_prompt=desired.system_prompt,
            tools=list(desired.tools),
        )
        trigger: str | None = None
        if projected:
            trigger = "oversized_tool_result"
        if projected_tokens >= self.policy.high_watermark_tokens:
            trigger = "high_watermark"

        if trigger != "high_watermark" or not self.policy.compaction_enabled:
            self._enforce_hard_limit(projected_tokens)
            return ContextCompactionResult(
                messages=candidate,
                compacted=False,
                trigger_reason=trigger,
                estimated_tokens_before=before,
                estimated_tokens_after=projected_tokens,
                compression_ratio=_ratio(before, projected_tokens),
                compacted_message_count=0,
                preserved_message_count=len(candidate),
                evicted_messages=0,
                tool_results_projected=projected,
                summary=previous,
                summary_reused=previous is not None,
            )

        covered = previous.covered_message_count if previous else 0
        compact_end = self._eligible_prefix_end(
            raw,
            covered,
            desired.required_message_indexes,
        )
        if compact_end <= covered:
            self._enforce_hard_limit(projected_tokens)
            return ContextCompactionResult(
                messages=candidate,
                compacted=False,
                trigger_reason="pinned_context",
                estimated_tokens_before=before,
                estimated_tokens_after=projected_tokens,
                compression_ratio=_ratio(before, projected_tokens),
                compacted_message_count=0,
                preserved_message_count=len(candidate),
                evicted_messages=0,
                tool_results_projected=projected,
                summary=previous,
                summary_reused=previous is not None,
            )

        eligible = raw[covered:compact_end]
        summary = await self._summarize(
            eligible,
            previous=previous,
            objective=desired.objective or _latest_user_text(raw),
            covered_message_count=compact_end,
            all_messages=raw,
        )
        compacted_messages = [
            Message(role="system", content=summary.to_text()),
            *[_copy_message(message) for message in raw[compact_end:]],
        ]
        compacted_messages, newly_projected = self._project_oversized_historical_tools(
            compacted_messages
        )
        projected += newly_projected
        after = self.estimator.estimate_messages(
            compacted_messages,
            system_prompt=desired.system_prompt,
            tools=list(desired.tools),
        )
        self._enforce_hard_limit(after)
        return ContextCompactionResult(
            messages=compacted_messages,
            compacted=True,
            trigger_reason="high_watermark",
            estimated_tokens_before=before,
            estimated_tokens_after=after,
            compression_ratio=_ratio(before, after),
            compacted_message_count=len(eligible),
            preserved_message_count=len(raw) - len(eligible),
            evicted_messages=len(eligible),
            tool_results_projected=projected,
            summary=summary,
            summary_reused=previous is not None,
        )

    def _with_previous_summary(
        self,
        messages: list[Message],
        previous: RuntimeContextSummary | None,
    ) -> list[Message]:
        if previous is None:
            return messages
        return [
            Message(role="system", content=previous.to_text()),
            *messages[previous.covered_message_count :],
        ]

    def _eligible_prefix_end(
        self,
        messages: list[Message],
        covered: int,
        required_message_indexes: frozenset[int],
    ) -> int:
        units = _message_units(messages)
        recent_start = max(0, len(messages) - self.policy.recent_message_reserve)
        objective_index = _latest_user_index(messages)
        eligible_end = covered
        for unit in units:
            if unit.end <= covered:
                continue
            pinned = (
                unit.end > recent_start
                or (objective_index is not None and unit.start <= objective_index < unit.end)
                or unit.pending_protocol
                or any(
                    index in required_message_indexes for index in range(unit.start, unit.end)
                )
            )
            if pinned:
                break
            if unit.start != eligible_end:
                break
            eligible_end = unit.end
        return eligible_end

    async def _summarize(
        self,
        messages: list[Message],
        *,
        previous: RuntimeContextSummary | None,
        objective: str,
        covered_message_count: int,
        all_messages: list[Message],
    ) -> RuntimeContextSummary:
        previous_text = previous.to_text() if previous else None
        try:
            segments = segment_runtime_messages(
                messages,
                max_estimated_tokens=self.summary_policy.map_chunk_estimated_tokens,
            )
            partials = [
                await self.summarizer.summarize_map(
                    segment,
                    previous_summary=previous_text,
                )
                for segment in segments
            ]
            narrative = await self.summarizer.summarize_reduce(
                partials,
                previous_summary=previous_text,
            )
        except Exception:  # derived projection; deterministic fallback must remain available
            fallback = DeterministicConversationSummarizer()
            segments = segment_runtime_messages(
                messages,
                max_estimated_tokens=self.summary_policy.map_chunk_estimated_tokens,
            )
            partials = [
                await fallback.summarize_map(segment, previous_summary=previous_text)
                for segment in segments
            ]
            narrative = await fallback.summarize_reduce(
                partials,
                previous_summary=previous_text,
            )
        summary = _structured_summary(
            messages,
            objective=objective,
            previous=previous,
            narrative=_clip(narrative, self.summary_policy.max_summary_chars),
        )
        summary.covered_message_count = covered_message_count
        summary.source_fingerprint = _message_fingerprint(all_messages[:covered_message_count])
        return summary

    def _project_oversized_historical_tools(
        self,
        messages: list[Message],
    ) -> tuple[list[Message], int]:
        recent_start = max(0, len(messages) - self.policy.recent_message_reserve)
        tool_names = _tool_names(messages)
        projected = 0
        result: list[Message] = []
        for index, message in enumerate(messages):
            copied = _copy_message(message)
            raw = _content_text(copied.content)
            if (
                copied.role == "tool"
                and index < recent_start
                and len(raw) > self.policy.max_tool_result_chars
            ):
                copied.content = _tool_projection(
                    raw,
                    tool_name=tool_names.get(copied.tool_call_id or "", copied.name or "unknown"),
                    is_error=_tool_is_error(copied, raw),
                    max_chars=self.policy.max_tool_result_chars,
                )
                projected += 1
            result.append(copied)
        return result, projected

    def _enforce_hard_limit(self, estimated_tokens: int) -> None:
        hard = int(self.policy.hard_input_limit or self.policy.usable_input)
        if estimated_tokens > hard:
            raise ContextBudgetExceededError(estimated_tokens, hard)


class ContextManager:
    """Compatibility facade composing semantic construction with token fitting."""

    def __init__(
        self,
        policy: ContextBudgetPolicy,
        *,
        estimator: TokenEstimator | None = None,
        summarizer: ConversationSummarizer | None = None,
        summary_policy: SummaryPolicy | None = None,
        builder: ContextBuilder | None = None,
    ) -> None:
        self.builder = builder or ContextBuilder()
        self.budget = ContextBudget(
            policy,
            estimator=estimator,
            summarizer=summarizer,
            summary_policy=summary_policy,
        )
        # Compatibility attributes for callers that previously inspected the manager.
        self.policy = self.budget.policy
        self.estimator = self.budget.estimator
        self.summarizer = self.budget.summarizer
        self.summary_policy = self.budget.summary_policy

    def build(
        self,
        messages: list[Message],
        *,
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
        objective: str = "",
        previous_summary: RuntimeContextSummary | None = None,
        step_context: StepContext | None = None,
    ) -> ContextBuildResult:
        return self.builder.build(
            messages,
            system_prompt=system_prompt,
            tools=tools,
            objective=objective,
            previous_summary=previous_summary,
            step_context=step_context,
        )

    async def fit(self, desired: ContextBuildResult) -> ContextCompactionResult:
        return await self.budget.fit(desired)

    async def prepare(
        self,
        messages: list[Message],
        *,
        system_prompt: str,
        tools: list[dict[str, Any]] | None = None,
        objective: str = "",
        previous_summary: RuntimeContextSummary | None = None,
        step_context: StepContext | None = None,
    ) -> ContextCompactionResult:
        desired = self.build(
            messages,
            system_prompt=system_prompt,
            tools=tools,
            objective=objective,
            previous_summary=previous_summary,
            step_context=step_context,
        )
        return await self.fit(desired)


def context_policy_from_config(config: AxiomConfig, llm_client: Any) -> ContextBudgetPolicy:
    context = config.context
    model_window = context.model_context_window
    if model_window is None:
        model_window = getattr(llm_client, "max_context_window", None)
        if not isinstance(model_window, int) or model_window < MINIMUM_TRUSTED_MODEL_CONTEXT_WINDOW:
            model_window = DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW
    reserve = context.reserved_output_tokens
    if reserve is None:
        reserve = min(
            max(0, int(config.llm.max_tokens)),
            max(1, model_window // 4),
        )
    return ContextBudgetPolicy(
        model_context_window=model_window,
        reserved_output_tokens=reserve,
        high_watermark_ratio=context.high_watermark_ratio,
        target_after_compaction_ratio=context.target_after_compaction_ratio,
        recent_message_reserve=context.recent_message_reserve,
        hard_input_limit=context.hard_input_limit,
        max_tool_result_chars=context.max_tool_result_chars,
        compaction_enabled=config.features.context_compression,
    ).normalized()


def summary_from_strategy_state(state: dict[str, Any]) -> RuntimeContextSummary | None:
    raw = state.get(_SUMMARY_STATE_KEY)
    return RuntimeContextSummary.from_dict(raw) if isinstance(raw, dict) else None


def apply_compaction_to_strategy_state(
    state: dict[str, Any], result: ContextCompactionResult
) -> int:
    if result.summary is not None:
        state[_SUMMARY_STATE_KEY] = result.summary.to_dict()
    count = max(0, int(state.get(_COMPACTION_COUNT_KEY) or 0))
    if result.compacted:
        count += 1
        state[_COMPACTION_COUNT_KEY] = count
    return count


def compaction_count_from_strategy_state(state: dict[str, Any]) -> int:
    return max(0, int(state.get(_COMPACTION_COUNT_KEY) or 0))


@dataclass(frozen=True, slots=True)
class _MessageUnit:
    start: int
    end: int
    pending_protocol: bool = False


def _message_units(messages: list[Message]) -> list[_MessageUnit]:
    units: list[_MessageUnit] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "assistant" and message.tool_calls:
            required = {str(call.get("id") or "") for call in message.tool_calls}
            found: set[str] = set()
            end = index + 1
            while end < len(messages) and messages[end].role == "tool":
                found.add(messages[end].tool_call_id or "")
                end += 1
            units.append(
                _MessageUnit(
                    start=index,
                    end=end,
                    pending_protocol=bool(required - found),
                )
            )
            index = end
            continue
        units.append(
            _MessageUnit(
                start=index,
                end=index + 1,
                pending_protocol=message.role == "tool",
            )
        )
        index += 1
    return units


def _structured_summary(
    messages: list[Message],
    *,
    objective: str,
    previous: RuntimeContextSummary | None,
    narrative: str,
) -> RuntimeContextSummary:
    summary = (
        RuntimeContextSummary.from_dict(previous.to_dict()) if previous else RuntimeContextSummary()
    )
    summary.objective = _clip(objective or summary.objective, 600)
    summary.narrative = narrative
    for message in messages:
        text = _content_text(message.content).strip()
        if not text:
            continue
        snippets = [part.strip() for part in re.split(r"[\r\n]+", text) if part.strip()]
        lower = text.lower()
        if message.role == "user":
            for snippet in snippets:
                marker = snippet.lower()
                if any(
                    word in marker
                    for word in ("must", "do not", "don't", "never", "constraint", "required")
                ):
                    _append_unique(summary.constraints, snippet)
                if any(
                    word in marker for word in ("todo", "pending", "open work", "next", "remaining")
                ):
                    _append_unique(summary.open_work, snippet)
        elif message.role == "assistant":
            for snippet in snippets:
                marker = snippet.lower()
                if any(word in marker for word in ("decided", "decision", "chosen", "will use")):
                    _append_unique(summary.decisions, snippet)
                if any(
                    word in marker
                    for word in ("completed", "implemented", "fixed", "created", "updated")
                ):
                    _append_unique(summary.completed_work, snippet)
                if any(word in marker for word in ("todo", "pending", "remaining", "next")):
                    _append_unique(summary.open_work, snippet)
        elif message.role == "tool":
            target = (
                summary.known_failures
                if _tool_is_error(message, text)
                else summary.important_evidence
            )
            _append_unique(target, text)
        for path in re.findall(r"(?:[A-Za-z]:[\\/])?[\w.-]+(?:[\\/][\w.-]+)+", text):
            _append_unique(summary.modified_files, path)
        if "error" in lower or "failed" in lower or "exception" in lower:
            _append_unique(summary.known_failures, text)
    return summary


def _append_unique(values: list[str], value: str, *, limit: int = 8) -> None:
    clipped = _clip(" ".join(value.split()), 280)
    if clipped and clipped not in values:
        values.append(clipped)
        del values[:-limit]


def _tool_projection(raw: str, *, tool_name: str, is_error: bool, max_chars: int) -> str:
    header = (
        "[bounded historical tool result]\n"
        f"tool: {tool_name}\n"
        f"status: {'error' if is_error else 'success'}\n"
        f"original_chars: {len(raw)}\n"
        "truncated: true\n"
    )
    room = max(40, max_chars - len(header) - 80)
    head = max(20, room // 2)
    tail = max(20, room - head)
    return (
        f"{header}content_head:\n{raw[:head]}\n"
        f"... [content truncated] ...\ncontent_tail:\n{raw[-tail:]}"
    )


def _tool_names(messages: list[Message]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls:
            call_id = str(call.get("id") or "")
            function = call.get("function")
            if call_id and isinstance(function, dict):
                names[call_id] = str(function.get("name") or "unknown")
    return names


def _tool_is_error(message: Message, raw: str) -> bool:
    if message.name and "error" in message.name.lower():
        return True
    lower = raw.lower()
    return any(
        marker in lower for marker in ('"is_error":true', "traceback", "exception", "error:")
    )


def _latest_user_index(messages: list[Message]) -> int | None:
    return next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == "user"),
        None,
    )


def _latest_user_text(messages: list[Message]) -> str:
    index = _latest_user_index(messages)
    return _content_text(messages[index].content) if index is not None else ""


def _message_fingerprint(messages: list[Message]) -> str:
    rows = [
        {
            "role": message.role,
            "content": message.content,
            "tool_call_id": message.tool_call_id,
            "tool_calls": message.tool_calls,
        }
        for message in messages
    ]
    return hashlib.sha256(_json_text(rows).encode("utf-8")).hexdigest()


def _copy_message(message: Message) -> Message:
    content = json.loads(json.dumps(message.content, ensure_ascii=False))
    calls = json.loads(json.dumps(message.tool_calls, ensure_ascii=False))
    return Message(
        role=message.role,
        content=content,
        name=message.name,
        tool_call_id=message.tool_call_id,
        tool_calls=calls,
    )


def _content_text(content: str | list[dict[str, Any]]) -> str:
    return content if isinstance(content, str) else _json_text(content)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _clip(text: str, max_chars: int) -> str:
    compact = str(text or "").strip()
    return compact if len(compact) <= max_chars else compact[: max_chars - 3].rstrip() + "..."


def _ratio(before: int, after: int) -> float | None:
    return round(after / before, 4) if before > 0 else None
