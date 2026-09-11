from __future__ import annotations

import json
from dataclasses import replace

import pytest

from axiom.evaluation import EvaluationRunResult
from axiom.rl import (
    AgentTrajectory,
    RewardConfig,
    RewardPipeline,
    TerminationReason,
    TrajectoryBuilder,
    TrajectoryBuildError,
    read_trajectories_jsonl,
    write_trajectories_jsonl,
)
from axiom.runtime import (
    Checkpoint,
    RunStatus,
    Span,
    SpanStatus,
    SpanType,
    ToolExecutionRecord,
    ToolExecutionStatus,
    Trace,
    TraceBundle,
)
from axiom.runtime.models import RunError
from axiom.types import Message

NOW = "2026-01-01T00:00:00+00:00"
LATER = "2026-01-01T00:00:01+00:00"


def _evidence(
    *,
    status: RunStatus = RunStatus.COMPLETED,
    verification: dict | None = None,
    error: RunError | None = None,
    tool: bool = False,
    secret: bool = False,
    parent_run_id: str | None = None,
    child_run_ids: tuple[str, ...] = (),
) -> tuple[Checkpoint, TraceBundle, list[ToolExecutionRecord], tuple[str, ...]]:
    messages = [Message(role="user", content="inspect the repository")]
    records = []
    spans = [
        Span(
            span_id="root",
            trace_id="trace-1",
            span_type=SpanType.AGENT,
            name="run",
            started_at=NOW,
            ended_at=LATER,
            status=SpanStatus.SUCCEEDED,
            attributes={"budget.cost_known": True, "budget.cost_usd": "0.25"},
        ),
        Span(
            span_id="step-0",
            trace_id="trace-1",
            span_type=SpanType.AGENT,
            name="agent.step",
            started_at=NOW,
            ended_at=LATER,
            status=SpanStatus.SUCCEEDED,
            parent_span_id="root",
            attributes={"step_index": 0},
        ),
    ]
    tool_calls = []
    if tool:
        arguments = {"path": "README.md"}
        if secret:
            arguments["api_key"] = "sk-abcdefghijklmnopqrstuvwxyz"
        tool_calls = [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps(arguments)},
            }
        ]
    messages.append(Message(role="assistant", content="done", tool_calls=tool_calls))
    if tool:
        content = "contents"
        if secret:
            content = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
        messages.append(
            Message(role="tool", content=content, name="read_file", tool_call_id="call-1")
        )
        records.append(
            ToolExecutionRecord(
                invocation_id="run-1:call-1",
                run_id="run-1",
                tool_call_id="call-1",
                tool_name="read_file",
                arguments_hash="a" * 64,
                status=ToolExecutionStatus.SUCCEEDED,
                attempt=1,
                result=content,
            )
        )
        spans.append(
            Span(
                span_id="tool-1",
                trace_id="trace-1",
                span_type=SpanType.TOOL,
                name="tool.read_file",
                started_at=NOW,
                ended_at=LATER,
                status=SpanStatus.SUCCEEDED,
                parent_span_id="step-0",
                attributes={"tool_name": "read_file", "retry_count": 1},
            )
        )
    spans.append(
        Span(
            span_id="llm-1",
            trace_id="trace-1",
            span_type=SpanType.LLM,
            name="llm.chat",
            started_at=NOW,
            ended_at=LATER,
            status=SpanStatus.SUCCEEDED,
            parent_span_id="step-0",
            attributes={
                "model": "test-model",
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "latency_ms": 10.0,
                "context.compaction_triggered": False,
                "context.tool_results_projected": 0,
            },
        )
    )
    checkpoint = Checkpoint(
        run_id="run-1",
        thread_id="thread-1",
        turn_id="turn-1",
        input="inspect the repository",
        messages=messages,
        status=status,
        parent_run_id=parent_run_id,
        completion_contract={"checks": [{"type": "run_status"}]} if verification else {},
        completion_verification=verification or {},
        error=error,
    )
    trace = Trace(
        trace_id="trace-1",
        run_id="run-1",
        thread_id="thread-1",
        turn_id="turn-1",
        started_at=NOW,
        ended_at=LATER,
        status=status.value,
    )
    return checkpoint, TraceBundle(trace, spans), records, child_run_ids


