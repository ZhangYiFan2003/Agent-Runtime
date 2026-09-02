from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig, RunBudgetConfig
from axiom.evaluation import (
    DurableEvaluationExecutor,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunResult,
    EvaluationSuiteResult,
    RegressionThresholds,
    evaluate_regression_gate,
)
from axiom.evaluation.badcases import FailureType, classify_failure
from axiom.runtime import (
    BudgetExceededError,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ModelPricing,
    ModelPricingRegistry,
    RunBudgetPolicy,
    RunStatus,
    SQLiteCheckpointStore,
)
from axiom.runtime.budget import BudgetManager
from axiom.runtime.models import Checkpoint
from axiom.runtime.observability_store import ObservabilityService, RunTracer
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolResult, object_schema


class UsageLlm:
    provider_name = "fake"
    model_name = "priced"
    max_context_window = 10_000

    def __init__(self, attempts: list[list[dict] | BaseException]) -> None:
        self.attempts = attempts
        self.calls = 0

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        item = self.attempts[self.calls]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        for event in item:
            if event.get("type") == "raise":
                raise RuntimeError(str(event.get("message") or "provider failed"))
            yield event


def _events(*, input_tokens: int = 10, output_tokens: int = 5, text: str = "ok"):
    return [
        {"type": "text_delta", "text": text},
        {
            "type": "usage",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        },
        {"type": "message_end", "stop_reason": "end_turn"},
    ]


def _config(**limits) -> AxiomConfig:
    config = AxiomConfig()
    pricing = limits.pop("model_pricing", {})
    config.run_budget = RunBudgetConfig(model_pricing=pricing, **limits)
    return config


def _runtime(tmp_path, llm, store, *, config=None, tools=None, retry_policy=None):
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=tools or ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=config or AxiomConfig(),
        store=store,
        retry_policy=retry_policy,
    )


def _state(run_id: str, *, owner: str | None = None) -> Checkpoint:
    return Checkpoint.create(
        thread_id="thread",
        input="task",
        run_id=run_id,
        budget_owner_run_id=owner,
    )


def _manager(store, policy, *, pricing=None):
    registry = ModelPricingRegistry([pricing] if pricing else [])
    return BudgetManager(
        store,
        policy=policy,
        provider="fake",
        model="priced",
        pricing_registry=registry,
    )


def test_policy_validation_and_unbounded_defaults():
    assert RunBudgetPolicy().max_total_tokens is None
    with pytest.raises(ValueError, match="max_steps"):
        RunBudgetPolicy(max_steps=0)
    with pytest.raises(ValueError, match="soft_limit_ratio"):
        RunBudgetPolicy(soft_limit_ratio=1)


def test_known_pricing_and_cached_input_normalization():
    pricing = ModelPricing(
        provider="fake",
        model="priced",
        input_per_million_usd=Decimal("2"),
        output_per_million_usd=Decimal("4"),
        cached_input_per_million_usd=Decimal("0.5"),
    )
    assert pricing.cost(input_tokens=100, output_tokens=50, cached_input_tokens=40) == Decimal(
        "0.00034"
    )


def test_unknown_pricing_is_unknown_and_hard_cost_limit_rejected():
    manager = _manager(MemoryCheckpointStore(), RunBudgetPolicy())
    assert manager.pricing is None
    with pytest.raises(ValueError, match="requires configured pricing"):
        _manager(
            MemoryCheckpointStore(),
            RunBudgetPolicy(max_cost_usd=Decimal("1")),
        )


def test_discrete_limits_soft_pressure_and_error_metadata():
    async def scenario():
        store = MemoryCheckpointStore()
        manager = _manager(store, RunBudgetPolicy(max_steps=2, soft_limit_ratio=0.5))
        state = _state("root")
        await manager.initialize(state)
        first = await manager.consume_step(state, "step-1")
        assert first.soft_limit_reached
        await manager.consume_step(state, "step-2")
        with pytest.raises(BudgetExceededError) as raised:
            await manager.consume_step(state, "step-3")
        assert raised.value.code == "STEP_BUDGET_EXCEEDED"
        assert raised.value.metadata() == {
            "dimension": "steps",
            "limit": 2,
            "used": 2,
            "remaining": "0",
            "run_id": "root",
        }

    asyncio.run(scenario())


