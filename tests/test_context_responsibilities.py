from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from axiom.context import (
    ContextBudget,
    ContextBudgetExceededError,
    ContextBudgetPolicy,
    ContextBuilder,
)
from axiom.runtime import Checkpoint, MemoryCheckpointStore
from axiom.types import Message


def _policy(**overrides) -> ContextBudgetPolicy:
    values = {
        "model_context_window": 1_200,
        "reserved_output_tokens": 200,
        "high_watermark_ratio": 0.4,
        "target_after_compaction_ratio": 0.25,
        "recent_message_reserve": 2,
        "hard_input_limit": 1_000,
        "max_tool_result_chars": 240,
    }
    values.update(overrides)
    return ContextBudgetPolicy(**values)


def _history(count: int = 8, size: int = 300) -> list[Message]:
    return [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=f"history-{index} " + chr(97 + index) * size,
        )
        for index in range(count)
    ]


def test_builder_selects_desired_sources_independent_of_token_pressure():
    messages = [*_history(), Message(role="user", content="current objective")]
    builder = ContextBuilder()

    desired = builder.build(
        messages,
        system_prompt="system",
        tools=[{"type": "function", "function": {"name": "lookup"}}],
        objective="current objective",
    )

    assert [message.content for message in desired.messages] == [
        message.content for message in messages
    ]
    assert desired.required_message_indexes == frozenset({len(messages) - 1})
    assert desired.source_metadata[0] == ("system", "system_prompt", "required")
    assert len(desired.messages) == len(messages)


def test_budget_compacts_optional_context_but_preserves_required_objective():
    async def scenario():
        messages = [*_history(), Message(role="user", content="REQUIRED_OBJECTIVE")]
        desired = ContextBuilder().build(
            messages,
            system_prompt="system",
            objective="REQUIRED_OBJECTIVE",
        )

        result = await ContextBudget(_policy()).fit(desired)

        rendered = "\n".join(str(message.content) for message in result.messages)
        assert result.compacted
        assert "REQUIRED_OBJECTIVE" in rendered
        assert result.evicted_messages > 0

    asyncio.run(scenario())


def test_budget_keeps_tool_protocol_units_atomic():
    async def scenario():
        call = {
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }
        messages = [
            Message(role="assistant", content="", tool_calls=[call]),
            Message(role="tool", content="durable evidence", tool_call_id="call-1"),
            *_history(),
            Message(role="user", content="current objective"),
        ]
        desired = ContextBuilder().build(
            messages,
            system_prompt="system",
            objective="current objective",
        )

        result = await ContextBudget(_policy()).fit(desired)
        retained_call = any(message.tool_calls for message in result.messages)
        retained_result = any(message.tool_call_id == "call-1" for message in result.messages)

        assert retained_call is retained_result

    asyncio.run(scenario())


def test_output_reserve_defines_input_budget_and_required_overflow_fails():
    policy = _policy(
        model_context_window=800,
        reserved_output_tokens=200,
        hard_input_limit=600,
        recent_message_reserve=2,
    ).normalized()
    assert policy.usable_input == 600
    desired = ContextBuilder().build(
        [Message(role="user", content="required " + "x" * 4_000)],
        system_prompt="system",
        objective="required " + "x" * 4_000,
    )

    with pytest.raises(ContextBudgetExceededError) as captured:
        asyncio.run(ContextBudget(policy).fit(desired))

    assert captured.value.hard_input_limit == policy.usable_input


def test_builder_and_budget_do_not_mutate_authoritative_run_state():
    async def scenario():
        state = Checkpoint.create(
            thread_id="thread",
            run_id="run-context",
            input="current objective",
            history=[*_history(), Message(role="user", content="current objective")],
        )
        before = deepcopy(state.to_dict())
        desired = ContextBuilder().build(
            state.messages,
            system_prompt="system",
            objective=state.input,
        )
        desired.messages[0].content = "mutated ephemeral copy"
        await ContextBudget(_policy()).fit(desired)

        assert state.to_dict() == before

    asyncio.run(scenario())


def test_context_build_result_is_ephemeral_and_rebuild_is_deterministic():
    async def scenario():
        store = MemoryCheckpointStore()
        state = Checkpoint.create(
            thread_id="thread",
            run_id="run-rebuild",
            input="current objective",
            history=[*_history(2), Message(role="user", content="current objective")],
        )
        await store.save(state)
        loaded = await store.load(state.run_id)
        assert loaded is not None
        builder = ContextBuilder()

        first = builder.build(
            state.messages,
            system_prompt="system",
            objective=state.input,
        )
        rebuilt = builder.build(
            loaded.messages,
            system_prompt="system",
            objective=loaded.input,
        )

        assert not hasattr(first, "to_dict")
        assert first.source_metadata == rebuilt.source_metadata
        assert list(first.messages) == list(rebuilt.messages)

    asyncio.run(scenario())
