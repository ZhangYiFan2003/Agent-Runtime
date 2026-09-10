from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from axiom.agent import QueryEngine
from axiom.evaluation.attribution import build_evaluation_attribution
from axiom.evaluation.models import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunResult,
    EvaluationSuiteResult,
    ScorerSpec,
    now,
)
from axiom.evaluation.scorers import Scorer, required_scores_passed, score_case, scorer_from_spec
from axiom.runtime.checkpoints import RuntimeStore
from axiom.runtime.completion import CompletionVerificationResult
from axiom.runtime.durable import DurableAgentRuntime, RetryPolicy
from axiom.runtime.models import Checkpoint, RunStatus
from axiom.runtime.multi_agent_strategy import MultiAgentExecutionStrategy
from axiom.runtime.observability import SpanType
from axiom.runtime.observability_store import ObservabilityService, ObservabilityStore, RunTracer

EngineFactory = Callable[[EvaluationCase], QueryEngine | Awaitable[QueryEngine]]
ScorerFactory = Callable[[ScorerSpec], Scorer]


class EvaluationExecutor(Protocol):
    async def execute(self, case: EvaluationCase) -> EvaluationRunResult: ...


class DurableEvaluationExecutor:
    """Runs one evaluation case through the real durable Agent Runtime."""

    def __init__(
        self,
        *,
        engine_factory: EngineFactory,
        checkpoint_store: RuntimeStore,
        observability_store: ObservabilityStore,
        retry_policy: RetryPolicy | None = None,
        execution_strategy: str = "react",
    ) -> None:
        self.engine_factory = engine_factory
        self.checkpoint_store = checkpoint_store
        self.observability_store = observability_store
        self.retry_policy = retry_policy
        self.execution_strategy = execution_strategy
        self.observability = ObservabilityService(observability_store)

    async def execute(self, case: EvaluationCase) -> EvaluationRunResult:
        engine = self.engine_factory(case)
        if inspect.isawaitable(engine):
            engine = await engine
        thread_id = f"eval_thread_{uuid4().hex}"
        turn_id = f"eval_turn_{uuid4().hex}"
        run_id = f"eval_run_{uuid4().hex}"
        tracer = RunTracer(self.observability_store)
        runtime = DurableAgentRuntime(
            llm_client=engine.llm_client,
            tool_registry=engine.tool_registry,
            system_prompt=engine.system_prompt,
            cwd=engine.cwd,
            config=engine.config,
            store=self.checkpoint_store,
            retry_policy=self.retry_policy,
            tracer=tracer,
            execution_strategy=self.execution_strategy,
        )
        state: Checkpoint | None = None
        execution_error: str | None = None
        try:
            state = await asyncio.wait_for(
                runtime.start(
                    thread_id=thread_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    input=case.prompt,
                    completion_contract=case.completion_contract,
                ),
                timeout=case.timeout_seconds,
            )
        except TimeoutError:
            execution_error = f"evaluation timed out after {case.timeout_seconds:g}s"
            state = await self.checkpoint_store.load(run_id)
            if state is not None and not state.finished:
                await tracer.start_run(state, recovered=True)
                try:
                    state = await runtime.cancel(run_id)
                except Exception as exc:  # noqa: BLE001 - preserve the evaluation result
                    execution_error = f"{execution_error}; cancel failed: {_safe_error(exc)}"
                    state = await self.checkpoint_store.load(run_id)
        except Exception as exc:  # noqa: BLE001 - one failed case must not abort a suite
            execution_error = _safe_error(exc)
            state = await self.checkpoint_store.load(run_id)

        bundle = await self.observability.trace(run_id)
        metrics = await self.observability.metrics(run_id)
        child_bundles = []
        child_metrics = []
        child_run_ids: set[str] = set()
        if state is not None and state.execution_strategy == "multi_agent":
            orchestration = MultiAgentExecutionStrategy.load_state(state)
            child_run_ids = (
                {
                    assignment.child_run_id
                    for assignment in orchestration.assignments
                    if assignment.child_run_id
                }
                if orchestration is not None
                else set()
            )
        elif state is not None and state.execution_strategy == "plan_execute":
            from axiom.plan import ExecutionPlan

            raw = state.strategy_state.get("plan")
            plan = ExecutionPlan.from_dict(raw) if isinstance(raw, dict) else None
            if plan is not None:
                child_run_ids = {
                    task.child_run_id
                    for task in [
                        *plan.all_tasks(),
                        *(task for revision in plan.history for task in revision.tasks),
                    ]
                    if task.child_run_id
                }
        for child_run_id in sorted(child_run_ids):
            child_bundle = await self.observability.trace(child_run_id)
            child_metric = await self.observability.metrics(child_run_id)
            if child_bundle is not None:
                child_bundles.append(child_bundle)
            if child_metric is not None:
                child_metrics.append(child_metric)
        tool_calls = (
            [
                str(span.attributes.get("tool_name") or span.name.removeprefix("tool."))
                for trace_bundle in [bundle, *child_bundles]
                for span in trace_bundle.spans
                if span.span_type == SpanType.TOOL
            ]
            if bundle is not None
            else []
        )
        state_error = state.error.message if state is not None and state.error else None
        error_metadata = state.error.to_dict() if state is not None and state.error else {}
        if execution_error and not error_metadata:
            error_metadata = {"type": "EvaluationExecutionError", "message": execution_error}
        actual_status = state.status.value if state is not None else "ERROR"
        if execution_error and actual_status == RunStatus.RUNNING.value:
            actual_status = "ERROR"
        budget = await runtime.budget_snapshot(state) if state is not None else None
        aggregate_usage = budget.aggregate_usage if budget is not None else None
        verification = (
            CompletionVerificationResult.from_dict(state.completion_verification)
            if state is not None and state.completion_verification
            else None
        )
        return EvaluationRunResult(
            case_id=case.id,
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            trace_id=bundle.trace.trace_id if bundle is not None else None,
            status=actual_status,
            assistant_output=state.output_text if state is not None else "",
            duration_ms=metrics.duration_ms if metrics is not None else None,
            prompt_tokens=(
                aggregate_usage.input_tokens
                if aggregate_usage is not None
                else (metrics.prompt_tokens if metrics is not None else 0)
                + sum(item.prompt_tokens for item in child_metrics)
            ),
            completion_tokens=(
                aggregate_usage.output_tokens
                if aggregate_usage is not None
                else (metrics.completion_tokens if metrics is not None else 0)
                + sum(item.completion_tokens for item in child_metrics)
            ),
            total_tokens=(
                aggregate_usage.total_tokens
                if aggregate_usage is not None
                else (metrics.total_tokens if metrics is not None else 0)
                + sum(item.total_tokens for item in child_metrics)
            ),
            tool_calls=tool_calls,
            step_count=(
                aggregate_usage.steps
                if aggregate_usage is not None
                else (metrics.step_count if metrics is not None else 0)
                + sum(item.step_count for item in child_metrics)
            ),
            error=execution_error or state_error,
            error_metadata=error_metadata,
            attribution=build_evaluation_attribution(engine),
            cost_usd=(
                str(aggregate_usage.cost_usd)
                if aggregate_usage is not None and aggregate_usage.cost_known
                else None
            ),
            cost_known=bool(aggregate_usage and aggregate_usage.cost_known),
            completion_verified=verification.verified if verification is not None else None,
            verification_status=(
                verification.status.value if verification is not None else "NOT_APPLICABLE"
            ),
            verification_attempts=(verification.attempt if verification is not None else 0),
            failed_verification_checks=(
                list(verification.failed_check_ids) if verification is not None else []
            ),
        )


