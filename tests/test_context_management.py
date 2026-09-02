from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from axiom.config import AxiomConfig
from axiom.context import (
    CONTEXT_BUDGET_EXCEEDED,
    DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW,
    ContextBudgetExceededError,
    ContextBudgetPolicy,
    ContextManager,
    RuntimeContextSummary,
    apply_compaction_to_strategy_state,
    context_policy_from_config,
    summary_from_strategy_state,
)
from axiom.runtime import (
    Checkpoint,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RunStatus,
    SpanType,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from axiom.runtime.api import ThreadEventRepository
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry
from axiom.types import Message


def _policy(**overrides) -> ContextBudgetPolicy:
    values = {
        "model_context_window": 2_000,
        "reserved_output_tokens": 200,
        "high_watermark_ratio": 0.5,
        "target_after_compaction_ratio": 0.3,
        "recent_message_reserve": 2,
        "hard_input_limit": 1_800,
        "max_tool_result_chars": 240,
    }
    values.update(overrides)
    return ContextBudgetPolicy(**values)


def _long_messages(count: int, *, chars: int = 500) -> list[Message]:
    return [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=f"message-{index} " + (chr(97 + index % 20) * chars),
        )
        for index in range(count)
    ]


def _prepare(manager: ContextManager, messages: list[Message], **kwargs):
    return asyncio.run(
        manager.prepare(
            messages,
            system_prompt=str(kwargs.pop("system_prompt", "system")),
            tools=kwargs.pop("tools", []),
            **kwargs,
        )
    )


def _assert_valid_tool_protocol(messages: list[Message]) -> None:
    assistant_calls: dict[str, int] = {}
    tool_results: dict[str, int] = {}
    for index, message in enumerate(messages):
        for call in message.tool_calls:
            assistant_calls[str(call.get("id") or "")] = index
        if message.role == "tool" and message.tool_call_id:
            tool_results[message.tool_call_id] = index
    assert assistant_calls.keys() == tool_results.keys()
    assert all(assistant_calls[key] < tool_results[key] for key in assistant_calls)


def test_budget_below_watermark_does_not_compact_and_output_reserve_is_respected():
    policy = _policy(model_context_window=1_000, reserved_output_tokens=300, hard_input_limit=700)
    manager = ContextManager(policy)
    result = _prepare(manager, [Message(role="user", content="small request")])

    assert policy.normalized().usable_input == 700
    assert policy.normalized().high_watermark_tokens == 350
    assert policy.normalized().target_after_compaction_tokens == 210
    assert result.compacted is False
    assert result.messages[0].content == "small request"


def test_budget_above_watermark_compacts_old_context_and_preserves_recent_messages():
    messages = _long_messages(10)
    manager = ContextManager(_policy())
    result = _prepare(manager, messages, objective=str(messages[-2].content))

    assert result.compacted is True
    assert result.estimated_tokens_after < result.estimated_tokens_before
    assert result.messages[0].role == "system"
    assert [message.content for message in result.messages[-2:]] == [
        message.content for message in messages[-2:]
    ]


def test_unknown_model_uses_deterministic_fallback_limit():
    class UnknownClient:
        model_name = "unknown"
        provider_name = "unknown"

    policy = context_policy_from_config(AxiomConfig(), UnknownClient())
    assert policy.model_context_window == DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW


@pytest.mark.parametrize(
    "policy",
    [
        _policy(reserved_output_tokens=2_000),
        _policy(high_watermark_ratio=0),
        _policy(target_after_compaction_ratio=0.5),
        _policy(hard_input_limit=1_900),
        _policy(recent_message_reserve=-1),
    ],
)
def test_invalid_budget_configuration_is_rejected(policy: ContextBudgetPolicy):
    with pytest.raises(ValueError):
        policy.normalized()


