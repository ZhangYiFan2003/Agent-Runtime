from __future__ import annotations

import asyncio
import json

import pytest

from axiom.config import AxiomConfig
from axiom.evaluation.badcases import FailureType, classify_failure
from axiom.evaluation.models import EvaluationRunResult
from axiom.runtime import DurableAgentRuntime, MemoryCheckpointStore, RunStatus
from axiom.runtime.models import Checkpoint, RunError
from axiom.runtime.progress import (
    ProgressDecisionType,
    ProgressDetector,
    ProgressDetectorType,
    ProgressObservation,
    ProgressPolicy,
    ProgressState,
    action_fingerprint,
    error_fingerprint,
    state_fingerprint,
)
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolResult, object_schema


def _policy(**overrides) -> ProgressPolicy:
    values = {
        "max_identical_actions": 4,
        "max_identical_errors": 3,
        "max_cycle_repetitions": 3,
        "max_stagnant_steps": 8,
        "max_recovery_attempts": 1,
        "history_limit": 32,
    }
    values.update(overrides)
    return ProgressPolicy(**values)


def _observe(
    detector: ProgressDetector,
    state: ProgressState,
    index: int,
    *,
    action: str | None = None,
    error: str | None = None,
    progress: str | None = None,
):
    return detector.observe(
        state,
        ProgressObservation(
            operation_id=f"op-{index}",
            step=index,
            action_fingerprint=action,
            error_fingerprint=error,
            state_fingerprint=progress,
        ),
    )


def test_identical_actions_trigger_only_at_threshold_and_progress_resets() -> None:
    detector = ProgressDetector(_policy(max_identical_actions=3))
    state = ProgressState()
    assert _observe(detector, state, 1, action="A", progress="S1").decision == "CONTINUE"
    assert _observe(detector, state, 2, action="A", progress="S1").decision == "CONTINUE"
    decision = _observe(detector, state, 3, action="A", progress="S1")
    assert decision.decision == ProgressDecisionType.RECOVER
    assert decision.detector_type == ProgressDetectorType.IDENTICAL_ACTION_LOOP
    assert _observe(detector, state, 4, action="A", progress="S2").decision == "CONTINUE"
    assert state.recovery_attempts == 0
    assert state.stagnant_steps == 0


def test_repeated_errors_ignore_volatile_ids_but_keep_different_errors_distinct() -> None:
    first = error_fingerprint(
        "TimeoutError",
        "failed at 2026-09-01T10:11:12Z request_id=abc-123 address 0xabc",
        tool_name="shell",
    )
    second = error_fingerprint(
        "TimeoutError",
        "failed at 2026-09-02T11:12:13Z request_id=xyz-999 address 0xdef",
        tool_name="shell",
    )
    assert first == second
    assert first != error_fingerprint("ValueError", "different", tool_name="shell")
    detector = ProgressDetector(_policy(max_identical_errors=2))
    state = ProgressState()
    _observe(detector, state, 1, action="A", error=first)
    decision = _observe(detector, state, 2, action="A", error=second)
    assert decision.detector_type == ProgressDetectorType.REPEATED_ERROR


def test_short_cycle_detected_but_non_cycle_is_not() -> None:
    detector = ProgressDetector(_policy(max_cycle_repetitions=3, max_identical_actions=10))
    state = ProgressState()
    decision = None
    for index, action in enumerate("ABABAB", 1):
        decision = _observe(detector, state, index, action=action)
    assert decision is not None
    assert decision.detector_type == ProgressDetectorType.ACTION_CYCLE
    assert decision.cycle_length == 2

    state = ProgressState()
    for index, action in enumerate("ABAC", 1):
        decision = _observe(detector, state, index, action=action)
    assert decision is not None and not decision.detected


