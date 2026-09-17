from __future__ import annotations

import asyncio

import pytest

from axiom.config import AxiomConfig
from axiom.runtime import (
    BudgetExceededError,
    Checkpoint,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    RunStatus,
    StepResult,
)
from axiom.tools import ToolRegistry


class _UnusedLlm:
    provider_name = "step-loop-test"
    model_name = "unused"
    max_context_window = 10_000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        if False:
            yield {}


def _runtime(tmp_path):
    config = AxiomConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return DurableAgentRuntime(
        llm_client=_UnusedLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=config,
        store=MemoryCheckpointStore(),
    )


async def _persist_initial(runtime: DurableAgentRuntime, state: Checkpoint) -> None:
    await runtime.budget_manager.initialize(state)
    await runtime.store.save(state)


def test_common_loop_preflights_and_advances_each_logical_step_once(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        state = Checkpoint.create(thread_id="thread", run_id="run-loop", input="work")
        await _persist_initial(runtime, state)
        observed: list[int] = []

        async def execute(context):
            current = context.run_state
            observed.append(context.step_index)
            current.step_index += 1
            if len(observed) == 2:
                current.status = RunStatus.COMPLETED
            await runtime._save_checkpoint(current, operation=f"test.step.{len(observed)}")
            return StepResult.from_run_state(
                step_index=context.step_index,
                run_state=current,
            )

        result = await runtime._run_step_loop(state, execute)

        assert observed == [0, 1]
        assert result.status == RunStatus.COMPLETED
        assert result.step_index == 2

    asyncio.run(scenario())


def test_wait_stops_without_spinning_another_step(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        state = Checkpoint.create(thread_id="thread", run_id="run-wait", input="work")
        await _persist_initial(runtime, state)
        calls = 0

        async def execute(context):
            nonlocal calls
            calls += 1
            current = context.run_state
            current.status = RunStatus.WAITING_CHILD
            await runtime._save_checkpoint(current, operation="test.wait")
            return StepResult.from_run_state(
                step_index=context.step_index,
                run_state=current,
            )

        result = await runtime._run_step_loop(state, execute)

        assert calls == 1
        assert result.status == RunStatus.WAITING_CHILD

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [RunStatus.CANCELLED, RunStatus.INTERRUPTED])
def test_control_state_prevents_step_creation(tmp_path, status: RunStatus) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        state = Checkpoint.create(thread_id="thread", input="work")
        state.status = status
        await _persist_initial(runtime, state)

        async def execute(_context):
            raise AssertionError("control state must prevent ordinary Step execution")

        result = await runtime._run_step_loop(state, execute)

        assert result.status == status
        assert result.step_index == 0

    asyncio.run(scenario())


def test_hard_budget_preflight_preserves_specific_failure(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        state = Checkpoint.create(thread_id="thread", run_id="run-budget", input="work")
        await _persist_initial(runtime, state)

        async def exhausted(_state):
            raise BudgetExceededError(
                code="WALL_TIME_BUDGET_EXCEEDED",
                dimension="elapsed_seconds",
                limit=1,
                used=2,
                run_id=state.run_id,
            )

        runtime.budget_manager.ensure_wall_time = exhausted  # type: ignore[method-assign]

        async def execute(_context):
            raise AssertionError("budget failure must prevent ordinary Step execution")

        result = await runtime._run_step_loop(state, execute)

        assert result.status == RunStatus.FAILED
        assert result.error is not None
        assert result.error.type == "WALL_TIME_BUDGET_EXCEEDED"
        assert result.error.step == "step_preflight"

    asyncio.run(scenario())
