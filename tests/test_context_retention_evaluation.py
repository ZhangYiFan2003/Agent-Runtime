from __future__ import annotations

import asyncio
from pathlib import Path

from axiom.context import ContextBudgetPolicy, ContextManager
from axiom.evaluation import (
    ContextRetentionCase,
    ContextRetentionCaseResult,
    ContextRetentionDataset,
    ContextRetentionEvaluator,
    ContextRetentionSuiteResult,
    RetainedItemResult,
    compare_context_retention,
    load_context_retention_dataset,
)
from axiom.types import Message


def _manager(**overrides) -> ContextManager:
    values = {
        "model_context_window": 1_200,
        "reserved_output_tokens": 200,
        "high_watermark_ratio": 0.35,
        "target_after_compaction_ratio": 0.2,
        "recent_message_reserve": 2,
        "hard_input_limit": 1_000,
        "max_tool_result_chars": 240,
    }
    values.update(overrides)
    return ContextManager(ContextBudgetPolicy(**values))


def _noise_messages(count: int = 6, *, size: int = 300) -> list[Message]:
    return [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=f"noise-{index} " + (chr(97 + index) * size),
        )
        for index in range(count)
    ]


def _case(
    messages: list[Message],
    must_preserve: dict[str, tuple[str, ...]],
    *,
    objective: str = "current objective",
    must_drop: tuple[str, ...] = (),
    case_id: str = "case",
) -> ContextRetentionCase:
    return ContextRetentionCase(
        id=case_id,
        messages=tuple(messages),
        objective=objective,
        must_preserve=must_preserve,
        must_drop=must_drop,
    )


def test_fixed_context_retention_dataset_has_ten_designed_cases():
    root = Path(__file__).resolve().parents[1]

    dataset = load_context_retention_dataset(
        root / "benchmarks" / "datasets" / "context-retention-v1.json"
    )

    assert dataset.name == "context-retention-v1"
    assert len(dataset.cases) == 10
    assert {
        "old-objective",
        "multiple-constraints",
        "open-vs-completed",
        "critical-failure",
        "oversized-tool-critical-tail",
        "tool-protocol-pair",
        "previous-summary-plus-raw",
        "irrelevant-noise",
        "pinned-recent-state",
        "pending-tool-state",
    } == {case.id for case in dataset.cases}
    result = asyncio.run(ContextRetentionEvaluator(_manager()).evaluate_dataset(dataset))

    assert result.required_items == result.retained_items == 22
    assert result.required_state_retention_rate == 1.0
    assert result.protocol_integrity_rate == 1.0
    assert result.average_compression_ratio == 0.5348


def test_all_declared_state_categories_can_survive_compaction():
    async def scenario():
        call = {
            "id": "call_evidence",
            "type": "function",
            "function": {"name": "test", "arguments": "{}"},
        }
        pending = {
            "id": "call_pending",
            "type": "function",
            "function": {"name": "write", "arguments": "{}"},
        }
        messages = [
            Message(
                role="user",
                content=(
                    "Must preserve constraint-42. Remaining open work: task-open-7. "
                    "Review src/axiom/runtime/durable.py. "
                    + "x" * 400
                ),
            ),
            Message(role="assistant", content="Decision: will use durable evidence. " + "y" * 300),
            Message(role="assistant", content="", tool_calls=[call]),
            Message(
                role="tool",
                content="critical-evidence-99 tests passed",
                tool_call_id="call_evidence",
            ),
            *_noise_messages(4),
            Message(role="user", content="objective-retain-1"),
            Message(role="assistant", content="", tool_calls=[pending]),
        ]
        result = await ContextRetentionEvaluator(_manager()).evaluate_case(
            _case(
                messages,
                {
                    "objective": ("objective-retain-1",),
                    "constraints": ("constraint-42",),
                    "open_tasks": ("task-open-7",),
                    "decisions": ("will use durable evidence",),
                    "artifact_references": ("src/axiom/runtime/durable.py",),
                    "critical_evidence": ("critical-evidence-99",),
                    "pending_protocol_state": ("call_pending",),
                },
                objective="objective-retain-1",
            )
        )

        assert result.passed
        assert result.required_items == result.retained_items == 7
        assert result.protocol_valid

    asyncio.run(scenario())


def test_missing_constraint_is_reported_with_loss_stage():
    async def scenario():
        hidden = "z" * 500 + " LOST_CONSTRAINT_OMEGA"
        messages = [Message(role="assistant", content=hidden), *_noise_messages()]
        result = await ContextRetentionEvaluator(_manager()).evaluate_case(
            _case(messages, {"constraints": ("LOST_CONSTRAINT_OMEGA",)})
        )

        assert result.required_state_retention_rate == 0.0
        assert result.items[0].loss_stage == "lost_after_compaction"

    asyncio.run(scenario())


def test_open_task_loss_is_detected_independently_of_compression():
    async def scenario():
        hidden = "pending " + "z" * 500 + " LOST_OPEN_TASK_SIGMA"
        messages = [Message(role="assistant", content=hidden), *_noise_messages()]
        result = await ContextRetentionEvaluator(_manager()).evaluate_case(
            _case(messages, {"open_tasks": ("LOST_OPEN_TASK_SIGMA",)})
        )

        assert not result.passed
        assert [item.item for item in result.items if not item.retained] == [
            "LOST_OPEN_TASK_SIGMA"
        ]

    asyncio.run(scenario())