def test_stagnation_history_is_bounded_and_new_state_resets() -> None:
    detector = ProgressDetector(_policy(max_stagnant_steps=4, history_limit=12))
    state = ProgressState()
    _observe(detector, state, 1, action="A", progress="plan:pending")
    for index in range(2, 6):
        decision = _observe(detector, state, index, action=f"read-{index}", progress="plan:pending")
    assert decision.detector_type == ProgressDetectorType.STATE_STAGNATION
    assert (
        _observe(detector, state, 6, action="complete", progress="plan:done").decision == "CONTINUE"
    )
    for index in range(7, 30):
        _observe(detector, state, index, action=f"x-{index}", progress=f"s-{index}")
    assert len(state.action_history) <= 12
    assert len(state.processed_operations) <= 12
    assert len(state.progress_history) <= 12


def test_recovery_is_bounded_and_duplicate_observations_are_idempotent() -> None:
    detector = ProgressDetector(_policy(max_identical_actions=2))
    state = ProgressState()
    _observe(detector, state, 1, action="A", progress="S")
    recovery = _observe(detector, state, 2, action="A", progress="S")
    assert recovery.decision == ProgressDecisionType.RECOVER
    duplicate = detector.observe(
        state,
        ProgressObservation(operation_id="op-2", step=2, action_fingerprint="A"),
    )
    assert duplicate.duplicate
    _observe(detector, state, 3, action="A", progress="S")
    terminal = _observe(detector, state, 4, action="A", progress="S")
    assert terminal.decision == ProgressDecisionType.TERMINATE


def test_near_threshold_state_survives_restart_and_disabled_policy_is_inert() -> None:
    detector = ProgressDetector(_policy(max_identical_actions=3))
    state = ProgressState()
    _observe(detector, state, 1, action="A", progress="S")
    _observe(detector, state, 2, action="A", progress="S")
    restored = ProgressState.from_dict(state.to_dict())
    decision = _observe(detector, restored, 3, action="A", progress="S")
    assert decision.decision == ProgressDecisionType.RECOVER

    disabled = ProgressDetector(_policy(enabled=False))
    inert = ProgressState()
    for index in range(10):
        assert _observe(disabled, inert, index, action="A").decision == "CONTINUE"
    assert inert.processed_operations == []


def test_policy_rejects_unbounded_or_inconsistent_thresholds() -> None:
    with pytest.raises(ValueError, match="max_identical_actions"):
        _policy(max_identical_actions=1)
    with pytest.raises(ValueError, match="history_limit is too small"):
        _policy(max_stagnant_steps=20, history_limit=12)


def test_progress_observability_contains_only_counts_and_safe_hashes() -> None:
    state = ProgressState(
        action_repeat_count=4,
        error_repeat_count=2,
        stagnant_steps=5,
        recovery_attempts=1,
        detected=True,
        detector_type="ACTION_CYCLE",
        cycle_length=2,
        last_action_fingerprint="sha256:safe",
    )
    attributes = state.observability_attributes()
    assert attributes["progress.action_repeat_count"] == 4
    assert attributes["progress.detector_type"] == "ACTION_CYCLE"
    assert "arguments" not in attributes
    assert all("secret" not in str(value) for value in attributes.values())


def test_state_and_action_fingerprints_are_canonical_and_checkpointed() -> None:
    assert action_fingerprint("grep", {"path": "src", "query": "Budget"}) == action_fingerprint(
        "grep", {"query": "Budget", "path": "src"}
    )
    assert action_fingerprint("grep", {"query": "A"}) != action_fingerprint("grep", {"query": "B"})
    progress = ProgressState(
        action_history=["A"],
        stagnant_steps=3,
        recovery_attempts=1,
        last_progress_step=4,
    )
    checkpoint = Checkpoint.create(
        thread_id="thread",
        input="task",
        progress_policy=_policy().to_dict(),
        progress_state=progress.to_dict(),
    )
    restored = Checkpoint.from_dict(checkpoint.to_dict())
    assert ProgressState.from_dict(restored.progress_state) == progress
    assert state_fingerprint({"completed": ["a"]}) == state_fingerprint({"completed": ["a"]})