def test_objective_constraints_open_work_and_files_survive_structured_compaction():
    messages = [
        Message(
            role="user",
            content=(
                "Must preserve the audit trail.\n"
                "Remaining open work: verify src/axiom/runtime/durable.py. " + "x" * 600
            ),
        ),
        Message(role="assistant", content="Implemented the initial decision. " + "y" * 600),
        *_long_messages(6),
        Message(role="user", content="CURRENT OBJECTIVE: finish context management"),
        Message(role="assistant", content="working"),
    ]
    result = _prepare(
        ContextManager(_policy()),
        messages,
        objective="CURRENT OBJECTIVE: finish context management",
    )

    assert result.summary is not None
    assert "CURRENT OBJECTIVE" in result.summary.objective
    assert any("audit trail" in value for value in result.summary.constraints)
    assert any("Remaining open work" in value for value in result.summary.open_work)
    assert any("src/axiom/runtime/durable.py" in value for value in result.summary.modified_files)


class _TrackingSummarizer:
    version = "tracking"

    def __init__(self) -> None:
        self.map_previous: list[str | None] = []
        self.map_sizes: list[int] = []

    async def summarize_map(self, messages, *, previous_summary=None):
        self.map_previous.append(previous_summary)
        self.map_sizes.append(len(messages))
        return f"mapped {len(messages)}"

    async def summarize_reduce(self, partial_summaries, *, previous_summary=None):
        return "reduced: " + ", ".join(partial_summaries)


def test_incremental_summary_reuses_previous_prefix_without_summary_duplication():
    summarizer = _TrackingSummarizer()
    manager = ContextManager(_policy(), summarizer=summarizer)
    original = _long_messages(10)
    first = _prepare(manager, original, objective="objective")
    first_map_total = sum(summarizer.map_sizes)
    extended = [*original, *_long_messages(4, chars=550)]
    second = _prepare(
        manager,
        extended,
        objective="objective",
        previous_summary=first.summary,
    )

    assert second.compacted is True
    assert second.summary_reused is True
    assert any(previous is not None for previous in summarizer.map_previous)
    assert sum(summarizer.map_sizes) - first_map_total < second.summary.covered_message_count
    assert sum(message.role == "system" for message in second.messages) == 1


def test_tool_call_and_result_are_compacted_atomically():
    call = {
        "id": "call_old",
        "type": "function",
        "function": {"name": "search", "arguments": "{}"},
    }
    messages = [
        Message(role="user", content="old objective " + "x" * 500),
        Message(role="assistant", content="", tool_calls=[call]),
        Message(role="tool", content="old result " + "r" * 500, tool_call_id="call_old"),
        *_long_messages(6),
    ]
    result = _prepare(ContextManager(_policy()), messages, objective="current objective")

    _assert_valid_tool_protocol(result.messages)
    projected_ids = {message.tool_call_id for message in result.messages if message.role == "tool"}
    assert "call_old" not in projected_ids


def test_pending_current_tool_interaction_is_pinned():
    pending = {
        "id": "call_pending",
        "type": "function",
        "function": {"name": "write", "arguments": "{}"},
    }
    messages = [
        *_long_messages(8),
        Message(role="assistant", content="", tool_calls=[pending]),
    ]
    result = _prepare(
        ContextManager(_policy(hard_input_limit=1_800)),
        messages,
        objective="continue pending tool",
    )

    assert any(
        any(call.get("id") == "call_pending" for call in message.tool_calls)
        for message in result.messages
    )


