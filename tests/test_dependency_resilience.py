from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from axiom.config import AxiomConfig, RunBudgetConfig
from axiom.runtime import (
    DEPENDENCY_DEADLINE_EXCEEDED,
    DEPENDENCY_RETRY_EXHAUSTED,
    DEPENDENCY_TIMEOUT,
    BackoffPolicy,
    Checkpoint,
    DependencyFailureCategory,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    OperationDeadline,
    RetryClassifier,
    RetryPolicy,
    RetrySafety,
    RunStatus,
    SpanType,
    SQLiteCheckpointStore,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolRetryState,
)
from axiom.runtime.models import RunError
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolResult, object_schema
from axiom.types import Message


class FinalLlm:
    provider_name = "fake"
    model_name = "fake-model"
    max_context_window = 10_000

    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.calls = 0

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        self.calls += 1
        yield {"type": "text_delta", "text": self.text}
        yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ToolLlm(FinalLlm):
    def __init__(self, tool_name: str) -> None:
        super().__init__()
        self.tool_name = tool_name

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        self.calls += 1
        if not any(message.role == "tool" for message in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": f"call_{self.tool_name}",
                    "function": {
                        "name": self.tool_name,
                        "arguments": json.dumps({"secret": "do-not-log"}),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class SequenceLlm(FinalLlm):
    def __init__(self, failures: list[BaseException]) -> None:
        super().__init__()
        self.failures = failures

    async def chat(self, messages, tools, *, system_prompt):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _config(*, wall_time: float | None = None) -> AxiomConfig:
    config = AxiomConfig(run_budget=RunBudgetConfig(max_wall_time_seconds=wall_time))
    config.policy.hitl_mode = "never"
    config.progress.enabled = False
    return config


def _runtime(
    tmp_path,
    *,
    llm,
    tools: list[Tool] | None = None,
    store=None,
    observations=None,
    policy: RetryPolicy | None = None,
    config: AxiomConfig | None = None,
    retry_sleep=None,
    event_sink=None,
) -> DurableAgentRuntime:
    registry = ToolRegistry()
    registry.register_all(tools or [])
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=registry,
        system_prompt="test",
        cwd=str(tmp_path),
        config=config or _config(),
        store=store or MemoryCheckpointStore(),
        tracer=RunTracer(observations) if observations is not None else None,
        retry_policy=policy
        or RetryPolicy(
            max_attempts=3,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
            jitter_enabled=False,
        ),
        retry_random=lambda: 0.5,
        retry_sleep=retry_sleep,
        event_sink=event_sink,
    )


def _tool(
    name: str,
    handler,
    *,
    read_only: bool = True,
    timeout: float = 1,
    retry_safety: RetrySafety | None = None,
) -> Tool:
    return Tool(
        name=name,
        description=name,
        parameters=object_schema({"secret": {"type": "string"}}),
        handler=handler,
        is_read_only=read_only,
        timeout=timeout,
        retry_safety=retry_safety,
    )


async def _seed_pending_tool_run(
    store,
    *,
    run_id: str,
    tool_name: str,
    status: ToolExecutionStatus,
    attempt: int,
    retry_state: ToolRetryState,
    next_retry_at: str | None = None,
    retry_suppressed_reason: str | None = None,
) -> None:
    state = Checkpoint.create(thread_id="thread", input="resume", run_id=run_id)
    call = {
        "id": f"call_{tool_name}",
        "function": {
            "name": tool_name,
            "arguments": json.dumps({"secret": "do-not-log"}),
        },
    }
    state.messages.append(Message(role="assistant", content="", tool_calls=[call]))
    state.pending_tool_calls = [call]
    await store.save(state)
    payload_hash = hashlib.sha256(
        json.dumps({"secret": "do-not-log"}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await store.save_tool_execution(
        ToolExecutionRecord(
            invocation_id=f"{run_id}:call_{tool_name}",
            run_id=run_id,
            tool_call_id=f"call_{tool_name}",
            tool_name=tool_name,
            arguments_hash=payload_hash,
            status=status,
            attempt=attempt,
            result="recovered" if status == ToolExecutionStatus.SUCCEEDED else "temporary",
            is_error=status != ToolExecutionStatus.SUCCEEDED,
            error=None if status == ToolExecutionStatus.SUCCEEDED else "temporary",
            last_failure_category=DependencyFailureCategory.CONNECTION_ERROR.value,
            retry_state=retry_state,
            retry_suppressed_reason=retry_suppressed_reason,
            next_retry_at=next_retry_at,
        )
    )


def test_operation_deadline_clamps_attempt_timeout_to_run_remainder():
    deadline = OperationDeadline(configured_timeout_seconds=10, remaining_run_seconds=3)
    assert deadline.effective_timeout_seconds == 3


def test_backoff_is_exponential_and_capped():
    policy = BackoffPolicy(base_delay_seconds=0.5, max_delay_seconds=2, jitter_enabled=False)
    assert [policy.delay_seconds(i, random_source=lambda: 1) for i in range(1, 5)] == [
        0.5,
        1,
        2,
        2,
    ]


def test_full_jitter_uses_injected_random_source_deterministically():
    policy = BackoffPolicy(base_delay_seconds=2, max_delay_seconds=8, jitter_enabled=True)
    assert policy.delay_seconds(2, random_source=lambda: 0.25) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError(), DependencyFailureCategory.TIMEOUT),
        (ConnectionError("reset"), DependencyFailureCategory.CONNECTION_ERROR),
    ],
)
def test_retry_classifier_recognizes_transport_failures(error, expected):
    assert RetryClassifier().classify(error) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, DependencyFailureCategory.RATE_LIMITED),
        (503, DependencyFailureCategory.TRANSIENT_SERVER_ERROR),
        (400, DependencyFailureCategory.VALIDATION_ERROR),
        (401, DependencyFailureCategory.AUTH_ERROR),
    ],
)
def test_retry_classifier_uses_provider_status(status, expected):
    error = RuntimeError("provider failed")
    error.status_code = status  # type: ignore[attr-defined]
    assert RetryClassifier().classify(error) == expected