def test_actual_usage_is_authoritative_idempotent_and_restart_safe():
    async def scenario():
        store = MemoryCheckpointStore()
        policy = RunBudgetPolicy(max_total_tokens=100)
        manager = _manager(store, policy)
        state = _state("root")
        await manager.initialize(state)
        await manager.reserve_model_call(state, "call-1", estimated_input_tokens=80)
        await manager.complete_model_call(state, "call-1", input_tokens=7, output_tokens=3)
        restarted = _manager(store, policy)
        await restarted.complete_model_call(state, "call-1", input_tokens=99, output_tokens=99)
        snapshot = await restarted.snapshot(state)
        assert snapshot.local_usage.model_calls == 1
        assert snapshot.local_usage.input_tokens == 7
        assert snapshot.local_usage.output_tokens == 3
        assert snapshot.local_usage.total_tokens == 10

    asyncio.run(scenario())


def test_actual_token_and_cost_overshoot_is_accounted_then_fails():
    async def scenario():
        store = MemoryCheckpointStore()
        pricing = ModelPricing(
            provider="fake",
            model="priced",
            input_per_million_usd=Decimal("1"),
            output_per_million_usd=Decimal("2"),
        )
        manager = _manager(
            store,
            RunBudgetPolicy(
                max_total_tokens=5,
                max_cost_usd=Decimal("0.000006"),
            ),
            pricing=pricing,
        )
        state = _state("root")
        await manager.initialize(state)
        await manager.reserve_model_call(state, "call", estimated_input_tokens=1)
        with pytest.raises(BudgetExceededError) as raised:
            await manager.complete_model_call(state, "call", input_tokens=4, output_tokens=2)
        assert raised.value.code in {
            "TOTAL_TOKEN_BUDGET_EXCEEDED",
            "COST_BUDGET_EXCEEDED",
        }
        snapshot = await manager.snapshot(state)
        assert snapshot.local_usage.total_tokens == 6
        assert snapshot.local_usage.cost_usd == Decimal("0.000008")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("policy", "input_tokens", "output_tokens", "code"),
    [
        (RunBudgetPolicy(max_input_tokens=5), 6, 0, "INPUT_TOKEN_BUDGET_EXCEEDED"),
        (RunBudgetPolicy(max_output_tokens=5), 0, 6, "OUTPUT_TOKEN_BUDGET_EXCEEDED"),
        (RunBudgetPolicy(max_total_tokens=5), 3, 3, "TOTAL_TOKEN_BUDGET_EXCEEDED"),
    ],
)
def test_each_token_hard_limit_has_a_structured_error(policy, input_tokens, output_tokens, code):
    async def scenario():
        store = MemoryCheckpointStore()
        manager = _manager(store, policy)
        state = _state(code)
        await manager.initialize(state)
        await manager.reserve_model_call(state, "call")
        with pytest.raises(BudgetExceededError) as raised:
            await manager.complete_model_call(
                state,
                "call",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        assert raised.value.code == code

    asyncio.run(scenario())


def test_elapsed_budget_uses_persisted_run_lifetime():
    async def scenario():
        store = MemoryCheckpointStore()
        manager = _manager(store, RunBudgetPolicy(max_wall_time_seconds=0.001))
        state = _state("old")
        state.created_at = "2000-01-01T00:00:00+00:00"
        await manager.initialize(state)
        with pytest.raises(BudgetExceededError) as raised:
            await manager.consume_step(state, "next")
        assert raised.value.code == "WALL_TIME_BUDGET_EXCEEDED"

    asyncio.run(scenario())


def test_cost_budget_is_enforced_with_known_pricing_and_survives_reload():
    async def scenario():
        store = MemoryCheckpointStore()
        pricing = ModelPricing(
            provider="fake",
            model="priced",
            input_per_million_usd=Decimal("1"),
            output_per_million_usd=Decimal("1"),
        )
        policy = RunBudgetPolicy(max_cost_usd=Decimal("0.000005"))
        manager = _manager(store, policy, pricing=pricing)
        state = _state("cost")
        await manager.initialize(state)
        await manager.reserve_model_call(state, "first")
        await manager.complete_model_call(state, "first", input_tokens=3, output_tokens=2)
        restarted = _manager(store, policy, pricing=pricing)
        snapshot = await restarted.snapshot(state)
        assert snapshot.local_usage.cost_usd == Decimal("0.000005")
        with pytest.raises(BudgetExceededError) as raised:
            await restarted.reserve_model_call(state, "blocked", estimated_input_tokens=1)
        assert raised.value.code == "COST_BUDGET_EXCEEDED"

    asyncio.run(scenario())


def test_parent_child_aggregate_and_concurrent_reservation_are_atomic():
    async def scenario():
        store = MemoryCheckpointStore()
        manager = _manager(store, RunBudgetPolicy(max_model_calls=1))
        root = _state("root")
        child_a = _state("child-a", owner="root")
        child_b = _state("child-b", owner="root")
        await manager.initialize(root)
        await manager.initialize(child_a)
        await manager.initialize(child_b)
        outcomes = await asyncio.gather(
            manager.reserve_model_call(child_a, "a"),
            manager.reserve_model_call(child_b, "b"),
            return_exceptions=True,
        )
        assert sum(isinstance(item, BudgetExceededError) for item in outcomes) == 1
        root_snapshot = await manager.snapshot(root)
        assert root_snapshot.local_usage.model_calls == 0
        assert root_snapshot.aggregate_usage.model_calls == 1
        assert root_snapshot.remaining["model_calls"] == 1.0
        assert root_snapshot.aggregate_remaining["model_calls"] == 0.0

    asyncio.run(scenario())


def test_sqlite_budget_ledger_survives_store_reopen(tmp_path):
    async def scenario():
        database = tmp_path / "runtime.db"
        state = _state("persisted")
        first = _manager(SQLiteCheckpointStore(database), RunBudgetPolicy())
        await first.initialize(state)
        await first.consume_step(state, "step")
        await first.reserve_model_call(state, "model")
        await first.complete_model_call(state, "model", input_tokens=8, output_tokens=2)
        reopened = _manager(SQLiteCheckpointStore(database), RunBudgetPolicy())
        snapshot = await reopened.snapshot(state)
        assert snapshot.local_usage.steps == 1
        assert snapshot.local_usage.model_calls == 1
        assert snapshot.local_usage.total_tokens == 10

    asyncio.run(scenario())


def test_nested_child_usage_aggregates_and_child_local_limit_applies():
    async def scenario():
        store = MemoryCheckpointStore()
        manager = _manager(store, RunBudgetPolicy(max_tool_calls=3))
        root = _state("root")
        child = _state("child", owner="root")
        grandchild = _state("grandchild", owner="root")
        for state in (root, child, grandchild):
            await manager.initialize(state)
        await manager.consume_tool_call(child, "child-tool")
        await manager.consume_tool_call(grandchild, "grandchild-tool")
        snapshot = await manager.snapshot(root)
        assert snapshot.local_usage.tool_calls == 0
        assert snapshot.aggregate_usage.tool_calls == 2

    asyncio.run(scenario())


def test_nested_runtime_child_inherits_root_budget_owner(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        runtime = _runtime(
            tmp_path,
            UsageLlm([_events(), _events(), _events()]),
            store,
        )
        root = await runtime.start(thread_id="thread", input="root", run_id="root")
        child = await runtime.start(
            thread_id="thread",
            input="child",
            run_id="child",
            parent_run_id=root.run_id,
        )
        grandchild = await runtime.start(
            thread_id="thread",
            input="grandchild",
            run_id="grandchild",
            parent_run_id=child.run_id,
        )
        snapshot = await runtime.budget_snapshot(grandchild)
        assert child.budget_owner_run_id == root.run_id
        assert grandchild.budget_owner_run_id == root.run_id
        assert snapshot.aggregate_usage.model_calls == 3

    asyncio.run(scenario())


def test_child_can_have_a_narrower_local_limit_than_parent():
    async def scenario():
        store = MemoryCheckpointStore()
        manager = _manager(store, RunBudgetPolicy(max_model_calls=2))
        root = _state("root")
        child = _state("child", owner="root")
        child.budget_policy = RunBudgetPolicy(max_model_calls=1).to_dict()
        await manager.initialize(root)
        await manager.initialize(child)
        await manager.reserve_model_call(child, "first")
        await manager.complete_model_call(child, "first", input_tokens=1, output_tokens=1)
        with pytest.raises(BudgetExceededError) as raised:
            await manager.reserve_model_call(child, "second")
        assert raised.value.code == "MODEL_CALL_BUDGET_EXCEEDED"
        root_snapshot = await manager.snapshot(root)
        assert root_snapshot.aggregate_usage.model_calls == 1

    asyncio.run(scenario())


def test_child_actual_overshoot_marks_parent_aggregate_hard_limit():
    async def scenario():
        store = MemoryCheckpointStore()
        manager = _manager(store, RunBudgetPolicy(max_total_tokens=5))
        root = _state("root")
        child = _state("child", owner="root")
        child.budget_policy = RunBudgetPolicy(max_total_tokens=10).to_dict()
        await manager.initialize(root)
        await manager.initialize(child)
        await manager.reserve_model_call(child, "call", estimated_input_tokens=1)
        with pytest.raises(BudgetExceededError):
            await manager.complete_model_call(child, "call", input_tokens=4, output_tokens=2)
        root_snapshot = await manager.snapshot(root)
        assert root_snapshot.hard_limit_reached
        assert root_snapshot.exceeded_dimension == "total_tokens"

    asyncio.run(scenario())


def test_durable_runtime_uses_actual_tokens_and_blocks_next_model_call(tmp_path):
    async def scenario():
        llm = UsageLlm([_events(input_tokens=7, output_tokens=3)])
        store = MemoryCheckpointStore()
        runtime = _runtime(
            tmp_path,
            llm,
            store,
            config=_config(max_model_calls=1, max_total_tokens=10),
        )
        completed = await runtime.start(thread_id="thread", input="small", run_id="run")
        assert completed.status == RunStatus.COMPLETED
        snapshot = await runtime.budget_snapshot(completed)
        assert snapshot.local_usage.total_tokens == 10
        with pytest.raises(BudgetExceededError) as raised:
            await runtime.budget_manager.reserve_model_call(completed, "next")
        assert raised.value.code == "MODEL_CALL_BUDGET_EXCEEDED"
        assert llm.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("limits", "expected_code", "expected_calls", "expected_tools"),
    [
        ({"max_model_calls": 1}, "MODEL_CALL_BUDGET_EXCEEDED", 1, 2),
        ({"max_tool_calls": 1}, "TOOL_CALL_BUDGET_EXCEEDED", 1, 1),
        ({"max_steps": 1}, "STEP_BUDGET_EXCEEDED", 1, 0),
    ],
)
def test_hard_limits_block_next_provider_tool_or_step(
    tmp_path, limits, expected_code, expected_calls, expected_tools
):
    async def scenario():
        tool_executions = 0

        async def handler(_payload, _context):
            nonlocal tool_executions
            tool_executions += 1
            return ToolResult(content="done")

        registry = ToolRegistry()
        for name in ("a", "b"):
            registry.register(
                Tool(
                    name=name,
                    description=name,
                    parameters=object_schema({}, required=[]),
                    handler=handler,
                )
            )
        calls = [
            {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": index,
                    "id": f"call_{name}",
                    "function": {"name": name, "arguments": "{}"},
                },
            }
            for index, name in enumerate(("a", "b"))
        ]
        llm = UsageLlm([[*calls, {"type": "message_end", "stop_reason": "tool_use"}]])
        store = MemoryCheckpointStore()
        runtime = _runtime(
            tmp_path,
            llm,
            store,
            config=_config(**limits),
            tools=registry,
        )
        failed = await runtime.start(thread_id="thread", input="task", run_id=expected_code)
        assert failed.status == RunStatus.FAILED
        assert failed.error is not None and failed.error.type == expected_code
        assert llm.calls == expected_calls
        assert tool_executions == expected_tools

    asyncio.run(scenario())


