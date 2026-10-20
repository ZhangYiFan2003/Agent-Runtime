from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from axiom.memory.context import (
    MemoryContextBuilder,
    completed_conversation_records,
    is_identified_conversation_summary,
)
from axiom.memory.facts import (
    DeterministicFactExtractor,
    FactCandidate,
    FactExtractionRunResult,
    FactExtractor,
    normalize_fact_key,
    validate_fact_candidate,
)
from axiom.memory.models import MemoryContextResult, MemoryKind, MemoryRecord, MemoryScopeType
from axiom.memory.store import MemoryStore
from axiom.memory.summarizer import (
    ConversationSummarizer,
    DeterministicConversationSummarizer,
    SummaryPolicy,
    SummaryRunResult,
    segment_messages,
)
from axiom.types import Message

if TYPE_CHECKING:
    from axiom.runtime.events import RuntimeEvent

SECRET_PATTERN = re.compile(
    r"(authorization|bearer\s+[a-z0-9._-]+|sk-[a-z0-9]+|api[_-]?key|\.env)",
    re.IGNORECASE,
)
WINDOWS_PATH_PATTERN = re.compile(r"\b[A-Za-z]:\\[^\s]+")
BASE64_BLOB_PATTERN = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")
DEFAULT_TOOL_DIGEST_CHARS = 500


@dataclass(frozen=True, slots=True)
class _CompletedRuntimeTurn:
    user: RuntimeEvent
    assistant: RuntimeEvent
    completed_event_id: int