def test_auth_and_policy_failures_are_not_retryable():
    policy = RetryPolicy(max_attempts=3, base_backoff_seconds=0, max_backoff_seconds=0)
    for category in (
        DependencyFailureCategory.AUTH_ERROR,
        DependencyFailureCategory.POLICY_DENIED,
    ):
        decision = policy.decide(
            category=category,
            attempt=1,
            safety=RetrySafety.SAFE,
            error="denied",
            random_source=lambda: 0,
        )
        assert not decision.retry


def test_retry_after_is_respected_and_deadline_can_suppress_retry():
    policy = RetryPolicy(max_attempts=3, base_backoff_seconds=0.5, max_backoff_seconds=4)
    decision = policy.decide(
        category=DependencyFailureCategory.RATE_LIMITED,
        attempt=1,
        safety=RetrySafety.SAFE,
        error="429",
        random_source=lambda: 0,
        retry_after_seconds=4,
        remaining_seconds=2,
    )
    assert not decision.retry
    assert decision.blocked_by_deadline
    assert decision.delay_seconds == 4


def test_tool_attempt_respects_configured_timeout(tmp_path):
    async def scenario():
        async def slow(_payload, _context):
            await asyncio.sleep(1)
            return ToolResult("late")

        store = MemoryCheckpointStore()
        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("slow"),
            tools=[_tool("slow", slow, timeout=0.01)],
            store=store,
            policy=RetryPolicy(max_attempts=1, base_backoff_seconds=0, max_backoff_seconds=0),
        )
        completed = await runtime.start(thread_id="thread", input="slow", run_id="tool-timeout")
        record = await store.load_tool_execution("tool-timeout:call_slow")
        assert completed.status == RunStatus.COMPLETED
        assert record is not None and record.last_error_code == DEPENDENCY_TIMEOUT

    asyncio.run(scenario())