@pytest.mark.parametrize("is_error", [False, True])
def test_oversized_historical_tool_result_has_explicit_bounded_projection(is_error: bool):
    call = {
        "id": "call_large",
        "type": "function",
        "function": {"name": "shell", "arguments": "{}"},
    }
    raw = ("error: command failed\n" if is_error else "command succeeded\n") + "z" * 2_000
    messages = [
        Message(role="assistant", content="", tool_calls=[call]),
        Message(
            role="tool",
            content=raw,
            name=f"shell{' (error)' if is_error else ''}",
            tool_call_id="call_large",
        ),
        Message(role="user", content="current objective"),
        Message(role="assistant", content="continue"),
    ]
    result = _prepare(
        ContextManager(_policy(model_context_window=20_000, hard_input_limit=19_800)),
        messages,
        objective="current objective",
    )
    projected = next(message for message in result.messages if message.role == "tool")
    content = str(projected.content)

    assert result.tool_results_projected == 1
    assert "bounded historical tool result" in content
    assert "tool: shell" in content
    assert f"status: {'error' if is_error else 'success'}" in content
    assert "original_chars:" in content and "truncated: true" in content
    assert len(content) < len(raw)
    _assert_valid_tool_protocol(result.messages)


def test_compaction_does_not_mutate_raw_messages_or_checkpoint_history():
    messages = _long_messages(10)
    original = deepcopy(messages)
    checkpoint = Checkpoint.create(thread_id="thread", input="current", history=messages)
    state_before = checkpoint.to_dict()

    _prepare(ContextManager(_policy()), checkpoint.messages, objective="current")

    assert messages == original
    assert checkpoint.to_dict() == state_before


def test_compaction_does_not_delete_runtime_events_checkpoints_or_tool_executions(tmp_path):
    async def scenario():
        events = ThreadEventRepository(tmp_path / "events.db")
        thread_id = events.create_thread()
        event_id = events.append_event(thread_id, "user.message", {"text": "durable raw"})
        store = MemoryCheckpointStore()
        checkpoint = Checkpoint.create(
            thread_id=thread_id,
            input="current objective",
            history=_long_messages(10),
            run_id="run_durable_sources",
        )
        await store.save(checkpoint)
        record = ToolExecutionRecord(
            invocation_id="run_durable_sources:call_1",
            run_id=checkpoint.run_id,
            tool_call_id="call_1",
            tool_name="shell",
            arguments_hash="hash",
            status=ToolExecutionStatus.SUCCEEDED,
            result="full durable result " + "z" * 2_000,
        )
        await store.save_tool_execution(record)

        await ContextManager(_policy()).prepare(
            checkpoint.messages,
            system_prompt="system",
            objective=checkpoint.input,
        )

        assert any(event.id == event_id for event in events.list_events(thread_id))
        assert await store.load(checkpoint.run_id) is not None
        persisted_tool = await store.load_tool_execution(record.invocation_id)
        assert persisted_tool is not None and persisted_tool.result == record.result

    asyncio.run(scenario())


def test_restart_rebuilds_projection_from_raw_checkpoint_and_derived_summary():
    async def scenario():
        store = MemoryCheckpointStore()
        manager = ContextManager(_policy())
        checkpoint = Checkpoint.create(
            thread_id="thread_restart",
            input="current objective",
            history=_long_messages(10),
            run_id="run_restart_context",
        )
        result = await manager.prepare(
            checkpoint.messages,
            system_prompt="system",
            objective=checkpoint.input,
        )
        apply_compaction_to_strategy_state(checkpoint.strategy_state, result)
        raw_before = deepcopy(checkpoint.messages)
        await store.save(checkpoint)
        loaded = await store.load(checkpoint.run_id)
        assert loaded is not None
        rebuilt = await manager.prepare(
            loaded.messages,
            system_prompt="system",
            objective=loaded.input,
            previous_summary=summary_from_strategy_state(loaded.strategy_state),
        )
        assert loaded.messages == raw_before
        assert rebuilt.summary_reused is True
        assert sum(message.role == "system" for message in rebuilt.messages) == 1

    asyncio.run(scenario())


class _FailingSummarizer:
    async def summarize_map(self, messages, *, previous_summary=None):
        raise RuntimeError("summary failed")

    async def summarize_reduce(self, partial_summaries, *, previous_summary=None):
        raise RuntimeError("summary failed")