class MemoryService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        project_scope: str,
        user_scope: str = "local",
        summarizer: ConversationSummarizer | None = None,
        summary_policy: SummaryPolicy | None = None,
        fact_extractor: FactExtractor | None = None,
    ):
        self.store = MemoryStore(db_path)
        self.project_scope = project_scope
        self.user_scope = user_scope
        self.context_builder = MemoryContextBuilder(self.store)
        self.summarizer = summarizer or DeterministicConversationSummarizer()
        self.summary_policy = (summary_policy or SummaryPolicy()).normalized()
        self.fact_extractor = fact_extractor or DeterministicFactExtractor()

    def save_fact(
        self,
        content: str,
        *,
        key: str | None = None,
        category: str = "fact",
        confidence: float = 1.0,
        source: str = "explicit",
        scope_type: MemoryScopeType = MemoryScopeType.PROJECT,
        scope_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        target_scope = scope_id or self._default_scope(scope_type)
        if key:
            normalized_key = normalize_fact_key(category, key)
            candidate = validate_fact_candidate(
                FactCandidate(
                    key=normalized_key,
                    value=content,
                    category=category,
                    scope_type=scope_type,
                    confidence=confidence,
                    explicit=True,
                    source_event_start_id=-1,
                    source_event_end_id=-1,
                    evidence=str(metadata.get("evidence")) if metadata else None,
                )
            )
            if candidate is None:
                raise ValueError("memory fact was rejected by validation policy")
            return self.store.save_fact_value(
                scope_type=scope_type,
                scope_id=target_scope,
                key=candidate.key,
                category=candidate.category,
                content=candidate.value,
                confidence=candidate.confidence,
                metadata={
                    "source": source,
                    "explicit": True,
                    **dict(metadata or {}),
                },
            )
        record = self.store.save_record(
            kind=MemoryKind.FACT,
            scope_type=scope_type,
            scope_id=target_scope,
            content=content,
            metadata={
                "active": True,
                "category": category,
                "confidence": confidence,
                "source": source,
                **({"key": key} if key else {}),
                **dict(metadata or {}),
            },
        )
        return record

    def forget_fact(
        self,
        key_or_id: str,
        *,
        scope_type: MemoryScopeType = MemoryScopeType.PROJECT,
        scope_id: str | None = None,
        category: str | None = None,
    ) -> int:
        return self.store.retract_fact(
            scope_type=scope_type,
            scope_id=scope_id or self._default_scope(scope_type),
            key_or_id=key_or_id,
            category=category,
        )

    def fact_history(
        self,
        key_or_id: str,
        *,
        scope_type: MemoryScopeType = MemoryScopeType.PROJECT,
        scope_id: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        return self.store.fact_history(
            scope_type=scope_type,
            scope_id=scope_id or self._default_scope(scope_type),
            key_or_id=key_or_id,
            limit=limit,
        )

    def save_summary(
        self,
        thread_id: str,
        content: str,
        *,
        source_event_start_id: int | None = None,
        source_event_end_id: int | None = None,
        version: int = 1,
        replaces: str | None = None,
        active: bool = True,
        summary_stage: str = "reduce",
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        summary_metadata = {
            "active": active,
            "version": version,
            "replaces": replaces,
            "summary_stage": summary_stage,
            **dict(metadata or {}),
        }
        if active and summary_stage == "reduce":
            return self.store.save_active_summary(
                scope_id=thread_id,
                content=content,
                source_event_start_id=source_event_start_id,
                source_event_end_id=source_event_end_id,
                metadata=summary_metadata,
                replaces=replaces,
            )
        record = self.store.save_record(
            kind=MemoryKind.SUMMARY,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            content=content,
            source_event_start_id=source_event_start_id,
            source_event_end_id=source_event_end_id,
            metadata=summary_metadata,
        )
        return record

    def save_conversation(
        self,
        thread_id: str,
        *,
        role: str,
        content: str,
        event_id: int,
        turn_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        return self.store.save_record(
            kind=MemoryKind.CONVERSATION,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            content=content,
            source_event_start_id=event_id,
            source_event_end_id=event_id,
            metadata={
                "active": True,
                "role": role,
                "turn_id": turn_id,
                **dict(metadata or {}),
            },
            record_id=f"event_{event_id}",
        )

    def save_tool_result(
        self,
        thread_id: str,
        *,
        tool_name: str,
        success: bool,
        content: str,
        source_event_id: int,
        max_preview_chars: int = DEFAULT_TOOL_DIGEST_CHARS,
        metadata: dict[str, object] | None = None,
    ) -> MemoryRecord:
        raw = str(content or "")
        preview = _bounded_preview(_redact(raw), max_preview_chars)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self.store.save_record(
            kind=MemoryKind.TOOL_RESULT,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            content=preview,
            source_event_start_id=source_event_id,
            source_event_end_id=source_event_id,
            metadata={
                "active": True,
                "tool_name": tool_name,
                "success": success,
                "result_chars": len(raw),
                "sha256": digest,
                "truncated": len(raw) > max_preview_chars,
                **dict(metadata or {}),
            },
            record_id=f"tool_event_{source_event_id}",
        )

    def list_records(
        self,
        *,
        kind: MemoryKind | None = None,
        scope_type: MemoryScopeType | None = None,
        scope_id: str | None = None,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        return self.store.list_records(
            kind=kind,
            scope_type=scope_type,
            scope_id=scope_id,
            include_inactive=include_inactive,
            limit=limit,
        )

    def search_records(
        self,
        query: str,
        *,
        kinds: Iterable[MemoryKind] | None = None,
        scopes: Iterable[tuple[MemoryScopeType, str]] | None = None,
        limit: int = 20,
    ) -> list[MemoryRecord]:
        return self.store.search_records(query, kinds=kinds, scopes=scopes, limit=limit)

    def build_context(
        self,
        *,
        thread_id: str | None = None,
        query: str = "",
        max_context_chars: int = 8_000,
        max_estimated_tokens: int = 2_000,
        max_records: int = 40,
    ) -> MemoryContextResult:
        return self.context_builder.build(
            thread_id=thread_id,
            project_scope=self.project_scope,
            user_scope=self.user_scope,
            query=query,
            max_context_chars=max_context_chars,
            max_estimated_tokens=max_estimated_tokens,
            max_records=max_records,
        )

    async def summarize_thread(self, thread_id: str) -> SummaryRunResult:
        try:
            return await self._summarize_thread_unchecked(thread_id)
        except Exception as exc:  # noqa: BLE001 - summarization is derived state
            return SummaryRunResult(created=False, error=exc.__class__.__name__)

    async def extract_facts_from_thread(
        self,
        thread_id: str,
        events: Iterable[RuntimeEvent],
    ) -> FactExtractionRunResult:
        try:
            return await self._extract_facts_from_thread_unchecked(thread_id, events)
        except Exception as exc:  # noqa: BLE001 - extraction is derived state
            return FactExtractionRunResult(error=exc.__class__.__name__)

    async def _extract_facts_from_thread_unchecked(
        self,
        thread_id: str,
        events: Iterable[RuntimeEvent],
    ) -> FactExtractionRunResult:
        last_event_id = _int_or_none(self.store.get_meta(_fact_checkpoint_key(thread_id)))
        user_messages: list[Message] = []
        max_event_id = last_event_id
        for turn in _completed_runtime_turns(events):
            if turn.user.thread_id != thread_id:
                continue
            if last_event_id is not None and turn.completed_event_id <= last_event_id:
                continue
            max_event_id = (
                turn.completed_event_id
                if max_event_id is None
                else max(max_event_id, turn.completed_event_id)
            )
            user_messages.append(
                Message(
                    role="user", content=turn.user.payload["text"], name=f"event:{turn.user.id}"
                )
            )
        if not user_messages:
            if max_event_id is not None:
                self.store.set_meta(_fact_checkpoint_key(thread_id), str(max_event_id))
            return FactExtractionRunResult(processed_event_end_id=max_event_id)
        summary = self.store.active_summary(thread_id=thread_id)
        if summary is not None and not is_identified_conversation_summary(
            summary,
            self.store.list_records(
                kind=MemoryKind.CONVERSATION,
                scope_type=MemoryScopeType.THREAD,
                scope_id=thread_id,
                limit=1000,
            ),
        ):
            summary = None
        candidates = list(
            await self.fact_extractor.extract(
                user_messages,
                summary=summary.content if summary is not None else None,
            )
        )
        result = FactExtractionRunResult(
            processed_event_end_id=max_event_id,
            candidate_count=len(candidates),
        )
        for candidate in candidates:
            validated = validate_fact_candidate(candidate)
            if validated is None:
                result.rejected_count += 1
                continue
            scope_id = self._default_scope(validated.scope_type)
            if validated.scope_type == MemoryScopeType.THREAD:
                scope_id = thread_id
            if validated.action == "retract":
                result.retracted_count += self.store.retract_fact(
                    scope_type=validated.scope_type,
                    scope_id=scope_id,
                    key_or_id=validated.key,
                    category=validated.category,
                    source_event_start_id=validated.source_event_start_id,
                    source_event_end_id=validated.source_event_end_id,
                )
                result.accepted_count += 1
                continue
            self.store.save_fact_value(
                scope_type=validated.scope_type,
                scope_id=scope_id,
                key=validated.key,
                category=validated.category,
                content=validated.value,
                confidence=validated.confidence,
                source_event_start_id=validated.source_event_start_id,
                source_event_end_id=validated.source_event_end_id,
                metadata={
                    "source": "runtime_fact_extractor",
                    "explicit": validated.explicit,
                    "evidence": validated.evidence,
                    "extractor_version": getattr(
                        self.fact_extractor,
                        "version",
                        self.fact_extractor.__class__.__name__,
                    ),
                },
            )
            result.accepted_count += 1
            result.merged_or_saved_count += 1
        if max_event_id is not None:
            self.store.set_meta(_fact_checkpoint_key(thread_id), str(max_event_id))
        return result

    async def _summarize_thread_unchecked(self, thread_id: str) -> SummaryRunResult:
        policy = self.summary_policy
        if not policy.enabled:
            return SummaryRunResult(created=False)
        raw_conversation = self.store.list_records(
            kind=MemoryKind.CONVERSATION,
            scope_type=MemoryScopeType.THREAD,
            scope_id=thread_id,
            limit=1000,
        )
        active_summary = self.store.active_summary(thread_id=thread_id)
        if not is_identified_conversation_summary(active_summary, raw_conversation):
            active_summary = None
        covered_end = active_summary.source_event_end_id if active_summary else None
        conversation = self._conversation_records_for_summary(
            raw_conversation,
            after_event_id=covered_end,
            reserve=policy.recent_message_reserve,
        )
        total_conversation = completed_conversation_records(raw_conversation)
        if len(total_conversation) < policy.threshold_messages:
            return SummaryRunResult(created=False)
        if len(conversation) < policy.minimum_unsummarized_messages:
            return SummaryRunResult(created=False)
        segments = segment_messages(
            conversation,
            max_estimated_tokens=policy.map_chunk_estimated_tokens,
        )
        if not segments:
            return SummaryRunResult(created=False)
        previous = active_summary.content if active_summary else None
        partials: list[MemoryRecord] = []
        for segment in segments:
            content = await self.summarizer.summarize_map(
                segment.messages,
                previous_summary=previous,
            )
            partials.append(
                self.save_summary(
                    thread_id,
                    _clip(content, policy.max_summary_chars),
                    source_event_start_id=segment.source_event_start_id,
                    source_event_end_id=segment.source_event_end_id,
                    version=_summary_version(active_summary) + 1,
                    active=False,
                    summary_stage="map",
                    metadata={
                        "message_count": len(segment.messages),
                        "summarizer_version": getattr(
                            self.summarizer,
                            "version",
                            self.summarizer.__class__.__name__,
                        ),
                    },
                )
            )
        reduce_input = _bounded_reduce_input(
            [record.content for record in partials],
            max_estimated_tokens=policy.reduce_input_estimated_tokens,
        )
        reduce_text = await self.summarizer.summarize_reduce(
            reduce_input,
            previous_summary=previous,
        )
        new_start_id = partials[0].source_event_start_id
        new_end_id = partials[-1].source_event_end_id
        start_id = (
            active_summary.source_event_start_id
            if active_summary is not None and active_summary.source_event_start_id is not None
            else new_start_id
        )
        end_id = new_end_id
        version = _summary_version(active_summary) + 1
        summarized_messages = [message for segment in segments for message in segment.messages]
        summarized_message_count = len(summarized_messages)
        estimated_before = sum(
            estimate_text_tokens(str(message.content)) for message in summarized_messages
        )
        reduced = _clip(reduce_text, policy.max_summary_chars)
        estimated_after = estimate_text_tokens(reduced)
        summary = self.save_summary(
            thread_id,
            reduced,
            source_event_start_id=start_id,
            source_event_end_id=end_id,
            version=version,
            replaces=active_summary.id if active_summary else None,
            active=True,
            summary_stage="reduce",
            metadata={
                "map_summary_ids": [record.id for record in partials],
                "message_count": summarized_message_count,
                "estimated_tokens_before": estimated_before,
                "estimated_tokens_after": estimated_after,
                "compression_ratio": _compression_ratio(estimated_before, estimated_after),
                "summarizer_version": getattr(
                    self.summarizer,
                    "version",
                    self.summarizer.__class__.__name__,
                ),
            },
        )
        return SummaryRunResult(
            created=True,
            summary_id=summary.id,
            source_event_start_id=start_id,
            source_event_end_id=end_id,
            version=version,
            map_count=len(partials),
            message_count=summarized_message_count,
            estimated_tokens_before=estimated_before,
            estimated_tokens_after=estimated_after,
            compression_ratio=_compression_ratio(estimated_before, estimated_after),
        )

    def history_from_runtime_events(self, events: Iterable[RuntimeEvent]) -> list[Message]:
        messages: list[Message] = []
        for turn in _completed_runtime_turns(events):
            messages.append(Message(role="user", content=turn.user.payload["text"]))
            messages.append(Message(role="assistant", content=turn.assistant.payload["text"]))
        return messages

    def _default_scope(self, scope_type: MemoryScopeType) -> str:
        if scope_type == MemoryScopeType.USER:
            return self.user_scope
        if scope_type == MemoryScopeType.PROJECT:
            return self.project_scope
        return self.project_scope

    def _conversation_records_for_summary(
        self,
        records: list[MemoryRecord],
        *,
        after_event_id: int | None,
        reserve: int,
    ) -> list[MemoryRecord]:
        records = completed_conversation_records(records)
        turns = [records[index : index + 2] for index in range(0, len(records), 2)]
        if after_event_id is not None:
            turns = [turn for turn in turns if turn[0].source_event_start_id > after_event_id]
        eligible_count = max(0, len(turns) - (max(reserve, 0) + 1) // 2)
        safe_prefix = 0
        prefix_end = -1
        for index, turn in enumerate(turns[:eligible_count]):
            prefix_end = max(prefix_end, turn[1].source_event_start_id)
            # A summary's event range must not cover a reserved, interleaved turn.
            # Otherwise the later range filter would silently drop that whole turn.
            if index + 1 == len(turns) or prefix_end < turns[index + 1][0].source_event_start_id:
                safe_prefix = index + 1
        return [record for turn in turns[:safe_prefix] for record in turn]


def _completed_runtime_turns(events: Iterable[RuntimeEvent]) -> list[_CompletedRuntimeTurn]:
    grouped: dict[tuple[str, str, str], list[RuntimeEvent]] = {}
    seen_event_ids: set[tuple[str, int]] = set()
    for event in sorted(events, key=lambda item: (item.id, item.thread_id)):
        event_key = (event.thread_id, event.id)
        if event_key in seen_event_ids:
            continue
        seen_event_ids.add(event_key)
        if event.type not in {
            "user.message",
            "assistant.message",
            "turn.completed",
            "run.completed",
        }:
            continue
        if event.parent_run_id is not None or event.payload.get("parent_run_id") is not None:
            continue
        identity = _runtime_event_identity(event)
        if identity is not None:
            grouped.setdefault(identity, []).append(event)

    turns: list[_CompletedRuntimeTurn] = []
    for group in grouped.values():
        users = [event for event in group if event.type == "user.message"]
        assistants = [event for event in group if event.type == "assistant.message"]
        completed = [event for event in group if event.type == "turn.completed"]
        if not completed:
            completed = [event for event in group if event.type == "run.completed"]
        if not users or not assistants or not completed:
            continue
        user_texts = [event.payload.get("text") for event in users]
        assistant_texts = [event.payload.get("text") for event in assistants]
        if any(not isinstance(text, str) or not text for text in [*user_texts, *assistant_texts]):
            continue
        if len(set(user_texts)) != 1 or len(set(assistant_texts)) != 1:
            continue
        user, assistant = users[0], assistants[0]
        if assistant.id <= user.id:
            continue
        turns.append(_CompletedRuntimeTurn(user, assistant, completed[0].id))
    turns.sort(key=lambda turn: (turn.user.id, turn.user.thread_id))
    return turns


def _runtime_event_identity(event: RuntimeEvent) -> tuple[str, str, str] | None:
    fields = (
        (event.thread_id, event.payload.get("thread_id")),
        (event.run_id, event.payload.get("run_id")),
        (event.turn_id, event.payload.get("turn_id")),
    )
    identity: list[str] = []
    for envelope, payload in fields:
        if payload is not None and (not isinstance(payload, str) or not payload):
            return None
        if envelope is not None and payload is not None and envelope != payload:
            return None
        value = envelope if envelope is not None else payload
        if not isinstance(value, str) or not value:
            return None
        identity.append(value)
    return (identity[0], identity[1], identity[2])


def _bounded_preview(text: str, max_chars: int) -> str:
    limit = max(0, min(max_chars, DEFAULT_TOOL_DIGEST_CHARS))
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def _redact(text: str) -> str:
    redacted = SECRET_PATTERN.sub("[redacted]", text)
    redacted = WINDOWS_PATH_PATTERN.sub("[redacted-path]", redacted)
    return BASE64_BLOB_PATTERN.sub("[redacted-binary]", redacted)


def estimate_text_tokens(text: str) -> int:
    from axiom.memory.context import estimate_tokens

    return estimate_tokens(text)


def _summary_version(record: MemoryRecord | None) -> int:
    if record is None:
        return 0
    try:
        return int(record.metadata.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def _compression_ratio(before: int, after: int) -> float | None:
    if before <= 0:
        return None
    return round(after / before, 4)


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[truncated]"


def _bounded_reduce_input(items: list[str], *, max_estimated_tokens: int) -> list[str]:
    bounded: list[str] = []
    total = 0
    for item in items:
        tokens = estimate_text_tokens(item)
        if bounded and total + tokens > max_estimated_tokens:
            break
        if tokens > max_estimated_tokens:
            bounded.append(_clip(item, max_estimated_tokens * 4))
            break
        bounded.append(item)
        total += tokens
    return bounded


def _fact_checkpoint_key(thread_id: str) -> str:
    safe_thread_id = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    return f"fact_extracted:{safe_thread_id}"


def _int_or_none(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