def test_runtime_controls_llm_attempt_timeout(tmp_path):
    class HangingLlm(FinalLlm):
        async def chat(self, _messages, _tools, *, system_prompt):
            del system_prompt
            self.calls += 1
            await asyncio.sleep(1)
            if False:
                yield {}

    async def scenario():
        config = _config()
        config.llm.timeout = 0.01
        runtime = _runtime(
            tmp_path,
            llm=HangingLlm(),
            config=config,
            policy=RetryPolicy(max_attempts=1, base_backoff_seconds=0, max_backoff_seconds=0),
        )
        failed = await runtime.start(thread_id="thread", input="slow", run_id="llm-timeout")
        assert failed.status == RunStatus.FAILED
        assert failed.error is not None and failed.error.type == DEPENDENCY_TIMEOUT

    asyncio.run(scenario())


def test_run_remainder_shortens_tool_timeout(tmp_path):
    async def scenario():
        observed: list[float] = []

        async def inspect_timeout(_payload, context):
            observed.append(float(context.operation_timeout_seconds))
            return ToolResult("ok")

        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("inspect"),
            tools=[_tool("inspect", inspect_timeout, timeout=10)],
            config=_config(wall_time=0.5),
        )
        completed = await runtime.start(thread_id="thread", input="inspect")
        assert completed.status == RunStatus.COMPLETED
        assert 0 < observed[0] <= 0.5

    asyncio.run(scenario())


def test_read_only_tool_retries_but_unsafe_write_does_not(tmp_path):
    async def scenario():
        read_attempts = 0
        write_attempts = 0

        async def read(_payload, _context):
            nonlocal read_attempts
            read_attempts += 1
            if read_attempts == 1:
                raise ConnectionError("temporary")
            return ToolResult("read")

        async def write(_payload, _context):
            nonlocal write_attempts
            write_attempts += 1
            raise TimeoutError()

        read_run = _runtime(tmp_path, llm=ToolLlm("read"), tools=[_tool("read", read)])
        write_store = MemoryCheckpointStore()
        write_run = _runtime(
            tmp_path,
            llm=ToolLlm("write"),
            tools=[_tool("write", write, read_only=False)],
            store=write_store,
        )
        assert (await read_run.start(thread_id="read", input="read")).status == RunStatus.COMPLETED
        write_result = await write_run.start(thread_id="write", input="write")
        assert write_result.status == RunStatus.COMPLETED
        assert read_attempts == 2
        assert write_attempts == 1
        record = await write_store.load_tool_execution(f"{write_result.run_id}:call_write")
        assert record is not None
        assert record.status == ToolExecutionStatus.UNKNOWN
        assert record.retry_suppressed_reason == "unsafe"

    asyncio.run(scenario())


def test_explicitly_idempotent_write_tool_may_retry(tmp_path):
    async def scenario():
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("temporary")
            return ToolResult("created once")

        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("idempotent"),
            tools=[
                _tool(
                    "idempotent",
                    handler,
                    read_only=False,
                    retry_safety=RetrySafety.IDEMPOTENT,
                )
            ],
        )
        await runtime.start(thread_id="thread", input="work")
        assert attempts == 2

    asyncio.run(scenario())