def test_summary_failure_uses_deterministic_fallback_without_corrupting_prior_state():
    previous = RuntimeContextSummary(
        objective="old objective",
        covered_message_count=0,
        source_fingerprint="",
    )
    persisted = {"summary": previous.to_dict(), "checkpoint_sequence": 7}
    before = deepcopy(persisted)
    result = _prepare(
        ContextManager(_policy(), summarizer=_FailingSummarizer()),
        _long_messages(10),
        objective="current objective",
    )

    assert result.compacted is True
    assert result.summary is not None
    assert persisted == before


def test_pinned_context_alone_over_hard_limit_returns_structured_error():
    manager = ContextManager(
        _policy(
            model_context_window=800,
            reserved_output_tokens=100,
            hard_input_limit=700,
            recent_message_reserve=4,
        )
    )
    with pytest.raises(ContextBudgetExceededError) as captured:
        _prepare(manager, [Message(role="user", content="x" * 4_000)], objective="x" * 4_000)
    assert captured.value.code == CONTEXT_BUDGET_EXCEEDED


class _RecordingFinalLlm:
    provider_name = "test"
    model_name = "test-model"
    max_context_window = 10_000

    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[list[Message]] = []

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        self.calls += 1
        self.messages.append(deepcopy(messages))
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "usage": {"input_tokens": 5, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _runtime(
    tmp_path,
    *,
    llm: _RecordingFinalLlm,
    config: AxiomConfig,
    store: MemoryCheckpointStore,
    observations: MemoryObservabilityStore | None = None,
) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=ToolRegistry(),
        system_prompt="system",
        cwd=str(tmp_path),
        config=config,
        store=store,
        tracer=RunTracer(observations) if observations is not None else None,
    )


def test_hard_limit_failure_makes_no_provider_call_and_keeps_raw_checkpoint(tmp_path):
    async def scenario():
        config = AxiomConfig()
        config.context.model_context_window = 800
        config.context.reserved_output_tokens = 100
        config.context.hard_input_limit = 700
        config.context.recent_message_reserve = 4
        llm = _RecordingFinalLlm()
        store = MemoryCheckpointStore()
        state = await _runtime(
            tmp_path,
            llm=llm,
            config=config,
            store=store,
        ).start(thread_id="thread_hard", input="x" * 4_000, run_id="run_hard")
        persisted = await store.load(state.run_id)

        assert state.status == RunStatus.FAILED
        assert state.error is not None and state.error.type == CONTEXT_BUDGET_EXCEEDED
        assert llm.calls == 0
        assert persisted is not None
        assert str(persisted.messages[0].content) == "x" * 4_000

    asyncio.run(scenario())


def test_durable_compaction_preserves_raw_state_and_records_observability(tmp_path):
    async def scenario():
        config = AxiomConfig()
        config.context.model_context_window = 2_000
        config.context.reserved_output_tokens = 200
        config.context.hard_input_limit = 1_800
        config.context.high_watermark_ratio = 0.5
        config.context.target_after_compaction_ratio = 0.3
        config.context.recent_message_reserve = 2
        llm = _RecordingFinalLlm()
        store = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        history = _long_messages(10)
        state = await _runtime(
            tmp_path,
            llm=llm,
            config=config,
            store=store,
            observations=observations,
        ).start(
            thread_id="thread_trace_context",
            input="current objective",
            history=history,
            run_id="run_trace_context",
        )
        bundle = await ObservabilityService(observations).trace(state.run_id)

        assert state.status == RunStatus.COMPLETED
        assert state.messages[: len(history)] == history
        assert llm.calls == 1 and llm.messages[0][0].role == "system"
        assert bundle is not None
        llm_span = next(span for span in bundle.spans if span.span_type == SpanType.LLM)
        assert llm_span.attributes["context.compaction_triggered"] is True
        assert (
            llm_span.attributes["context.estimated_tokens_before"]
            > llm_span.attributes["context.estimated_tokens_after"]
        )
        assert llm_span.attributes["context.evicted_messages"] > 0
        assert llm_span.attributes["context.preserved_messages"] >= 2

    asyncio.run(scenario())