def test_partial_stream_usage_and_retry_calls_are_both_accounted(tmp_path):
    async def scenario():
        from axiom.runtime import RetryPolicy

        llm = UsageLlm(
            [
                [
                    {
                        "type": "usage",
                        "usage": {"input_tokens": 4, "output_tokens": 2},
                    },
                    {"type": "raise", "message": "retry me"},
                ],
                _events(input_tokens=6, output_tokens=3),
            ]
        )
        store = MemoryCheckpointStore()
        runtime = _runtime(
            tmp_path,
            llm,
            store,
            retry_policy=RetryPolicy(max_attempts=2),
        )
        completed = await runtime.start(thread_id="thread", input="task", run_id="retry")
        snapshot = await runtime.budget_snapshot(completed)
        assert completed.status == RunStatus.COMPLETED
        assert llm.calls == 2
        assert snapshot.local_usage.model_calls == 2
        assert snapshot.local_usage.input_tokens == 10
        assert snapshot.local_usage.output_tokens == 5

    asyncio.run(scenario())


def test_provider_failure_without_usage_does_not_invent_tokens(tmp_path):
    async def scenario():
        llm = UsageLlm([RuntimeError("no usage")])
        store = MemoryCheckpointStore()
        runtime = _runtime(tmp_path, llm, store)
        failed = await runtime.start(thread_id="thread", input="task", run_id="failed")
        snapshot = await runtime.budget_snapshot(failed)
        assert failed.status == RunStatus.FAILED
        assert snapshot.local_usage.model_calls == 3
        assert snapshot.local_usage.total_tokens == 0

    asyncio.run(scenario())


