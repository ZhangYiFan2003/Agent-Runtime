from __future__ import annotations

import asyncio

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.evaluation import (
    DurableEvaluationExecutor,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunner,
)
from axiom.plan import ExecutionPlan, Task, TaskStatus
from axiom.runtime import (
    COMPLETION_NOT_VERIFIED,
    Checkpoint,
    CompletionCheck,
    CompletionContract,
    CompletionVerificationStatus,
    CompletionVerifier,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RunStatus,
    SQLiteCheckpointStore,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry


class _FinalSequenceLlm:
    provider_name = "completion-test"
    model_name = "completion-model"
    max_context_window = 10_000

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.calls = 0

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        yield {"type": "text_delta", "text": output}
        yield {"type": "usage", "usage": {"input_tokens": 4, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _contract(
    check_type: str,
    *,
    check_id: str = "acceptance",
    max_correction_attempts: int = 1,
    **config,
) -> CompletionContract:
    return CompletionContract(
        checks=(CompletionCheck(id=check_id, type=check_type, config=config),),
        max_correction_attempts=max_correction_attempts,
    )


def _runtime(tmp_path, llm, store, *, observations=None) -> DurableAgentRuntime:
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=ToolRegistry(),
        system_prompt="system",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=store,
        tracer=RunTracer(observations) if observations is not None else None,
    )


def test_no_completion_contract_preserves_existing_completion_behavior(tmp_path):
    async def scenario():
        llm = _FinalSequenceLlm("done")
        state = await _runtime(tmp_path, llm, MemoryCheckpointStore()).start(
            thread_id="thread-no-contract",
            run_id="run-no-contract",
            input="finish",
        )

        assert state.status == RunStatus.COMPLETED
        assert state.completion_verification_attempts == 0
        assert state.completion_verification == {}
        assert llm.calls == 1

    asyncio.run(scenario())


def test_contract_with_durable_tool_output_and_artifact_evidence_verifies(tmp_path):
    async def scenario():
        (tmp_path / "result.txt").write_text("ready", encoding="utf-8")
        store = MemoryCheckpointStore()
        state = Checkpoint.create(thread_id="thread", input="finish", run_id="run-pass")
        state.output_text = "ACCEPTED"
        await store.save_tool_execution(
            ToolExecutionRecord(
                invocation_id="run-pass:call-test",
                run_id="run-pass",
                tool_call_id="call-test",
                tool_name="shell",
                arguments_hash="hash",
                status=ToolExecutionStatus.SUCCEEDED,
                result="12 passed",
            )
        )
        contract = CompletionContract(
            checks=(
                CompletionCheck("status", "run_status"),
                CompletionCheck("tool", "required_tools", config={"tools": ["shell"]}),
                CompletionCheck(
                    "test",
                    "successful_tool",
                    config={"tool": "shell", "result_contains": "passed"},
                ),
                CompletionCheck(
                    "forbidden", "forbidden_tools", config={"tools": ["network_write"]}
                ),
                CompletionCheck(
                    "output", "output_contains", config={"expected": "accepted"}
                ),
                CompletionCheck(
                    "artifact", "artifacts_exist", config={"paths": ["result.txt"]}
                ),
            )
        )

        result = await CompletionVerifier().verify(
            state,
            contract,
            store=store,
            cwd=str(tmp_path),
            attempt=1,
        )

        assert result.status == CompletionVerificationStatus.VERIFIED
        assert result.verified is True
        assert not result.failed_check_ids

    asyncio.run(scenario())


def test_required_tool_missing_is_not_verified(tmp_path):
    async def scenario():
        result = await CompletionVerifier().verify(
            Checkpoint.create(thread_id="thread", input="x", run_id="run-missing-tool"),
            _contract("required_tools", tools=["shell"]),
            store=MemoryCheckpointStore(),
            cwd=str(tmp_path),
            attempt=1,
        )

        assert result.status == CompletionVerificationStatus.NOT_VERIFIED
        assert result.failed_check_ids == ("acceptance",)

    asyncio.run(scenario())


def test_forbidden_tool_usage_is_not_verified(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        await store.save_tool_execution(
            ToolExecutionRecord(
                invocation_id="run-forbidden:call-write",
                run_id="run-forbidden",
                tool_call_id="call-write",
                tool_name="network_write",
                arguments_hash="hash",
                status=ToolExecutionStatus.SUCCEEDED,
            )
        )
        result = await CompletionVerifier().verify(
            Checkpoint.create(thread_id="thread", input="x", run_id="run-forbidden"),
            _contract("forbidden_tools", tools=["network_write"]),
            store=store,
            cwd=str(tmp_path),
            attempt=1,
        )

        assert result.status == CompletionVerificationStatus.NOT_VERIFIED

    asyncio.run(scenario())


def test_sqlite_tool_evidence_is_available_to_verifier_after_restart(tmp_path):
    async def scenario():
        database = tmp_path / "runtime.db"
        first = SQLiteCheckpointStore(database)
        await first.save_tool_execution(
            ToolExecutionRecord(
                invocation_id="run-sqlite:call-test",
                run_id="run-sqlite",
                tool_call_id="call-test",
                tool_name="test",
                arguments_hash="hash",
                status=ToolExecutionStatus.SUCCEEDED,
                result="focused tests passed",
            )
        )
        restarted = SQLiteCheckpointStore(database)

        result = await CompletionVerifier().verify(
            Checkpoint.create(thread_id="thread", input="x", run_id="run-sqlite"),
            _contract("successful_tool", tool="test", result_contains="passed"),
            store=restarted,
            cwd=str(tmp_path),
            attempt=1,
        )

        assert result.status == CompletionVerificationStatus.VERIFIED

    asyncio.run(scenario())


def test_incomplete_plan_task_or_child_run_is_not_verified(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        child = Checkpoint.create(
            thread_id="thread", input="child", run_id="child-run", parent_run_id="plan-run"
        )
        await store.save(child)
        plan = ExecutionPlan(id="plan", goal="goal")
        task = Task(id="task-1", description="verify", status=TaskStatus.PENDING)
        task.child_run_id = child.run_id
        plan.add_task(task)
        state = Checkpoint.create(thread_id="thread", input="goal", run_id="plan-run")
        state.strategy_state["plan"] = plan.to_dict()

        result = await CompletionVerifier().verify(
            state,
            _contract("plan_tasks_completed", task_ids=["task-1"]),
            store=store,
            cwd=str(tmp_path),
            attempt=1,
        )

        assert result.status == CompletionVerificationStatus.NOT_VERIFIED
        assert "incomplete" in result.checks[0].reason

    asyncio.run(scenario())


def test_missing_or_outside_workspace_artifact_is_not_verified(tmp_path):
    async def scenario():
        result = await CompletionVerifier().verify(
            Checkpoint.create(thread_id="thread", input="x", run_id="run-artifact"),
            _contract("artifacts_exist", paths=["missing.txt", "../outside.txt"]),
            store=MemoryCheckpointStore(),
            cwd=str(tmp_path),
            attempt=1,
        )

        assert result.status == CompletionVerificationStatus.NOT_VERIFIED
        assert "missing.txt" in result.checks[0].reason

    asyncio.run(scenario())


def test_model_done_claim_gets_one_corrective_turn_and_can_verify(tmp_path):
    async def scenario():
        llm = _FinalSequenceLlm("done", " acceptance-token ")
        store = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        runtime = _runtime(tmp_path, llm, store, observations=observations)
        state = await runtime.start(
            thread_id="thread-correct",
            run_id="run-correct",
            input="produce the token",
            completion_contract=_contract("output_exact", expected="acceptance-token"),
        )
        metrics = await ObservabilityService(observations).metrics(state.run_id)
        budget = await runtime.budget_snapshot(state)
        persisted = await store.load(state.run_id)

        assert state.status == RunStatus.COMPLETED
        assert state.completion_verification_attempts == 2
        assert state.completion_verification["status"] == "VERIFIED"
        assert any("completion verification feedback" in str(msg.content) for msg in state.messages)
        assert llm.calls == 2 and state.agent_turn == 2 and state.step_index == 2
        assert budget.local_usage.model_calls == 2
        assert budget.local_usage.steps == 2
        assert budget.local_usage.total_tokens == 10
        assert persisted is not None
        assert persisted.completion_verification == state.completion_verification
        assert metrics is not None and metrics.completion_verified is True
        assert metrics.verification_attempts == 2

    asyncio.run(scenario())


def test_repeated_unverified_completion_is_terminal_failure(tmp_path):
    async def scenario():
        llm = _FinalSequenceLlm("done", "still done")
        store = MemoryCheckpointStore()
        state = await _runtime(tmp_path, llm, store).start(
            thread_id="thread-fail",
            run_id="run-fail",
            input="create artifact",
            completion_contract=_contract("artifacts_exist", paths=["required.txt"]),
        )

        assert state.status == RunStatus.FAILED
        assert state.error is not None and state.error.type == COMPLETION_NOT_VERIFIED
        assert state.completion_verification["status"] == "NOT_VERIFIED"
        assert state.completion_verification_attempts == 2
        assert llm.calls == 2

    asyncio.run(scenario())


def test_completion_verification_state_survives_checkpoint_round_trip():
    state = Checkpoint.create(thread_id="thread", input="x", run_id="run-roundtrip")
    state.completion_contract = _contract("output_contains", expected="ok").to_dict()
    state.completion_verification_attempts = 1
    state.completion_verification = {
        "status": "NOT_VERIFIED",
        "verified": False,
        "attempt": 1,
        "failed_check_ids": ["acceptance"],
        "checks": [],
        "error": None,
    }

    loaded = Checkpoint.from_dict(state.to_dict())

    assert loaded.completion_contract == state.completion_contract
    assert loaded.completion_verification == state.completion_verification
    assert loaded.completion_verification_attempts == 1


def test_evaluation_exposes_verified_vs_merely_completed(tmp_path):
    async def scenario():
        checkpoints = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()

        def engine_factory(_case):
            return QueryEngine(
                llm_client=_FinalSequenceLlm("verified-output"),
                tool_registry=ToolRegistry(),
                config=AxiomConfig(),
                cwd=str(tmp_path),
            )

        executor = DurableEvaluationExecutor(
            engine_factory=engine_factory,
            checkpoint_store=checkpoints,
            observability_store=observations,
        )
        dataset = EvaluationDataset(
            name="completion-eval",
            version="1",
            cases=(
                EvaluationCase(
                    id="verified",
                    prompt="x",
                    completion_contract=_contract(
                        "output_contains", expected="verified-output", max_correction_attempts=0
                    ),
                ),
                EvaluationCase(id="compatible", prompt="x"),
            ),
        )

        suite = await EvaluationRunner(executor).run(dataset)

        assert suite.results[0].completion_verified is True
        assert suite.results[0].verification_status == "VERIFIED"
        assert suite.results[1].completion_verified is None
        assert suite.results[1].verification_status == "NOT_APPLICABLE"
        assert all(result.status == RunStatus.COMPLETED for result in suite.results)

    asyncio.run(scenario())