def test_successful_tool_record_is_reused_without_attempt(tmp_path):
    async def scenario():
        executions = 0

        async def handler(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult("new")

        store = MemoryCheckpointStore()
        payload_hash = hashlib.sha256(
            json.dumps({"secret": "do-not-log"}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await store.save_tool_execution(
            ToolExecutionRecord(
                invocation_id="reuse:call_cached",
                run_id="reuse",
                tool_call_id="call_cached",
                tool_name="cached",
                arguments_hash=payload_hash,
                status=ToolExecutionStatus.SUCCEEDED,
                attempt=1,
                result="cached",
            )
        )

        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("cached"),
            tools=[_tool("cached", handler)],
            store=store,
        )
        completed = await runtime.start(
            thread_id="thread",
            input="reuse",
            run_id="reuse",
        )
        assert completed.status == RunStatus.COMPLETED
        assert executions == 0

    asyncio.run(scenario())


def test_llm_retries_consume_model_call_budget_and_budget_error_wins(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        allowed_config = _config()
        allowed_config.run_budget.max_model_calls = 2
        allowed = _runtime(
            tmp_path,
            llm=SequenceLlm([ConnectionError("temporary")]),
            store=store,
            config=allowed_config,
        )
        completed = await allowed.start(thread_id="allowed", input="retry", run_id="allowed")
        snapshot = await allowed.budget_snapshot(completed)
        assert completed.status == RunStatus.COMPLETED
        assert snapshot.local_usage.model_calls == 2

        denied_config = _config()
        denied_config.run_budget.max_model_calls = 1
        denied = _runtime(
            tmp_path,
            llm=SequenceLlm([ConnectionError("temporary")]),
            config=denied_config,
        )
        failed = await denied.start(thread_id="denied", input="retry", run_id="denied")
        assert failed.status == RunStatus.FAILED
        assert failed.error is not None
        assert failed.error.type == "MODEL_CALL_BUDGET_EXCEEDED"

    asyncio.run(scenario())


def test_cancel_during_backoff_stops_retry_loop(tmp_path):
    async def scenario():
        attempts = 0
        sleeping = asyncio.Event()
        never = asyncio.Event()

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            raise ConnectionError("temporary")

        async def blocked_sleep(_delay):
            sleeping.set()
            await never.wait()

        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            policy=RetryPolicy(max_attempts=3, base_backoff_seconds=1, max_backoff_seconds=2),
            retry_sleep=blocked_sleep,
        )
        task = asyncio.create_task(
            runtime.start(thread_id="thread", input="retry", run_id="cancel-backoff")
        )
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        await runtime.cancel("cancel-backoff")
        cancelled = await task
        assert cancelled.status == RunStatus.CANCELLED
        assert attempts == 1

    asyncio.run(scenario())


def test_transient_failure_persists_retry_pending_in_sqlite(tmp_path):
    async def scenario():
        sleeping = asyncio.Event()
        never = asyncio.Event()

        async def handler(_payload, _context):
            raise ConnectionError("temporary")

        async def blocked_sleep(_delay):
            sleeping.set()
            await never.wait()

        db_path = tmp_path / "runtime.db"
        store = SQLiteCheckpointStore(db_path)
        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=store,
            policy=RetryPolicy(
                max_attempts=3,
                base_backoff_seconds=10,
                max_backoff_seconds=10,
                jitter_enabled=False,
            ),
            retry_sleep=blocked_sleep,
        )
        task = asyncio.create_task(
            runtime.start(thread_id="thread", input="retry", run_id="pending")
        )
        await asyncio.wait_for(sleeping.wait(), timeout=1)
        restored = await SQLiteCheckpointStore(db_path).load_tool_execution(
            "pending:call_read"
        )
        assert restored is not None
        assert restored.retry_state == ToolRetryState.RETRY_PENDING
        assert restored.attempt == 1
        assert restored.next_retry_at is not None
        await runtime.cancel("pending")
        assert (await task).status == RunStatus.CANCELLED

    asyncio.run(scenario())


def test_sqlite_restart_before_retry_deadline_does_not_retry_early(tmp_path):
    async def scenario():
        db_path = tmp_path / "runtime.db"
        store = SQLiteCheckpointStore(db_path)
        retry_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        await _seed_pending_tool_run(
            store,
            run_id="before-deadline",
            tool_name="read",
            status=ToolExecutionStatus.FAILED,
            attempt=1,
            retry_state=ToolRetryState.RETRY_PENDING,
            next_retry_at=retry_at,
        )
        attempts = 0
        sleeps: list[float] = []

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult("unexpected")

        async def crash_during_restored_sleep(delay):
            sleeps.append(delay)
            raise RuntimeError("simulated second crash")

        restarted = _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=SQLiteCheckpointStore(db_path),
            retry_sleep=crash_during_restored_sleep,
        )
        with pytest.raises(RuntimeError, match="simulated second crash"):
            await restarted.resume("before-deadline")
        assert attempts == 0
        assert sleeps and sleeps[0] > 20

    asyncio.run(scenario())