def test_observability_exposes_policy_local_aggregate_and_remaining(tmp_path):
    async def scenario():
        llm = UsageLlm([_events(input_tokens=7, output_tokens=3)])
        runtime_store = MemoryCheckpointStore()
        trace_store = MemoryObservabilityStore()
        config = _config(max_model_calls=2, max_total_tokens=20)
        runtime = DurableAgentRuntime(
            llm_client=llm,
            tool_registry=ToolRegistry(),
            system_prompt="test",
            cwd=str(tmp_path),
            config=config,
            store=runtime_store,
            tracer=RunTracer(trace_store),
        )
        await runtime.start(thread_id="thread", input="task", run_id="observed")
        metrics = await ObservabilityService(trace_store).metrics("observed")
        assert metrics is not None
        assert metrics.budget_policy["max_model_calls"] == 2
        assert metrics.budget_usage["model_calls"] == 1
        assert metrics.aggregate_budget_usage["total_tokens"] == 10
        assert metrics.budget_remaining["total_tokens"] == 10.0
        assert metrics.aggregate_budget_remaining["total_tokens"] == 10.0
        assert not metrics.cost_known

    asyncio.run(scenario())


def test_tool_failures_and_retries_count_actual_invocations(tmp_path):
    async def scenario():
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult(content="failed" if attempts == 1 else "done", is_error=attempts == 1)

        tool = Tool(
            name="work",
            description="work",
            parameters=object_schema({}, required=[]),
            handler=handler,
            is_read_only=True,
        )
        registry = ToolRegistry()
        registry.register(tool)
        tool_events = [
            {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_work",
                    "function": {"name": "work", "arguments": json.dumps({})},
                },
            },
            {"type": "message_end", "stop_reason": "tool_use"},
        ]
        llm = UsageLlm([tool_events, _events()])
        store = MemoryCheckpointStore()
        runtime = _runtime(tmp_path, llm, store, tools=registry)
        completed = await runtime.start(thread_id="thread", input="task", run_id="tools")
        snapshot = await runtime.budget_snapshot(completed)
        assert completed.status == RunStatus.COMPLETED
        assert attempts == 2
        assert snapshot.local_usage.tool_calls == 2

    asyncio.run(scenario())