def _trajectory(**kwargs):
    checkpoint, trace, records, children = _evidence(**kwargs)
    return TrajectoryBuilder().build(
        checkpoint=checkpoint,
        trace=trace,
        tool_executions=records,
        child_run_ids=children,
        provenance={"runtime_version": "0.1.0"},
    )


def test_successful_react_run_builds_valid_trajectory() -> None:
    trajectory = _trajectory()
    trajectory.validate()
    assert trajectory.outcome.success is True
    assert trajectory.steps[0].action["type"] == "final_response"
    assert trajectory.steps[0].done is True


def test_tool_call_and_result_form_one_environment_transition() -> None:
    trajectory = _trajectory(tool=True)
    assert trajectory.steps[0].tool_actions[0]["tool_name"] == "read_file"
    assert trajectory.steps[0].tool_observations[0]["tool_call_id"] == "call-1"
    assert trajectory.steps[0].observation.messages[0]["role"] == "user"


def test_failed_run_is_still_a_terminated_episode() -> None:
    trajectory = _trajectory(
        status=RunStatus.FAILED,
        error=RunError(type="RuntimeError", message="boom"),
    )
    assert trajectory.outcome.done is True
    assert trajectory.outcome.success is False
    assert trajectory.outcome.termination_reason == TerminationReason.FAILED


def test_parent_child_lineage_is_preserved_without_flattening() -> None:
    trajectory = _trajectory(
        parent_run_id="parent-1", child_run_ids=("child-2", "child-1")
    )
    assert trajectory.parent_run_id == "parent-1"
    assert trajectory.child_run_ids == ("child-1", "child-2")
    assert all(step.parent_run_id == "parent-1" for step in trajectory.steps)


def test_malformed_mismatched_llm_evidence_is_rejected() -> None:
    checkpoint, trace, records, _ = _evidence()
    trace.spans[:] = [span for span in trace.spans if span.span_type != SpanType.LLM]
    with pytest.raises(TrajectoryBuildError, match="LLM spans"):
        TrajectoryBuilder().build(
            checkpoint=checkpoint,
            trace=trace,
            tool_executions=records,
        )


def test_secret_bearing_arguments_and_results_are_redacted(tmp_path) -> None:
    trajectory = _trajectory(tool=True, secret=True)
    encoded = json.dumps(trajectory.to_dict())
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in encoded
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in encoded
    assert encoded.count("[REDACTED]") >= 2

    unsafe_step = replace(trajectory.steps[0], action={"api_key": "unsafe"})
    unsafe = replace(trajectory, steps=(unsafe_step,))
    with pytest.raises(TrajectoryBuildError, match="sensitive export field"):
        write_trajectories_jsonl(tmp_path / "unsafe.jsonl", [unsafe])


def test_verified_completion_produces_configured_outcome_reward() -> None:
    trajectory = _trajectory(verification={"verified": True, "status": "VERIFIED"})
    result = RewardPipeline(RewardConfig(verified_outcome_reward=2.5)).score(trajectory)
    assert result.components["outcome_reward"] == 2.5


def test_not_verified_produces_configured_failure_penalty() -> None:
    trajectory = _trajectory(verification={"verified": False, "status": "NOT_VERIFIED"})
    result = RewardPipeline(
        RewardConfig(unverified_completion_penalty=0.75)
    ).score(trajectory)
    assert result.components["failure_penalty"] == -0.75


def test_tool_success_reward_counts_unique_tools_and_is_capped() -> None:
    trajectory = _trajectory(tool=True)
    config = RewardConfig(successful_tool_reward=0.4, max_tool_reward=0.25)
    result = RewardPipeline(config).score(trajectory)
    assert result.components["tool_reward"] == 0.25