def test_irrelevant_noise_may_disappear_without_retention_penalty():
    async def scenario():
        messages = [
            Message(role="user", content="Must preserve REQUIRED_ALPHA."),
            Message(role="assistant", content="q" * 500 + " DISPOSABLE_NOISE"),
            *_noise_messages(),
        ]
        result = await ContextRetentionEvaluator(_manager()).evaluate_case(
            _case(
                messages,
                {"constraints": ("REQUIRED_ALPHA",)},
                must_drop=("DISPOSABLE_NOISE",),
            )
        )

        assert result.required_state_retention_rate == 1.0
        assert result.retained_noise == ()

    asyncio.run(scenario())


def test_orphan_tool_result_fails_protocol_integrity_score():
    async def scenario():
        messages = [
            *_noise_messages(),
            Message(role="user", content="current objective"),
            Message(role="tool", content="orphan", tool_call_id="missing-call"),
        ]
        result = await ContextRetentionEvaluator(_manager()).evaluate_case(
            _case(messages, {"objective": ("current objective",)})
        )

        assert result.required_state_retention_rate == 1.0
        assert not result.protocol_valid
        assert not result.passed

    asyncio.run(scenario())


def test_oversized_tool_projection_retains_configured_tail_evidence():
    async def scenario():
        call = {
            "id": "call_large",
            "type": "function",
            "function": {"name": "shell", "arguments": "{}"},
        }
        messages = [
            Message(role="assistant", content="", tool_calls=[call]),
            Message(
                role="tool",
                content="start " + "x" * 2_000 + " CRITICAL_TAIL_77",
                tool_call_id="call_large",
            ),
            Message(role="user", content="current objective"),
            Message(role="assistant", content="working"),
        ]
        result = await ContextRetentionEvaluator(
            _manager(
                model_context_window=5_000,
                reserved_output_tokens=200,
                high_watermark_ratio=0.9,
                hard_input_limit=4_800,
            )
        ).evaluate_case(
            _case(messages, {"critical_evidence": ("CRITICAL_TAIL_77",)})
        )

        assert result.items[0].retained
        assert result.after_tokens < result.before_tokens
        assert result.protocol_valid

    asyncio.run(scenario())


def test_compression_metrics_use_before_and_after_token_estimates():
    async def scenario():
        result = await ContextRetentionEvaluator(_manager()).evaluate_case(
            _case(
                [*_noise_messages(8), Message(role="user", content="current objective")],
                {"objective": ("current objective",)},
            )
        )

        assert result.before_tokens > result.after_tokens
        assert result.compression_ratio == round(
            result.after_tokens / result.before_tokens, 4
        )

    asyncio.run(scenario())


def _result(
    case_id: str,
    *,
    retention: float,
    ratio: float,
    protocol: bool = True,
) -> ContextRetentionCaseResult:
    retained = int(retention * 2)
    items = tuple(
        RetainedItemResult("constraints", f"item-{index}", index < retained)
        for index in range(2)
    )
    return ContextRetentionCaseResult(
        case_id=case_id,
        before_tokens=100,
        after_tokens=int(100 * ratio),
        compression_ratio=ratio,
        required_items=2,
        retained_items=retained,
        required_state_retention_rate=retention,
        protocol_valid=protocol,
        items=items,
    )


def _suite(*results: ContextRetentionCaseResult) -> ContextRetentionSuiteResult:
    dataset = ContextRetentionDataset(
        name="retention",
        version="1",
        cases=tuple(
            _case(
                [Message(role="user", content="x")],
                {"objective": ("x",)},
                case_id=result.case_id,
            )
            for result in results
        ),
    )
    return ContextRetentionSuiteResult.create(dataset, list(results))


def test_baseline_comparison_makes_retention_loss_a_hard_regression():
    baseline = _suite(_result("case", retention=1.0, ratio=0.6))
    candidate = _suite(_result("case", retention=0.5, ratio=0.3))

    comparison = compare_context_retention(baseline, candidate)

    assert not comparison.passed
    assert "required-state retention decreased" in comparison.hard_regressions[0]


def test_better_compression_cannot_hide_lost_required_state():
    baseline = _suite(_result("case", retention=1.0, ratio=0.8))
    candidate = _suite(_result("case", retention=0.0, ratio=0.1))

    comparison = compare_context_retention(baseline, candidate)

    assert comparison.hard_regressions
    assert comparison.warnings == ()


def test_worse_compression_is_warning_when_retention_and_protocol_hold():
    baseline = _suite(_result("case", retention=1.0, ratio=0.4))
    candidate = _suite(_result("case", retention=1.0, ratio=0.7))

    comparison = compare_context_retention(baseline, candidate)

    assert comparison.passed
    assert "compression ratio worsened" in comparison.warnings[0]


def test_compressed_vs_uncompressed_outcome_probe_and_repeated_runs_are_deterministic():
    async def scenario():
        case = _case(
            [
                Message(role="user", content="Must preserve OUTCOME_KEY."),
                *_noise_messages(),
                Message(role="user", content="current objective"),
            ],
            {"constraints": ("OUTCOME_KEY",)},
        )

        def probe(messages):
            text = " ".join(str(message.content) for message in messages)
            return "success" if "OUTCOME_KEY" in text else "fail"

        evaluator = ContextRetentionEvaluator(_manager())
        first = await evaluator.evaluate_case(case, outcome_probe=probe)
        second = await evaluator.evaluate_case(case, outcome_probe=probe)

        assert first == second
        assert first.outcome_matches is True
        assert first.full_context_outcome == first.compressed_context_outcome == "success"

    asyncio.run(scenario())