def _eval_result(*, passed: bool, cost: str | None, tokens: int = 10):
    return EvaluationRunResult(
        case_id="case",
        run_id="run",
        thread_id="thread",
        turn_id="turn",
        trace_id="trace",
        status="COMPLETED",
        assistant_output="ok",
        duration_ms=1,
        prompt_tokens=tokens,
        completion_tokens=0,
        total_tokens=tokens,
        tool_calls=[],
        step_count=1,
        passed=passed,
        cost_usd=cost,
        cost_known=cost is not None,
    )


def _suite(results):
    dataset = EvaluationDataset(
        name="suite", version="1", cases=(EvaluationCase(id="case", prompt="task"),)
    )
    return EvaluationSuiteResult.create(dataset, results, started_at="start")


def test_evaluation_cost_aggregates_and_cost_per_success():
    suite = _suite(
        [
            _eval_result(passed=True, cost="0.10"),
            _eval_result(passed=False, cost="0.20"),
        ]
    )
    assert suite.total_cost_usd == "0.3"
    assert suite.avg_cost_usd == "0.15"
    assert suite.successful_trial_cost_usd == "0.1"
    assert suite.cost_per_success == "0.3"
    assert suite.cost_known_trial_count == 2


def test_durable_evaluation_trial_reads_authoritative_aggregate_cost(tmp_path):
    async def scenario():
        config = _config(
            model_pricing={
                "fake/priced": {
                    "input_per_million_usd": "1",
                    "output_per_million_usd": "2",
                }
            }
        )
        engine = QueryEngine(
            llm_client=UsageLlm([_events(input_tokens=7, output_tokens=3)]),
            tool_registry=ToolRegistry(),
            config=config,
            cwd=str(tmp_path),
        )
        executor = DurableEvaluationExecutor(
            engine_factory=lambda _case: engine,
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=MemoryObservabilityStore(),
        )
        result = await executor.execute(EvaluationCase(id="cost", prompt="task"))
        assert result.cost_known
        assert Decimal(result.cost_usd or "0") == Decimal("0.000013")
        assert result.prompt_tokens == 7
        assert result.completion_tokens == 3

    asyncio.run(scenario())


