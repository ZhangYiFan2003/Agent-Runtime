from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from axiom.memory.context import estimate_tokens
from axiom.types import Message

DEFAULT_SUMMARY_THRESHOLD_MESSAGES = 6
DEFAULT_MAP_CHUNK_TOKENS = 400
DEFAULT_REDUCE_INPUT_TOKENS = 1_200
DEFAULT_RECENT_MESSAGE_RESERVE = 2
DEFAULT_MAX_SUMMARY_CHARS = 2_000
DEFAULT_MAX_ATTEMPTS = 1
HARD_MAX_SUMMARY_CHARS = 6_000


class ConversationSummarizer(Protocol):
    async def summarize_map(
        self,
        messages: Sequence[Message],
        *,
        previous_summary: str | None = None,
    ) -> str: ...

    async def summarize_reduce(
        self,
        partial_summaries: Sequence[str],
        *,
        previous_summary: str | None = None,
    ) -> str: ...


@dataclass(slots=True)
class SummaryPolicy:
    enabled: bool = True
    threshold_messages: int = DEFAULT_SUMMARY_THRESHOLD_MESSAGES
    map_chunk_estimated_tokens: int = DEFAULT_MAP_CHUNK_TOKENS
    reduce_input_estimated_tokens: int = DEFAULT_REDUCE_INPUT_TOKENS
    minimum_unsummarized_messages: int = 4
    recent_message_reserve: int = DEFAULT_RECENT_MESSAGE_RESERVE
    max_summary_chars: int = DEFAULT_MAX_SUMMARY_CHARS
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def normalized(self) -> SummaryPolicy:
        return SummaryPolicy(
            enabled=self.enabled,
            threshold_messages=min(max(self.threshold_messages, 2), 200),
            map_chunk_estimated_tokens=min(max(self.map_chunk_estimated_tokens, 50), 4_000),
            reduce_input_estimated_tokens=min(max(self.reduce_input_estimated_tokens, 100), 8_000),
            minimum_unsummarized_messages=min(max(self.minimum_unsummarized_messages, 2), 100),
            recent_message_reserve=min(max(self.recent_message_reserve, 0), 20),
            max_summary_chars=min(max(self.max_summary_chars, 100), HARD_MAX_SUMMARY_CHARS),
            max_attempts=min(max(self.max_attempts, 1), 5),
        )


@dataclass(slots=True)
class ConversationSegment:
    messages: list[Message]
    source_event_start_id: int
    source_event_end_id: int


@dataclass(slots=True)
class SummaryRunResult:
    created: bool
    summary_id: str | None = None
    source_event_start_id: int | None = None
    source_event_end_id: int | None = None
    version: int = 0
    map_count: int = 0
    message_count: int = 0
    estimated_tokens_before: int = 0
    estimated_tokens_after: int = 0
    compression_ratio: float | None = None
    error: str | None = None


class DeterministicConversationSummarizer:
    version = "deterministic-v1"

    async def summarize_map(
        self,
        messages: Sequence[Message],
        *,
        previous_summary: str | None = None,
    ) -> str:
        lines = ["Map summary:"]
        if previous_summary:
            lines.append(f"Previous: {_clip(previous_summary, 160)}")
        user_goals = [
            _clip(str(message.content), 120) for message in messages if message.role == "user"
        ]
        assistant_actions = [
            _clip(str(message.content), 120) for message in messages if message.role == "assistant"
        ]
        if user_goals:
            lines.append("User goals: " + " | ".join(user_goals))
        if assistant_actions:
            lines.append("Completed actions: " + " | ".join(assistant_actions))
        lines.append("Unresolved issues: none recorded by deterministic summarizer")
        return "\n".join(lines)

    async def summarize_reduce(
        self,
        partial_summaries: Sequence[str],
        *,
        previous_summary: str | None = None,
    ) -> str:
        lines = ["Thread summary:"]
        if previous_summary:
            lines.append("Previous active summary:")
            lines.append(_clip(previous_summary, 400))
        lines.append("New summary fragments:")
        for index, summary in enumerate(partial_summaries, start=1):
            lines.append(f"{index}. {_clip(summary, 500)}")
        return "\n".join(lines)


def segment_messages(
    records,
    *,
    max_estimated_tokens: int,
) -> list[ConversationSegment]:
    turns: list[tuple[list[Message], int, int, int]] = []
    pending_user: tuple[Message, int, int] | None = None
    for record in records:
        role = str(record.metadata.get("role") or "")
        event_id = record.source_event_start_id
        if event_id is None:
            continue
        if role == "user":
            pending_user = (Message(role="user", content=record.content), event_id, event_id)
        elif role == "assistant" and pending_user is not None:
            user_message, start_id, _end_id = pending_user
            assistant_message = Message(role="assistant", content=record.content)
            token_count = estimate_tokens(str(user_message.content)) + estimate_tokens(
                str(assistant_message.content)
            )
            turns.append(([user_message, assistant_message], start_id, event_id, token_count))
            pending_user = None

    segments: list[ConversationSegment] = []
    current: list[Message] = []
    current_start: int | None = None
    current_end: int | None = None
    current_tokens = 0
    for messages, start_id, end_id, turn_tokens in turns:
        if current and current_tokens + turn_tokens > max_estimated_tokens:
            segments.append(
                ConversationSegment(
                    messages=current,
                    source_event_start_id=int(current_start or start_id),
                    source_event_end_id=int(current_end or end_id),
                )
            )
            current = []
            current_start = None
            current_tokens = 0
        if current_start is None:
            current_start = start_id
        current.extend(messages)
        current_end = end_id
        current_tokens += turn_tokens
    if current:
        segments.append(
            ConversationSegment(
                messages=current,
                source_event_start_id=int(current_start or 0),
                source_event_end_id=int(current_end or 0),
            )
        )
    return segments


def segment_runtime_messages(
    messages: Sequence[Message],
    *,
    max_estimated_tokens: int,
) -> list[list[Message]]:
    """Segment an in-flight message sequence for the existing map stage.

    Persisted conversation summaries use event-backed ``segment_messages`` above.
    Live runtime context has no event IDs yet, but it must use the same bounded
    map/reduce shape and summarizer protocol.  Tool-call protocol groups are
    selected atomically by the runtime context manager before this function is
    called, so this helper only has to create deterministic token-bounded maps.
    """

    limit = max(1, int(max_estimated_tokens))
    segments: list[list[Message]] = []
    current: list[Message] = []
    current_tokens = 0
    for message in messages:
        message_tokens = estimate_tokens(str(message.content))
        if message.tool_calls:
            message_tokens += estimate_tokens(str(message.tool_calls))
        if current and current_tokens + message_tokens > limit:
            segments.append(current)
            current = []
            current_tokens = 0
        current.append(message)
        current_tokens += message_tokens
    if current:
        segments.append(current)
    return segments


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."