def test_sqlite_restart_after_retry_deadline_uses_remaining_attempt(tmp_path):
    async def scenario():
        db_path = tmp_path / "runtime.db"
        await _seed_pending_tool_run(
            SQLiteCheckpointStore(db_path),
            run_id="after-deadline",
            tool_name="read",
            status=ToolExecutionStatus.FAILED,
            attempt=2,
            retry_state=ToolRetryState.RETRY_PENDING,
            next_retry_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult("recovered")

        store = SQLiteCheckpointStore(db_path)
        completed = await _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=store,
        ).resume("after-deadline")
        record = await store.load_tool_execution("after-deadline:call_read")
        assert completed.status == RunStatus.COMPLETED
        assert attempts == 1
        assert record is not None and record.attempt == 3
        assert record.retry_state == ToolRetryState.NONE

    asyncio.run(scenario())


def test_sqlite_restart_keeps_retry_exhausted_terminal(tmp_path):
    async def scenario():
        db_path = tmp_path / "runtime.db"
        await _seed_pending_tool_run(
            SQLiteCheckpointStore(db_path),
            run_id="exhausted",
            tool_name="read",
            status=ToolExecutionStatus.FAILED,
            attempt=3,
            retry_state=ToolRetryState.RETRY_EXHAUSTED,
        )
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult("unexpected")

        store = SQLiteCheckpointStore(db_path)
        completed = await _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=store,
        ).resume("exhausted")
        record = await store.load_tool_execution("exhausted:call_read")
        assert completed.status == RunStatus.COMPLETED
        assert attempts == 0
        assert record is not None
        assert record.retry_state == ToolRetryState.RETRY_EXHAUSTED

    asyncio.run(scenario())


def test_sqlite_restart_keeps_unsafe_unknown_retry_suppressed(tmp_path):
    async def scenario():
        db_path = tmp_path / "runtime.db"
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            raise TimeoutError("ambiguous timeout")

        def crash_after_unknown(event_type, _payload):
            if event_type == "tool.failed":
                raise RuntimeError("simulated crash after UNKNOWN persistence")

        first = _runtime(
            tmp_path,
            llm=ToolLlm("write"),
            tools=[_tool("write", handler, read_only=False)],
            store=SQLiteCheckpointStore(db_path),
            event_sink=crash_after_unknown,
        )
        with pytest.raises(RuntimeError, match="simulated crash"):
            await first.start(thread_id="thread", input="write", run_id="unsafe-unknown")
        persisted = await SQLiteCheckpointStore(db_path).load_tool_execution(
            "unsafe-unknown:call_write"
        )
        assert persisted is not None
        assert persisted.status == ToolExecutionStatus.UNKNOWN
        assert persisted.retry_state == ToolRetryState.RETRY_SUPPRESSED
        assert persisted.retry_suppressed_reason == "unsafe"

        store = SQLiteCheckpointStore(db_path)
        completed = await _runtime(
            tmp_path,
            llm=ToolLlm("write"),
            tools=[_tool("write", handler, read_only=False)],
            store=store,
        ).resume("unsafe-unknown")
        record = await store.load_tool_execution("unsafe-unknown:call_write")
        assert completed.status == RunStatus.COMPLETED
        assert attempts == 1
        assert record is not None and record.status == ToolExecutionStatus.UNKNOWN
        assert record.retry_state == ToolRetryState.RETRY_SUPPRESSED
        assert record.retry_suppressed_reason == "unsafe"

    asyncio.run(scenario())


