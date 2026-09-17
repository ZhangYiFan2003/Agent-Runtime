from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from axiom.config import AxiomConfig
from axiom.runtime import (
    Checkpoint,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    NextAction,
    RunOwnership,
    RunStatus,
    StepContext,
    StepResult,
    ToolExecutionStatus,
)
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolResult, object_schema


class _FinalLlm:
    provider_name = "step-test"
    model_name = "final"
    max_context_window = 10_000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _runtime(tmp_path, *, store=None, registry=None, ownership=None):
    config = AxiomConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return DurableAgentRuntime(
        llm_client=_FinalLlm(),
        tool_registry=registry or ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=config,
        store=store or MemoryCheckpointStore(),
        ownership=ownership,
    )


def test_step_context_is_immutable_ephemeral_run_correlation(tmp_path):
    ownership = RunOwnership(
        run_id="run-step",
        worker_id="worker-a",
        fencing_token=3,
        lease_until=datetime.now(UTC) + timedelta(seconds=30),
    )
    runtime = _runtime(tmp_path, ownership=ownership)
    state = Checkpoint.create(thread_id="thread", run_id="run-step", input="work")

    context = runtime._step_context(state)

    assert context.run_id == state.run_id
    assert context.step_index == state.step_index
    assert context.strategy == state.execution_strategy
    assert context.run_state is state
    assert context.ownership_context is ownership
    assert not hasattr(context, "to_dict")
    with pytest.raises(FrozenInstanceError):
        context.step_index = 2  # type: ignore[misc]


def test_next_action_has_only_current_runtime_continuations():
    assert {action.value for action in NextAction} == {
        "CONTINUE",
        "COMPLETE",
        "WAIT",
        "FAIL",
    }
    interrupted = Checkpoint.create(thread_id="thread", input="work")
    interrupted.status = RunStatus.INTERRUPTED
    cancelled = Checkpoint.create(thread_id="thread", input="work")
    cancelled.status = RunStatus.CANCELLED
    waiting = Checkpoint.create(thread_id="thread", input="work")
    waiting.status = RunStatus.WAITING_CHILD

    assert StepResult.from_run_state(step_index=0, run_state=interrupted).next_action is None
    assert StepResult.from_run_state(step_index=0, run_state=cancelled).next_action is None
    assert (
        StepResult.from_run_state(step_index=0, run_state=waiting).next_action
        == NextAction.WAIT
    )


def test_one_react_llm_iteration_returns_step_result_without_changing_final_behavior(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        runtime = _runtime(tmp_path, store=store)
        state = Checkpoint.create(thread_id="thread", run_id="run-final", input="answer")
        await store.save(state)
        await runtime.budget_manager.initialize(state)

        result = await runtime._execute_react_step(runtime._step_context(state))

        assert result.step_index == 0
        assert result.run_state.status == RunStatus.COMPLETED
        assert result.run_state.output_text == "done"
        assert result.next_action == NextAction.COMPLETE
        assert result.tool_invocation_ids == ()

    asyncio.run(scenario())


def test_tool_step_references_durable_invocation_without_copying_tool_state(tmp_path):
    async def scenario():
        async def handler(payload, _context):
            return ToolResult(content=f"observed:{payload['value']}")

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="observe",
                description="observe",
                parameters=object_schema(
                    {"value": {"type": "string"}}, required=["value"]
                ),
                handler=handler,
                is_read_only=True,
            )
        )
        store = MemoryCheckpointStore()
        runtime = _runtime(tmp_path, store=store, registry=registry)
        state = Checkpoint.create(thread_id="thread", run_id="run-tool", input="observe")
        state.pending_tool_calls = [
            {
                "id": "call-observe",
                "function": {
                    "name": "observe",
                    "arguments": json.dumps({"value": "x"}),
                },
            }
        ]
        await store.save(state)
        await runtime.budget_manager.initialize(state)

        result = await runtime._execute_react_step(runtime._step_context(state))
        record = await store.load_tool_execution("run-tool:call-observe")

        assert result.tool_invocation_ids == ("run-tool:call-observe",)
        assert result.next_action == NextAction.CONTINUE
        assert record is not None and record.status == ToolExecutionStatus.SUCCEEDED
        assert not hasattr(result, "tool_result")
        assert not hasattr(result, "tool_arguments")

    asyncio.run(scenario())


def test_plan_node_and_child_run_are_not_runtime_steps(tmp_path):
    runtime = _runtime(tmp_path)
    parent = Checkpoint.create(
        thread_id="thread",
        run_id="parent",
        input="plan",
        execution_strategy="plan_execute",
    )
    parent.strategy_state["plan"] = {"tasks": [{"id": "plan-node-1"}]}
    child = Checkpoint.create(
        thread_id="thread",
        run_id="child",
        input="worker task",
        parent_run_id=parent.run_id,
        parent_step_id="plan-node-1",
    )

    context = StepContext(
        run_id=parent.run_id,
        step_index=parent.step_index,
        strategy=parent.execution_strategy,
        run_state=parent,
        control_state=parent.status,
    )

    assert context.step_index == 0
    assert parent.strategy_state["plan"]["tasks"][0]["id"] != context.step_index
    assert child.parent_run_id == parent.run_id
    assert child.run_id != context.run_id
    assert runtime._step_context(child).run_state is child