def test_unknown_evaluation_cost_is_not_zero_or_cost_per_success():
    suite = _suite(
        [
            _eval_result(passed=True, cost="0.10"),
            _eval_result(passed=True, cost=None),
        ]
    )
    assert suite.total_cost_usd == "0.1"
    assert suite.cost_known_trial_count == 1
    assert not suite.cost_fully_known
    assert suite.cost_per_success is None


def test_context_budget_error_is_distinct_from_run_token_budget_error():
    context = _eval_result(passed=False, cost=None)
    context.status = "FAILED"
    context.error_metadata = {"type": "CONTEXT_BUDGET_EXCEEDED"}
    run_tokens = _eval_result(passed=False, cost=None)
    run_tokens.status = "FAILED"
    run_tokens.error_metadata = {"type": "TOTAL_TOKEN_BUDGET_EXCEEDED"}
    context_types, _ = classify_failure(context)
    token_types, _ = classify_failure(run_tokens)
    assert FailureType.CONTEXT_BUDGET_EXCEEDED.value in context_types
    assert FailureType.TOKEN_BUDGET_EXCEEDED.value not in context_types
    assert FailureType.TOKEN_BUDGET_EXCEEDED.value in token_types


def test_cost_comparison_warns_by_default_and_only_fails_when_configured():
    baseline = _suite([_eval_result(passed=True, cost="0.10", tokens=20)])
    candidate = _suite([_eval_result(passed=True, cost="0.20", tokens=10)])
    default = evaluate_regression_gate(baseline, candidate)
    strict = evaluate_regression_gate(
        baseline,
        candidate,
        thresholds=RegressionThresholds(max_cost_per_success_increase_ratio=0.5),
    )
    assert default.passed
    assert any("cost per successful" in warning for warning in default.warnings)
    assert not strict.passed
    assert any("cost-per-success regression" in failure for failure in strict.failures)


def test_lower_tokens_with_worse_success_can_worsen_cost_per_success():
    baseline = _suite(
        [
            _eval_result(passed=True, cost="0.10", tokens=20),
            _eval_result(passed=True, cost="0.10", tokens=20),
        ]
    )
    candidate = _suite(
        [
            _eval_result(passed=True, cost="0.10", tokens=10),
            _eval_result(passed=False, cost="0.10", tokens=10),
        ]
    )
    assert candidate.avg_tokens < baseline.avg_tokens
    assert Decimal(candidate.cost_per_success or "0") > Decimal(baseline.cost_per_success or "0")