def test_sqlite_restart_reuses_success_without_attempt_or_budget_charge(tmp_path):
    async def scenario():
        db_path = tmp_path / "runtime.db"
        await _seed_pending_tool_run(
            SQLiteCheckpointStore(db_path),
            run_id="reuse-success",
            tool_name="read",
            status=ToolExecutionStatus.SUCCEEDED,
            attempt=1,
            retry_state=ToolRetryState.NONE,
        )
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult("unexpected")

        store = SQLiteCheckpointStore(db_path)
        restarted = _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=store,
        )
        completed = await restarted.resume("reuse-success")
        budget = await restarted.budget_snapshot(completed)
        record = await store.load_tool_execution("reuse-success:call_read")
        assert completed.status == RunStatus.COMPLETED
        assert attempts == 0
        assert record is not None and record.attempt == 1
        assert budget.local_usage.tool_calls == 0

    asyncio.run(scenario())


def test_cancelled_run_blocks_persisted_pending_retry_after_restart(tmp_path):
    async def scenario():
        db_path = tmp_path / "runtime.db"
        store = SQLiteCheckpointStore(db_path)
        await _seed_pending_tool_run(
            store,
            run_id="cancelled-pending",
            tool_name="read",
            status=ToolExecutionStatus.FAILED,
            attempt=1,
            retry_state=ToolRetryState.RETRY_PENDING,
            next_retry_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        )
        state = await store.load("cancelled-pending")
        assert state is not None
        state.status = RunStatus.CANCELLED
        await store.save(state)
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult("unexpected")

        restarted = _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=SQLiteCheckpointStore(db_path),
        )
        with pytest.raises(ValueError, match="cancelled run cannot be resumed"):
            await restarted.resume("cancelled-pending")
        assert attempts == 0

    asyncio.run(scenario())


def test_outer_deadline_blocks_restored_retry(tmp_path):
    async def scenario():
        db_path = tmp_path / "runtime.db"
        store = SQLiteCheckpointStore(db_path)
        await _seed_pending_tool_run(
            store,
            run_id="expired-deadline",
            tool_name="read",
            status=ToolExecutionStatus.FAILED,
            attempt=1,
            retry_state=ToolRetryState.RETRY_PENDING,
            next_retry_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        )
        state = await store.load("expired-deadline")
        assert state is not None
        state.created_at = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        await store.save(state)
        attempts = 0

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult("unexpected")

        completed = await _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=SQLiteCheckpointStore(db_path),
            config=_config(wall_time=1),
        ).resume("expired-deadline")
        record = await SQLiteCheckpointStore(db_path).load_tool_execution(
            "expired-deadline:call_read"
        )
        assert completed.status == RunStatus.FAILED
        assert attempts == 0
        assert record is not None
        assert record.retry_state == ToolRetryState.RETRY_PENDING

    asyncio.run(scenario())