class _LoopLlm:
    model_name = "fake-model"
    provider_name = "fake"
    max_context_window = 10_000

    def __init__(self, *, recover: bool) -> None:
        self.calls = 0
        self.recover = recover
        self.saw_recovery = False

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        self.calls += 1
        self.saw_recovery = any(
            "Runtime recovery signal" in message.content for message in messages
        )
        if self.recover and self.saw_recovery:
            yield {"type": "text_delta", "text": "recovered"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {
            "type": "tool_call_delta",
            "tool_call": {
                "index": 0,
                "id": f"call-{self.calls}",
                "function": {"name": "probe", "arguments": json.dumps({"value": "same"})},
            },
        }
        yield {"type": "message_end", "stop_reason": "tool_use"}


def _loop_runtime(tmp_path, llm: _LoopLlm, *, max_model_calls: int | None = None):
    async def handler(_payload, _context):
        return ToolResult(content="unchanged evidence")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="probe",
            description="probe",
            parameters=object_schema({"value": {"type": "string"}}, required=["value"]),
            handler=handler,
        )
    )
    config = AxiomConfig()
    config.run_budget.max_model_calls = max_model_calls
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=registry,
        system_prompt="test",
        cwd=str(tmp_path),
        config=config,
        store=MemoryCheckpointStore(),
        progress_detector=ProgressDetector(_policy(max_identical_actions=2)),
    )


def test_first_detection_recovers_and_signal_is_model_facing_only(tmp_path) -> None:
    async def scenario() -> None:
        llm = _LoopLlm(recover=True)
        runtime = _loop_runtime(tmp_path, llm)
        state = await runtime.start(thread_id="thread", input="task")
        assert state.status == RunStatus.COMPLETED
        assert state.output_text == "recovered"
        assert llm.saw_recovery
        assert not any("Runtime recovery signal" in message.content for message in state.messages)

    asyncio.run(scenario())


def test_repeated_detection_terminates_no_progress_with_metadata(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _loop_runtime(tmp_path, _LoopLlm(recover=False))
        state = await runtime.start(thread_id="thread", input="task")
        assert state.status == RunStatus.FAILED
        assert state.error is not None and state.error.type == "NO_PROGRESS"
        assert state.error.metadata["detector_type"] == "IDENTICAL_ACTION_LOOP"
        restored = await runtime.store.load(state.run_id)
        assert restored is not None and restored.error == state.error

    asyncio.run(scenario())


def test_budget_exhaustion_during_recovery_remains_authoritative(tmp_path) -> None:
    async def scenario() -> None:
        runtime = _loop_runtime(tmp_path, _LoopLlm(recover=False), max_model_calls=2)
        state = await runtime.start(thread_id="thread", input="task")
        assert state.status == RunStatus.FAILED
        assert state.error is not None
        assert state.error.type == "MODEL_CALL_BUDGET_EXCEEDED"

    asyncio.run(scenario())


def test_badcase_taxonomy_recognizes_no_progress() -> None:
    result = EvaluationRunResult(
        case_id="case",
        run_id="run",
        thread_id="thread",
        turn_id="turn",
        trace_id=None,
        status="FAILED",
        assistant_output="",
        duration_ms=1.0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        tool_calls=[],
        step_count=4,
        error="no progress after bounded recovery",
        error_metadata={
            "type": "NO_PROGRESS",
            "metadata": {"detector_type": "ACTION_CYCLE"},
        },
    )
    kinds, evidence = classify_failure(result)
    assert FailureType.NO_PROGRESS.value in kinds
    assert any("ACTION_CYCLE" in item for item in evidence)


def test_terminal_no_progress_error_round_trips() -> None:
    checkpoint = Checkpoint.create(thread_id="thread", input="task")
    checkpoint.status = RunStatus.FAILED
    checkpoint.error = RunError(
        type="NO_PROGRESS",
        message="stalled",
        step="progress",
        metadata={"detector_type": "STATE_STAGNATION", "recovery_attempts": 1},
    )
    restored = Checkpoint.from_dict(checkpoint.to_dict())
    assert restored.error == checkpoint.error
