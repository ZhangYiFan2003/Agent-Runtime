from __future__ import annotations

import asyncio

import pytest

from axiom.config import AxiomConfig
from axiom.runtime import (
    BudgetExceededError,
    Checkpoint,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    NextAction,
    RunStatus,
    StepResult,
)
from axiom.tools import ToolRegistry
from axiom.types import Message


class _UnusedLlm:
    provider_name = "step-loop-test"
    model_name = "unused"
    max_context_window = 10_000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        if False:
            yield {}


def _runtime(tmp_path, store=None):
    config = AxiomConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return DurableAgentRuntime(
        llm_client=_UnusedLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=config,
        store=store or MemoryCheckpointStore(),
    )


async def _persist_initial(runtime: DurableAgentRuntime, state: Checkpoint) -> None:
    await runtime.budget_manager.initialize(state)
    await runtime.store.save(state)


def test_common_loop_preflights_and_advances_each_logical_step_once(tmp_path) -> None:
    async def scenario() -> None:
        store = MemoryCheckpointStore()
        runtime = _runtime(tmp_path, store)
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


def test_runtime_applies_completion_candidate_authoritatively(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        state = Checkpoint.create(thread_id="thread", run_id="run-complete", input="work")
        await _persist_initial(runtime, state)

        async def execute(context):
            candidate = context.run_state
            candidate.output_text = "done"
            return StepResult.propose(
                step_index=context.step_index,
                run_state=candidate,
                next_action=NextAction.COMPLETE,
            )

        result = await runtime._run_step_loop(state, execute)
        persisted = await runtime.store.load(state.run_id)

        assert result.status == RunStatus.COMPLETED
        assert persisted is not None and persisted.status == RunStatus.COMPLETED
        assert persisted.output_text == "done"

    asyncio.run(scenario())


def test_concurrent_cancel_outranks_stale_complete(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _runtime(tmp_path)
        state = Checkpoint.create(thread_id="thread", run_id="run-cancel-race", input="work")
        await _persist_initial(runtime, state)
        stale = await runtime.store.load(state.run_id)
        assert stale is not None

        cancelled = await runtime.cancel(state.run_id)
        result = await runtime._apply_next_action(
            StepResult.propose(
                step_index=stale.step_index,
                run_state=stale,
                next_action=NextAction.COMPLETE,
            )
        )

        assert cancelled.status == RunStatus.CANCELLED
        assert result.status == RunStatus.CANCELLED
        persisted = await runtime.store.load(state.run_id)
        assert persisted is not None and persisted.status == RunStatus.CANCELLED

    asyncio.run(scenario())


def test_resume_recomputes_lost_completion_policy_without_new_model_call(tmp_path) -> None:
    async def scenario() -> None:
        store = MemoryCheckpointStore()
        runtime = _runtime(tmp_path, store)
        state = Checkpoint.create(thread_id="thread", run_id="run-policy-crash", input="work")
        await _persist_initial(runtime, state)
        state.messages.append(Message(role="assistant", content="durable answer"))
        state.output_text = "durable answer"
        state.agent_turn = 1
        state.step_index = 1
        await runtime._save_checkpoint(state, operation="llm.completed")

        # Policy evaluation is ephemeral. Simulate process loss before its
        # authoritative transition write by discarding this return value.
        assert (
            await runtime._evaluate_completion(state, allow_correction=True)
            == NextAction.COMPLETE
        )

        resumed = await _runtime(tmp_path, store).resume(state.run_id)

        assert resumed.status == RunStatus.COMPLETED
        assert resumed.output_text == "durable answer"
        assert resumed.agent_turn == 1

    asyncio.run(scenario())