def test_sqlite_additive_migration_safely_suppresses_legacy_unknown(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table tool_executions (
                invocation_id text primary key,
                run_id text not null,
                tool_call_id text not null,
                tool_name text not null,
                arguments_hash text not null,
                status text not null,
                attempt integer not null,
                result text,
                is_error integer not null,
                error text,
                started_at text,
                completed_at text,
                updated_at text not null
            )
            """
        )
        conn.execute(
            """
            insert into tool_executions values (
                'legacy:call_write', 'legacy', 'call_write', 'write', 'hash',
                'UNKNOWN', 1, null, 1, 'timeout', null, null, '2026-01-01T00:00:00+00:00'
            )
            """
        )
    store = SQLiteCheckpointStore(db_path)
    record = asyncio.run(store.load_tool_execution("legacy:call_write"))
    assert record is not None
    assert record.retry_state == ToolRetryState.RETRY_SUPPRESSED
    assert record.retry_suppressed_reason == "unknown_outcome"


def test_restart_preserves_tool_attempt_count(tmp_path):
    async def scenario():
        attempts = 0
        store = MemoryCheckpointStore()
        state = Checkpoint.create(thread_id="thread", input="resume", run_id="restart")
        state.messages.append(
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    {
                        "id": "call_read",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"secret": "do-not-log"}),
                        },
                    }
                ],
            )
        )
        state.pending_tool_calls = list(state.messages[-1].tool_calls or [])
        await store.save(state)
        payload_hash = hashlib.sha256(
            json.dumps({"secret": "do-not-log"}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        await store.save_tool_execution(
            ToolExecutionRecord(
                invocation_id="restart:call_read",
                run_id="restart",
                tool_call_id="call_read",
                tool_name="read",
                arguments_hash=payload_hash,
                status=ToolExecutionStatus.FAILED,
                attempt=2,
                result="temporary",
                is_error=True,
                error="temporary",
                last_failure_category=DependencyFailureCategory.CONNECTION_ERROR.value,
                next_retry_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            )
        )

        async def handler(_payload, _context):
            nonlocal attempts
            attempts += 1
            return ToolResult("recovered")

        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=store,
        )
        completed = await runtime.resume("restart")
        record = await store.load_tool_execution("restart:call_read")
        assert completed.status == RunStatus.COMPLETED
        assert attempts == 1
        assert record is not None and record.attempt == 3

    asyncio.run(scenario())


def test_restart_preserves_llm_retry_allowance(tmp_path):
    async def scenario():
        store = MemoryCheckpointStore()
        state = Checkpoint.create(thread_id="thread", input="resume", run_id="llm-restart")
        state.error = RunError(
            type="ConnectionError",
            message="temporary",
            step="llm",
            metadata={
                "dependency_type": "model",
                "failure_category": "connection_error",
                "attempt_count": 2,
                "retryable": True,
                "retry_in_progress": True,
                "next_retry_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            },
        )
        await store.save(state)
        llm = SequenceLlm([ConnectionError("still offline")])
        runtime = _runtime(tmp_path, llm=llm, store=store)
        failed = await runtime.resume("llm-restart")
        assert failed.status == RunStatus.FAILED
        assert failed.error is not None
        assert failed.error.type == DEPENDENCY_RETRY_EXHAUSTED
        assert failed.error.metadata["attempt_count"] == 3
        assert llm.calls == 1

    asyncio.run(scenario())


def test_retry_trace_metrics_and_structured_exhaustion_do_not_leak_arguments(tmp_path):
    async def scenario():
        async def handler(_payload, _context):
            raise ConnectionError("offline")

        store = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        runtime = _runtime(
            tmp_path,
            llm=ToolLlm("read"),
            tools=[_tool("read", handler)],
            store=store,
            observations=observations,
            policy=RetryPolicy(
                max_attempts=2,
                base_backoff_seconds=0,
                max_backoff_seconds=0,
                jitter_enabled=False,
            ),
        )
        completed = await runtime.start(thread_id="thread", input="retry", run_id="evidence")
        record = await store.load_tool_execution("evidence:call_read")
        bundle = await ObservabilityService(observations).trace(completed.run_id)
        metrics = await ObservabilityService(observations).metrics(completed.run_id)
        assert record is not None and record.retry_exhausted
        assert record.last_error_code == DEPENDENCY_RETRY_EXHAUSTED
        assert bundle is not None and metrics is not None
        tool_span = next(span for span in bundle.spans if span.span_type == SpanType.TOOL)
        assert tool_span.attributes["dependency.retry_exhausted"] is True
        assert metrics.retried_tool_calls == 1
        assert metrics.retry_exhausted_count == 1
        assert "do-not-log" not in json.dumps(tool_span.attributes)

    asyncio.run(scenario())


def test_deadline_error_code_is_distinct_from_retry_exhaustion():
    policy = RetryPolicy(
        max_attempts=3,
        base_backoff_seconds=4,
        max_backoff_seconds=4,
        jitter_enabled=False,
    )
    decision = policy.decide(
        category=DependencyFailureCategory.CONNECTION_ERROR,
        attempt=1,
        safety=RetrySafety.SAFE,
        error="offline",
        random_source=lambda: 1,
        remaining_seconds=2,
    )
    assert decision.blocked_by_deadline
    assert DEPENDENCY_DEADLINE_EXCEEDED != DEPENDENCY_RETRY_EXHAUSTED