def test_step_token_cost_and_retry_penalties_are_configurable() -> None:
    trajectory = _trajectory(tool=True)
    result = RewardPipeline(
        RewardConfig(
            step_penalty=0.1,
            token_penalty_per_1k=1.0,
            cost_penalty_per_usd=2.0,
            retry_penalty=0.2,
        )
    ).score(trajectory)
    assert result.components["step_penalty"] == -0.1
    assert result.components["token_penalty"] == pytest.approx(-0.12)
    assert result.components["cost_penalty"] == -0.5
    assert result.components["retry_penalty"] == -0.2


def test_reward_decomposition_sums_to_total_and_attaches_to_final_step() -> None:
    rewarded = RewardPipeline(
        RewardConfig(completed_outcome_reward=1.0, step_penalty=0.1)
    ).apply(_trajectory())
    assert rewarded.reward is not None
    assert rewarded.reward.total_reward == pytest.approx(
        sum(rewarded.reward.components.values())
    )
    assert rewarded.steps[-1].reward_components == rewarded.reward.components


def test_reward_configuration_fingerprint_is_deterministic() -> None:
    first = RewardConfig(step_penalty=0.1)
    second = RewardConfig(step_penalty=0.1)
    different = RewardConfig(step_penalty=0.2)
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != different.fingerprint


def test_trajectory_build_is_deterministic_for_identical_evidence() -> None:
    checkpoint, trace, records, _ = _evidence(tool=True)
    builder = TrajectoryBuilder()
    first = builder.build(checkpoint=checkpoint, trace=trace, tool_executions=records)
    second = builder.build(checkpoint=checkpoint, trace=trace, tool_executions=records)
    assert first.to_dict() == second.to_dict()


def test_jsonl_round_trip_preserves_trajectory(tmp_path) -> None:
    path = tmp_path / "rollouts.jsonl"
    original = RewardPipeline().apply(_trajectory())
    write_trajectories_jsonl(path, [original])
    restored = read_trajectories_jsonl(path)
    assert len(restored) == 1
    restored_copy = AgentTrajectory.from_dict(restored[0].to_dict())
    assert restored_copy == original


def test_non_terminal_run_is_rejected() -> None:
    checkpoint, trace, records, _ = _evidence(status=RunStatus.RUNNING)
    with pytest.raises(TrajectoryBuildError, match="terminal Run"):
        TrajectoryBuilder().build(
            checkpoint=checkpoint,
            trace=trace,
            tool_executions=records,
        )


def test_lossy_context_projection_is_marked_not_exact() -> None:
    checkpoint, trace, records, _ = _evidence()
    llm = next(span for span in trace.spans if span.span_type == SpanType.LLM)
    llm.attributes["context.compaction_triggered"] = True
    trajectory = TrajectoryBuilder().build(
        checkpoint=checkpoint,
        trace=trace,
        tool_executions=records,
    )
    assert trajectory.steps[0].observation.exact is False


def test_corrupt_reward_sum_is_rejected() -> None:
    rewarded = RewardPipeline().apply(_trajectory())
    assert rewarded.reward is not None
    corrupt = replace(rewarded.reward, total_reward=999.0)
    with pytest.raises(ValueError, match="do not sum"):
        replace(rewarded, reward=corrupt).validate()


def test_failed_required_evaluation_overrides_verified_runtime_outcome() -> None:
    checkpoint, trace, records, _ = _evidence(
        verification={"verified": True, "status": "VERIFIED"}
    )
    evaluation = EvaluationRunResult(
        case_id="case",
        run_id="run-1",
        thread_id="thread-1",
        turn_id="turn-1",
        trace_id="trace-1",
        status="COMPLETED",
        assistant_output="done",
        duration_ms=1,
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        tool_calls=[],
        step_count=1,
        passed=False,
    )
    trajectory = TrajectoryBuilder().build(
        checkpoint=checkpoint,
        trace=trace,
        tool_executions=records,
        evaluation_result=evaluation,
    )
    reward = RewardPipeline(
        RewardConfig(evaluation_failure_penalty=0.5)
    ).score(trajectory, evaluation_result=evaluation)
    assert trajectory.outcome.success is False
    assert reward.components["outcome_reward"] == 0.0
    assert reward.components["failure_penalty"] == -0.5
