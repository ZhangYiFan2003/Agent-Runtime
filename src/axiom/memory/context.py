from __future__ import annotations

from axiom.memory.models import (
    MemoryContextResult,
    MemoryContextSection,
    MemoryKind,
    MemoryRecord,
    MemoryScopeType,
)
from axiom.memory.store import MemoryStore

DEFAULT_MAX_CONTEXT_CHARS = 8_000
DEFAULT_MAX_ESTIMATED_TOKENS = 2_000
DEFAULT_MAX_RECORDS = 40
HARD_MAX_CONTEXT_CHARS = 24_000
HARD_MAX_ESTIMATED_TOKENS = 6_000
HARD_MAX_RECORDS = 100


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4, len(text.splitlines()))


class MemoryContextBuilder:
    def __init__(self, store: MemoryStore):
        self.store = store

    def build(
        self,
        *,
        thread_id: str | None = None,
        project_scope: str | None = None,
        user_scope: str | None = None,
        query: str = "",
        recent_limit: int = 8,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        max_estimated_tokens: int = DEFAULT_MAX_ESTIMATED_TOKENS,
        max_records: int = DEFAULT_MAX_RECORDS,
    ) -> MemoryContextResult:
        max_context_chars = min(max(max_context_chars, 1), HARD_MAX_CONTEXT_CHARS)
        max_estimated_tokens = min(max(max_estimated_tokens, 1), HARD_MAX_ESTIMATED_TOKENS)
        max_records = min(max(max_records, 1), HARD_MAX_RECORDS)
        raw_conversation_records = (
            self.store.list_records(
                kind=MemoryKind.CONVERSATION,
                scope_type=MemoryScopeType.THREAD,
                scope_id=thread_id,
                limit=1000,
            )
            if thread_id
            else []
        )
        summary_record = self.store.active_summary(thread_id=thread_id) if thread_id else None
        if not is_identified_conversation_summary(summary_record, raw_conversation_records):
            summary_record = None
        all_conversation_records = completed_conversation_records(raw_conversation_records)
        candidates = self._candidate_records(
            thread_id=thread_id,
            project_scope=project_scope,
            user_scope=user_scope,
            query=query,
            recent_limit=recent_limit,
            summary=summary_record,
            conversation=all_conversation_records,
        )
        sections: list[MemoryContextSection] = []
        raw_skipped = _count_summarized_records(all_conversation_records, summary_record)
        before_tokens = sum(estimate_tokens(record.content) for record in all_conversation_records)
        chars = 0
        tokens = 0
        records = 0
        evicted = 0
        truncated = False
        for name, candidate_records in candidates:
            records_for_section = list(candidate_records)
            if not records_for_section:
                continue
            while records_for_section:
                section = _section(name, records_for_section)
                next_chars = chars + len(section.content)
                next_tokens = tokens + section.estimated_tokens
                next_records = records + len(section.records)
                if (
                    next_chars <= max_context_chars
                    and next_tokens <= max_estimated_tokens
                    and next_records <= max_records
                ):
                    sections.append(section)
                    chars = next_chars
                    tokens = next_tokens
                    records = next_records
                    break
                truncated = True
                if name == "recent conversation":
                    next_records = _drop_oldest_conversation_turn(records_for_section)
                    evicted += len(records_for_section) - len(next_records)
                    records_for_section = next_records
                else:
                    evicted += 1
                    records_for_section = records_for_section[:-1]
        return MemoryContextResult(
            sections=sections,
            estimated_chars=chars,
            estimated_tokens=tokens,
            truncated=truncated,
            evicted_count=evicted,
            included_count=records,
            summary_used=summary_record is not None,
            summary_source_start_id=(
                summary_record.source_event_start_id if summary_record is not None else None
            ),
            summary_source_end_id=(
                summary_record.source_event_end_id if summary_record is not None else None
            ),
            raw_messages_included=sum(
                len(section.records)
                for section in sections
                if section.name == "recent conversation"
            ),
            raw_messages_skipped_by_summary=raw_skipped,
            estimated_tokens_before_compression=before_tokens,
            estimated_tokens_after_compression=tokens,
            compression_ratio=_compression_ratio(before_tokens, tokens),
        )

    def _candidate_records(
        self,
        *,
        thread_id: str | None,
        project_scope: str | None,
        user_scope: str | None,
        query: str,
        recent_limit: int,
        summary: MemoryRecord | None,
        conversation: list[MemoryRecord],
    ) -> list[tuple[str, list[MemoryRecord]]]:
        sections: list[tuple[str, list[MemoryRecord]]] = []
        if thread_id:
            summaries = [summary] if summary is not None else []
            sections.append(("latest summary", summaries))
            summary_end = summary.source_event_end_id if summary is not None else None
            recent = _exclude_summarized(
                conversation,
                summary_end=summary_end,
            )
            recent_count = max(recent_limit, 0) // 2 * 2
            recent = recent[-recent_count:] if recent_count else []
            sections.append(("recent conversation", recent))
            tools = self.store.list_records(
                kind=MemoryKind.TOOL_RESULT,
                scope_type=MemoryScopeType.THREAD,
                scope_id=thread_id,
                limit=recent_limit,
            )
            sections.append(("tool result digests", tools))
        facts: list[MemoryRecord] = []
        scopes = []
        if thread_id:
            scopes.append((MemoryScopeType.THREAD, thread_id))
        if project_scope:
            scopes.append((MemoryScopeType.PROJECT, project_scope))
        if user_scope:
            scopes.append((MemoryScopeType.USER, user_scope))
        if scopes:
            facts = _collapse_fact_overrides(
                _rank_facts(
                    self.store.search_records(
                        query,
                        kinds=[MemoryKind.FACT],
                        scopes=scopes,
                        limit=50,
                    )
                )
            )[:20]
        sections.insert(2, ("facts and preferences", facts))
        return sections