class EvaluationRunner:
    def __init__(
        self,
        executor: EvaluationExecutor,
        *,
        scorer_factory: ScorerFactory = scorer_from_spec,
    ) -> None:
        self.executor = executor
        self.scorer_factory = scorer_factory

    async def run(
        self,
        dataset: EvaluationDataset,
        *,
        trials: int = 1,
    ) -> EvaluationSuiteResult:
        if trials < 1:
            raise ValueError("trials must be at least 1")
        configured = {case.id: self._scorers_for(case) for case in dataset.cases}
        started_at = now()
        results: list[EvaluationRunResult] = []
        for case in dataset.cases:
            for trial_index in range(1, trials + 1):
                try:
                    result = await self.executor.execute(case)
                except Exception as exc:  # noqa: BLE001 - custom executor isolation
                    error = _safe_error(exc)
                    result = EvaluationRunResult(
                        case_id=case.id,
                        run_id="",
                        thread_id="",
                        turn_id="",
                        trace_id=None,
                        status="ERROR",
                        assistant_output="",
                        duration_ms=None,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        tool_calls=[],
                        step_count=0,
                        error=error,
                        error_metadata={
                            "type": type(exc).__name__,
                            "message": error,
                        },
                    )
                result.trial_index = trial_index
                result.case_definition = case.to_dict()
                result.attribution = {
                    **result.attribution,
                    "dataset_version": dataset.version,
                }
                result.scores = await score_case(case, result, configured[case.id])
                result.passed = required_scores_passed(result.scores)
                results.append(result)
        suite_attribution = dict(results[0].attribution) if results else {}
        return EvaluationSuiteResult.create(
            dataset,
            results,
            started_at=started_at,
            trials_per_case=trials,
            attribution=suite_attribution,
        )

    def _scorers_for(self, case: EvaluationCase) -> list[Scorer]:
        if case.scorers:
            return [self.scorer_factory(spec) for spec in case.scorers]
        defaults: list[Scorer] = [self.scorer_factory(ScorerSpec(type="run_status"))]
        if case.completion_contract is not None and case.completion_contract.checks:
            defaults.append(self.scorer_factory(ScorerSpec(type="completion_verification")))
        return defaults


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]