def _rank_facts(records: list[MemoryRecord]) -> list[MemoryRecord]:
    def key(record: MemoryRecord) -> tuple[int, float, str, str, str]:
        category = str(record.metadata.get("category") or "")
        scope_rank = {
            MemoryScopeType.THREAD: 0,
            MemoryScopeType.PROJECT: 1,
            MemoryScopeType.USER: 2,
        }.get(record.scope_type, 3)
        category_rank = {
            "constraint": 0,
            "project_decision": 1,
            "workflow": 2,
            "preference": 3,
            "environment": 4,
            "identity-neutral_profile": 5,
            "fact": 6,
        }.get(category, 7)
        try:
            confidence = float(record.metadata.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return (
            scope_rank * 10 + category_rank,
            -confidence,
            str(record.metadata.get("key") or ""),
            record.updated_at,
            record.id,
        )

    return sorted(records, key=key)


def _collapse_fact_overrides(records: list[MemoryRecord]) -> list[MemoryRecord]:
    selected: list[MemoryRecord] = []
    seen_keys: set[str] = set()
    for record in records:
        logical_key = str(record.metadata.get("key") or record.id)
        if logical_key in seen_keys:
            continue
        seen_keys.add(logical_key)
        selected.append(record)
    return selected


def _section(name: str, records: list[MemoryRecord]) -> MemoryContextSection:
    content = _serialize_section(name, records)
    return MemoryContextSection(
        name=name,
        records=records,
        content=content,
        estimated_tokens=estimate_tokens(content),
    )


def _serialize_section(name: str, records: list[MemoryRecord]) -> str:
    if not records:
        return ""
    rows = [f"[{name}]"]
    for record in records:
        metadata = _metadata_suffix(record)
        prefix = f"- {record.kind.value}:{record.scope_type.value}:{record.scope_id}{metadata}: "
        rows.append(f"{prefix}{record.content}")
    return "\n".join(rows) + "\n"


def _metadata_suffix(record: MemoryRecord) -> str:
    if record.kind != MemoryKind.FACT:
        return ""
    category = record.metadata.get("category")
    key = record.metadata.get("key")
    source = ""
    if record.source_event_start_id is not None:
        source = f" events={record.source_event_start_id}-{record.source_event_end_id}"
    parts = [str(item) for item in (category, key) if item]
    return f" [{' '.join(parts)}{source}]" if parts or source else ""


def completed_conversation_records(records: list[MemoryRecord]) -> list[MemoryRecord]:
    """Return identified root pairs; Runtime writes these only after completion."""
    grouped: dict[tuple[str, str, str], list[MemoryRecord]] = {}
    for record in records:
        run_id = record.metadata.get("run_id")
        turn_id = record.metadata.get("turn_id")
        if not isinstance(run_id, str) or not run_id:
            continue
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if record.metadata.get("parent_run_id") is not None:
            continue
        if record.metadata.get("thread_id", record.scope_id) != record.scope_id:
            continue
        grouped.setdefault((record.scope_id, run_id, turn_id), []).append(record)

    pairs: list[tuple[MemoryRecord, MemoryRecord]] = []
    for group in grouped.values():
        users = [record for record in group if record.metadata.get("role") == "user"]
        assistants = [record for record in group if record.metadata.get("role") == "assistant"]
        if len({record.content for record in users}) != 1:
            continue
        if len({record.content for record in assistants}) != 1:
            continue
        users = [record for record in users if record.source_event_start_id is not None]
        assistants = [record for record in assistants if record.source_event_start_id is not None]
        if not users or not assistants:
            continue
        user = min(users, key=lambda record: (record.source_event_start_id, record.id))
        assistant = min(assistants, key=lambda record: (record.source_event_start_id, record.id))
        if assistant.source_event_start_id <= user.source_event_start_id:
            continue
        pairs.append((user, assistant))
    pairs.sort(key=lambda pair: (pair[0].source_event_start_id, pair[0].scope_id, pair[0].id))
    return [record for pair in pairs for record in pair]


def is_identified_conversation_summary(
    summary: MemoryRecord | None, records: list[MemoryRecord]
) -> bool:
    """Only reuse summaries whose retained source range proves identified root pairs."""
    if summary is None:
        return False
    start, end = summary.source_event_start_id, summary.source_event_end_id
    if start is None or end is None or start > end:
        return False
    covered = [
        record
        for record in records
        if record.scope_id == summary.scope_id
        and record.source_event_start_id is not None
        and start <= record.source_event_start_id <= end
    ]
    identified = completed_conversation_records(covered)
    if not identified:
        return False
    if min(record.source_event_start_id for record in identified) != start:
        return False
    if max(record.source_event_start_id for record in covered) != end:
        return False
    identities = {
        (
            record.metadata["run_id"],
            record.metadata["turn_id"],
            record.metadata["role"],
            record.content,
        )
        for record in identified
    }
    return all(
        isinstance(record.metadata.get("run_id"), str)
        and isinstance(record.metadata.get("turn_id"), str)
        and isinstance(record.metadata.get("role"), str)
        and record.metadata.get("parent_run_id") is None
        and record.metadata.get("thread_id", record.scope_id) == summary.scope_id
        and (
            record.metadata.get("run_id"),
            record.metadata.get("turn_id"),
            record.metadata.get("role"),
            record.content,
        )
        in identities
        for record in covered
    )


def _drop_oldest_conversation_turn(records: list[MemoryRecord]) -> list[MemoryRecord]:
    if len(records) >= 2:
        first_role = records[0].metadata.get("role")
        second_role = records[1].metadata.get("role")
        if first_role == "user" and second_role == "assistant":
            return records[2:]
    return records[1:]


def _exclude_summarized(
    records: list[MemoryRecord],
    *,
    summary_end: int | None,
) -> list[MemoryRecord]:
    if summary_end is None:
        return records
    return [
        record
        for record in records
        if record.source_event_start_id is None or record.source_event_start_id > summary_end
    ]


def _count_summarized_records(
    records: list[MemoryRecord],
    summary: MemoryRecord | None,
) -> int:
    if summary is None or summary.source_event_end_id is None:
        return 0
    return sum(
        1
        for record in records
        if record.source_event_start_id is not None
        and record.source_event_start_id <= summary.source_event_end_id
    )


def _compression_ratio(before: int, after: int) -> float | None:
    if before <= 0:
        return None
    return round(after / before, 4)
