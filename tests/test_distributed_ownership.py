from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from copy import deepcopy
from uuid import uuid4

import pytest

from axiom.config import AxiomConfig, StorageConfig, load_config
from axiom.runtime.capacity import AdmissionRejectedError
from axiom.runtime.durable import DurableAgentRuntime
from axiom.runtime.models import (
    BudgetLedgerRecord,
    Checkpoint,
    Interrupt,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
)
from axiom.runtime.ownership import (
    DistributedRunWorker,
    DistributedWorkerConfigurationError,
    OwnershipLostError,
)
from axiom.runtime.postgres import initialize_postgres_schema
from axiom.runtime.steps import NextAction, StepResult
from axiom.runtime.storage import DurableStorage, create_durable_storage
from axiom.tools import ToolRegistry


class _TakeoverLlm:
    provider_name = "ownership-test"
    model_name = "takeover"
    max_context_window = 10_000

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        yield {"type": "text_delta", "text": "resumed-through-step-loop"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


@pytest.fixture
def postgres_storage(tmp_path) -> Iterator[DurableStorage]:
    dsn = os.environ.get("AXIOM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AXIOM_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    conninfo = pytest.importorskip("psycopg.conninfo")
    schema = f"axiom_ownership_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    isolated_dsn = conninfo.make_conninfo(dsn, options=f"-c search_path={schema}")
    storage = create_durable_storage(
        StorageConfig(backend="postgres", postgres_dsn=isolated_dsn),
        default_sqlite_path=tmp_path / "unused.db",
    )
    try:
        yield storage
    finally:
        storage.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


async def _runnable(storage: DurableStorage, run_id: str = "run-owned") -> Checkpoint:
    state = Checkpoint.create(thread_id="thread-owned", run_id=run_id, input="work")
    await storage.runtime.save(state)
    await storage.runtime.mark_runnable(run_id)
    return state


def _expire(storage: DurableStorage, run_id: str) -> None:
    with storage._close.connection() as conn:
        conn.execute(
            "update runs set lease_until = current_timestamp - interval '1 second' "
            "where run_id = %s",
            (run_id,),
        )


async def _admit(
    storage: DurableStorage, run_id: str, *, limit: int
) -> Checkpoint:
    state = Checkpoint.create(thread_id="thread-capacity", run_id=run_id, input="work")
    await storage.runtime.admit_run(state, limit)
    return state


@pytest.mark.postgres
def test_external_admission_rejects_without_fake_run_and_recovers(postgres_storage):
    first = asyncio.run(_admit(postgres_storage, "admitted-1", limit=1))
    assert first.sequence == 1

    rejected = Checkpoint.create(
        thread_id="thread-capacity", run_id="rejected", input="work"
    )
    with pytest.raises(AdmissionRejectedError) as exc_info:
        asyncio.run(postgres_storage.runtime.admit_run(rejected, 1))
    assert exc_info.value.reason == "QUEUE_CAPACITY_EXCEEDED"
    assert asyncio.run(postgres_storage.runtime.load("rejected")) is None

    first.status = RunStatus.CANCELLED
    asyncio.run(postgres_storage.runtime.save(first))
    later = asyncio.run(_admit(postgres_storage, "admitted-2", limit=1))
    assert later.sequence == 1


@pytest.mark.postgres
def test_runtime_submit_uses_atomic_external_admission(postgres_storage, tmp_path):
    runtime = DurableAgentRuntime(
        llm_client=_TakeoverLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=postgres_storage.runtime,
    )

    state = asyncio.run(
        runtime.submit(
            thread_id="thread-submit",
            run_id="submitted-root",
            turn_id="turn-submit",
            input="work",
            max_queued_runs=1,
        )
    )

    assert state.sequence == 1
    snapshot = asyncio.run(postgres_storage.runtime.capacity_snapshot())
    assert snapshot.queued_runs == 1


@pytest.mark.postgres
def test_distributed_resume_requeues_then_respects_active_capacity(
    postgres_storage, tmp_path
):
    asyncio.run(_admit(postgres_storage, "capacity-holder", limit=2))
    holder = asyncio.run(
        postgres_storage.runtime.claim_run(
            "capacity-holder", "worker-holder", 30, max_active_runs=1
        )
    )
    assert holder is not None
    state = Checkpoint.create(
        thread_id="thread-resume",
        run_id="resume-queued",
        input="resume",
    )
    state.status = RunStatus.INTERRUPTED
    asyncio.run(postgres_storage.runtime.save(state))
    runtime = DurableAgentRuntime(
        llm_client=_TakeoverLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=postgres_storage.runtime,
    )

    resumed = asyncio.run(runtime.queue_resume(state.run_id))

    assert resumed.status == RunStatus.RUNNING
    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                resumed.run_id, "worker-resume", 30, max_active_runs=1
            )
        )
        is None
    )
    assert asyncio.run(postgres_storage.runtime.release_lease(holder, runnable=False))
    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                resumed.run_id, "worker-resume", 30, max_active_runs=1
            )
        )
        is not None
    )


@pytest.mark.postgres
def test_capacity_schema_migrates_v2_with_coordination_only(postgres_storage):
    with postgres_storage._close.connection() as conn:
        conn.execute(
            "update axiom_schema_versions set version = 2 where component = 'runtime'"
        )
    initialize_postgres_schema(postgres_storage._close)
    with postgres_storage._close.connection() as conn:
        version = conn.execute(
            "select version from axiom_schema_versions where component = 'runtime'"
        ).fetchone()[0]
        columns = {
            row[0]
            for row in conn.execute(
                """
                select column_name from information_schema.columns
                where table_schema = current_schema()
                  and table_name = 'runtime_capacity_coordination'
                """
            ).fetchall()
        }

    assert version == 3
    assert columns == {"singleton", "created_at"}


@pytest.mark.postgres
def test_concurrent_external_admission_never_overshoots_limit(postgres_storage):
    async def scenario():
        gate = asyncio.Event()

        async def actor(index: int) -> bool:
            await gate.wait()
            try:
                await _admit(postgres_storage, f"admission-{index}", limit=3)
            except AdmissionRejectedError:
                return False
            return True

        tasks = [asyncio.create_task(actor(index)) for index in range(10)]
        gate.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(scenario())
    snapshot = asyncio.run(
        postgres_storage.runtime.capacity_snapshot(max_queued_runs=3)
    )
    assert sum(results) == 3
    assert snapshot.queued_runs == 3
    assert snapshot.admission_rejections == 7


@pytest.mark.postgres
def test_global_active_capacity_is_atomic_across_claimers(postgres_storage):
    for index in range(5):
        asyncio.run(_admit(postgres_storage, f"active-{index}", limit=10))

    async def scenario():
        gate = asyncio.Event()

        async def actor(index: int):
            await gate.wait()
            return await postgres_storage.runtime.claim_next(
                f"worker-{index}", 30, max_active_runs=2
            )

        tasks = [asyncio.create_task(actor(index)) for index in range(5)]
        gate.set()
        return await asyncio.gather(*tasks)

    owners = [owner for owner in asyncio.run(scenario()) if owner is not None]
    snapshot = asyncio.run(
        postgres_storage.runtime.capacity_snapshot(max_active_runs=2)
    )
    assert len(owners) == 2
    assert snapshot.active_runs == 2
    assert snapshot.queued_runs == 3
    assert snapshot.capacity_blocked_claims == 3


@pytest.mark.postgres
def test_expired_lease_releases_global_active_capacity(postgres_storage):
    asyncio.run(_admit(postgres_storage, "lease-a", limit=2))
    asyncio.run(_admit(postgres_storage, "lease-b", limit=2))
    first = asyncio.run(
        postgres_storage.runtime.claim_next("worker-a", 30, max_active_runs=1)
    )
    assert first is not None
    assert (
        asyncio.run(
            postgres_storage.runtime.claim_next("worker-b", 30, max_active_runs=1)
        )
        is None
    )

    _expire(postgres_storage, first.run_id)
    second = asyncio.run(
        postgres_storage.runtime.claim_next("worker-b", 30, max_active_runs=1)
    )
    assert second is not None
    assert second.run_id in {"lease-a", "lease-b"}


@pytest.mark.postgres
def test_wait_releases_slot_and_resume_recompetes_for_capacity(postgres_storage):
    waiting = asyncio.run(_admit(postgres_storage, "waiting", limit=2))
    asyncio.run(_admit(postgres_storage, "other", limit=2))
    owner = asyncio.run(
        postgres_storage.runtime.claim_run(
            waiting.run_id, "worker-a", 30, max_active_runs=1
        )
    )
    assert owner is not None
    waiting.status = RunStatus.WAITING_CHILD
    asyncio.run(postgres_storage.runtime.save(waiting, ownership=owner))
    assert asyncio.run(postgres_storage.runtime.release_lease(owner, runnable=False))

    other = asyncio.run(
        postgres_storage.runtime.claim_next("worker-b", 30, max_active_runs=1)
    )
    assert other is not None
    waiting.status = RunStatus.RUNNING
    asyncio.run(postgres_storage.runtime.save(waiting))
    asyncio.run(postgres_storage.runtime.mark_runnable(waiting.run_id))
    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                waiting.run_id, "worker-c", 30, max_active_runs=1
            )
        )
        is None
    )
    assert asyncio.run(postgres_storage.runtime.release_lease(other, runnable=False))
    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                waiting.run_id, "worker-c", 30, max_active_runs=1
            )
        )
        is not None
    )


@pytest.mark.postgres
def test_terminal_child_wakes_waiting_parent_without_holding_active_slot(postgres_storage):
    parent = asyncio.run(_admit(postgres_storage, "parent-wait", limit=2))
    parent_owner = asyncio.run(
        postgres_storage.runtime.claim_run(
            parent.run_id, "worker-parent", 30, max_active_runs=1
        )
    )
    assert parent_owner is not None
    parent.status = RunStatus.WAITING_CHILD
    asyncio.run(postgres_storage.runtime.save(parent, ownership=parent_owner))
    assert asyncio.run(
        postgres_storage.runtime.release_lease(parent_owner, runnable=False)
    )

    child = Checkpoint.create(
        thread_id=parent.thread_id,
        run_id="child-active",
        input="child",
        parent_run_id=parent.run_id,
    )
    asyncio.run(postgres_storage.runtime.save(child))
    asyncio.run(postgres_storage.runtime.mark_runnable(child.run_id))
    child_owner = asyncio.run(
        postgres_storage.runtime.claim_run(
            child.run_id, "worker-child", 30, max_active_runs=1
        )
    )
    assert child_owner is not None
    child.status = RunStatus.COMPLETED
    asyncio.run(postgres_storage.runtime.save(child, ownership=child_owner))

    snapshot = asyncio.run(
        postgres_storage.runtime.capacity_snapshot(max_active_runs=1)
    )
    assert snapshot.active_runs == 0
    assert snapshot.queued_runs == 1
    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                parent.run_id, "worker-parent-2", 30, max_active_runs=1
            )
        )
        is not None
    )


def test_capacity_blocked_worker_reuses_bounded_idle_polling():
    class FullStore:
        backend = "postgres"

        def __init__(self):
            self.claims = 0

        async def claim_next(self, _worker_id, _lease_seconds, _max_active_runs=None):
            self.claims += 1
            return None

    store = FullStore()
    sleeps: list[float] = []
    worker = None

    async def controlled_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        worker._stopping = True

    worker = DistributedRunWorker(
        store=store,
        runtime_factory=lambda _ownership: object(),
        max_active_runs=1,
        poll_interval_seconds=0.25,
        sleep=controlled_sleep,
    )
    asyncio.run(worker.run_forever())

    assert store.claims == 1
    assert sleeps == [0.25]


@pytest.mark.postgres
def test_one_worker_claims_unowned_run(postgres_storage):
    asyncio.run(_runnable(postgres_storage))
    owner = asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-a", 30))
    assert owner is not None
    assert owner.worker_id == "worker-a"
    assert owner.fencing_token == 1


@pytest.mark.postgres
def test_second_worker_cannot_claim_active_lease(postgres_storage):
    asyncio.run(_runnable(postgres_storage))
    assert asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-a", 30))
    assert (
        asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-b", 30))
        is None
    )


@pytest.mark.postgres
def test_expired_lease_can_be_taken_over(postgres_storage):
    asyncio.run(_runnable(postgres_storage))
    asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-a", 30))
    _expire(postgres_storage, "run-owned")
    owner = asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-b", 30))
    assert owner is not None and owner.worker_id == "worker-b" and owner.takeover


@pytest.mark.postgres
def test_takeover_increments_fencing_token(postgres_storage):
    asyncio.run(_runnable(postgres_storage))
    first = asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-a", 30))
    assert first is not None
    _expire(postgres_storage, "run-owned")
    second = asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-b", 30))
    assert second is not None and second.fencing_token == first.fencing_token + 1


@pytest.mark.postgres
def test_old_owner_cannot_renew_new_generation(postgres_storage):
    asyncio.run(_runnable(postgres_storage))
    first = asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-a", 30))
    assert first is not None
    _expire(postgres_storage, "run-owned")
    assert asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-b", 30))
    assert asyncio.run(postgres_storage.runtime.renew_lease(first, 30)) is None


@pytest.mark.postgres
def test_old_owner_cannot_checkpoint_after_takeover(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage))
    first = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30))
    assert first is not None
    stale = deepcopy(state)
    _expire(postgres_storage, state.run_id)
    second = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30))
    assert second is not None
    asyncio.run(postgres_storage.runtime.save(state, ownership=second))
    with pytest.raises(OwnershipLostError):
        asyncio.run(postgres_storage.runtime.save(stale, ownership=first))


@pytest.mark.postgres
@pytest.mark.parametrize("action", [NextAction.CONTINUE, NextAction.COMPLETE])
def test_stale_step_transition_is_rejected_after_takeover(
    postgres_storage, tmp_path, action
):
    async def scenario():
        state = await _runnable(postgres_storage, f"run-stale-{action.value.lower()}")
        first = await postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30)
        assert first is not None
        stale = deepcopy(state)
        stale.output_text = "stale-step-evidence"
        _expire(postgres_storage, state.run_id)
        second = await postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30)
        assert second is not None and second.fencing_token == first.fencing_token + 1

        config = AxiomConfig()
        config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
        runtime = DurableAgentRuntime(
            llm_client=_TakeoverLlm(),
            tool_registry=ToolRegistry(),
            system_prompt="test",
            cwd=str(tmp_path),
            config=config,
            store=postgres_storage.runtime,
            ownership=first,
        )
        with pytest.raises(OwnershipLostError):
            await runtime._apply_next_action(
                StepResult.propose(
                    step_index=stale.step_index,
                    run_state=stale,
                    next_action=action,
                )
            )

        current = await postgres_storage.runtime.load(state.run_id)
        assert current is not None
        assert current.status == RunStatus.RUNNING
        assert current.output_text == ""

    asyncio.run(scenario())


@pytest.mark.postgres
def test_two_simultaneous_claimers_have_one_winner(postgres_storage):
    asyncio.run(_runnable(postgres_storage))

    async def race():
        return await asyncio.gather(
            postgres_storage.runtime.claim_run("run-owned", "worker-a", 30),
            postgres_storage.runtime.claim_run("run-owned", "worker-b", 30),
        )

    owners = [owner for owner in asyncio.run(race()) if owner is not None]
    assert len(owners) == 1
    assert owners[0].fencing_token == 1


@pytest.mark.postgres
def test_concurrent_takeover_has_one_winner(postgres_storage):
    asyncio.run(_runnable(postgres_storage))
    asyncio.run(postgres_storage.runtime.claim_run("run-owned", "worker-a", 30))
    _expire(postgres_storage, "run-owned")

    async def race():
        return await asyncio.gather(
            postgres_storage.runtime.claim_run("run-owned", "worker-b", 30),
            postgres_storage.runtime.claim_run("run-owned", "worker-c", 30),
        )

    owners = [owner for owner in asyncio.run(race()) if owner is not None]
    assert len(owners) == 1
    assert owners[0].fencing_token == 2


@pytest.mark.postgres
def test_worker_crash_expiry_makes_run_discoverable(postgres_storage):
    asyncio.run(_runnable(postgres_storage))
    asyncio.run(postgres_storage.runtime.claim_run("run-owned", "dead-worker", 30))
    _expire(postgres_storage, "run-owned")
    recovered = asyncio.run(postgres_storage.runtime.claim_next("recovery-worker", 30))
    assert recovered is not None and recovered.run_id == "run-owned" and recovered.takeover


@pytest.mark.postgres
def test_takeover_resumes_latest_durable_checkpoint(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage))
    first = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30))
    assert first is not None
    state.output_text = "durable-before-crash"
    asyncio.run(postgres_storage.runtime.save(state, ownership=first))
    _expire(postgres_storage, state.run_id)

    class Runtime:
        def __init__(self, ownership):
            self.ownership = ownership

        async def resume(self, run_id):
            current = await postgres_storage.runtime.load(run_id)
            assert current is not None and current.output_text == "durable-before-crash"
            current.status = RunStatus.COMPLETED
            await postgres_storage.runtime.save(current, ownership=self.ownership)
            return current

    worker = DistributedRunWorker(
        store=postgres_storage.runtime,
        runtime_factory=Runtime,
        worker_id="worker-b",
        lease_seconds=30,
        heartbeat_interval_seconds=10,
    )
    result = asyncio.run(worker.run_once())
    assert result is not None
    assert result.status == RunStatus.COMPLETED
    assert result.sequence == 3


@pytest.mark.postgres
def test_takeover_continues_through_unified_step_lifecycle(postgres_storage, tmp_path):
    async def scenario():
        state = await _runnable(postgres_storage, "run-step-takeover")
        first = await postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30)
        assert first is not None
        state.output_text = "durable-before-crash:"
        await postgres_storage.runtime.save(state, ownership=first)
        _expire(postgres_storage, state.run_id)
        second = await postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30)
        assert second is not None and second.fencing_token == first.fencing_token + 1

        config = AxiomConfig()
        config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
        runtime = DurableAgentRuntime(
            llm_client=_TakeoverLlm(),
            tool_registry=ToolRegistry(),
            system_prompt="test",
            cwd=str(tmp_path),
            config=config,
            store=postgres_storage.runtime,
            ownership=second,
        )

        result = await runtime.resume(state.run_id)

        assert result.status == RunStatus.COMPLETED
        assert result.output_text == "durable-before-crash:resumed-through-step-loop"
        stale = deepcopy(result)
        stale.status = RunStatus.FAILED
        with pytest.raises(OwnershipLostError):
            await postgres_storage.runtime.save(stale, ownership=first)

    asyncio.run(scenario())


@pytest.mark.postgres
def test_takeover_reuses_persisted_succeeded_tool_result(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage))
    first = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30))
    assert first is not None
    record = ToolExecutionRecord(
        invocation_id="stable-invocation",
        run_id=state.run_id,
        tool_call_id="tool-call",
        tool_name="write-tool",
        arguments_hash="same-arguments",
        status=ToolExecutionStatus.SUCCEEDED,
        attempt=1,
        result="already-durable",
    )
    asyncio.run(postgres_storage.runtime.save_tool_execution(record, ownership=first))
    _expire(postgres_storage, state.run_id)
    second = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30))
    assert second is not None
    restored = asyncio.run(
        postgres_storage.runtime.load_tool_execution(record.invocation_id)
    )
    assert restored is not None
    assert restored.status == ToolExecutionStatus.SUCCEEDED
    assert restored.result == "already-durable"
    assert restored.attempt == 1


@pytest.mark.postgres
def test_cancelled_run_cannot_be_reclaimed(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage))
    owner = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30))
    assert owner is not None
    state.status = RunStatus.CANCELLED
    asyncio.run(postgres_storage.runtime.save(state))
    assert asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30)) is None


@pytest.mark.postgres
def test_cancel_while_owner_dead_prevents_future_execution(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage))
    asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "dead-worker", 30))
    state.status = RunStatus.CANCELLED
    asyncio.run(postgres_storage.runtime.save(state))
    _expire(postgres_storage, state.run_id)
    assert asyncio.run(postgres_storage.runtime.claim_next("worker-b", 30)) is None


@pytest.mark.postgres
def test_interrupted_run_requires_explicit_resume_before_runnable(postgres_storage):
    state = Checkpoint.create(thread_id="thread-interrupt", run_id="run-interrupt", input="x")
    state.status = RunStatus.INTERRUPTED
    state.interrupt = Interrupt(kind="manual", reason="user requested")
    asyncio.run(postgres_storage.runtime.save(state))
    with pytest.raises(ValueError, match="not eligible"):
        asyncio.run(postgres_storage.runtime.mark_runnable(state.run_id))
    assert asyncio.run(postgres_storage.runtime.claim_next("worker-a", 30)) is None
    state.status = RunStatus.RUNNING
    state.interrupt = None
    asyncio.run(postgres_storage.runtime.save(state))
    asyncio.run(postgres_storage.runtime.mark_runnable(state.run_id))
    assert asyncio.run(postgres_storage.runtime.claim_next("worker-a", 30)) is not None


@pytest.mark.postgres
def test_fencing_mismatch_rejects_state_advance(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage))
    owner = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30))
    assert owner is not None
    wrong = type(owner)(
        run_id=owner.run_id,
        worker_id=owner.worker_id,
        fencing_token=owner.fencing_token + 1,
        lease_until=owner.lease_until,
    )
    with pytest.raises(OwnershipLostError):
        asyncio.run(postgres_storage.runtime.save(state, ownership=wrong))


@pytest.mark.postgres
def test_stale_owner_cannot_mutate_tool_or_budget_truth(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage))
    first = asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30))
    assert first is not None
    _expire(postgres_storage, state.run_id)
    assert asyncio.run(postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30))
    record = ToolExecutionRecord(
        invocation_id="stale-tool",
        run_id=state.run_id,
        tool_call_id="call",
        tool_name="write",
        arguments_hash="hash",
        status=ToolExecutionStatus.PENDING,
    )
    ledger = BudgetLedgerRecord(
        owner_run_id=state.run_id,
        version=0,
        state={"schema_version": 1, "owner_run_id": state.run_id, "runs": {}},
    )
    with pytest.raises(OwnershipLostError):
        asyncio.run(postgres_storage.runtime.save_tool_execution(record, ownership=first))
    with pytest.raises(OwnershipLostError):
        asyncio.run(postgres_storage.runtime.save_budget_ledger(ledger, ownership=first))


@pytest.mark.postgres
def test_ownership_uncertainty_stops_local_progression(postgres_storage, monkeypatch):
    asyncio.run(_runnable(postgres_storage))
    started = asyncio.Event()
    cancelled = asyncio.Event()
    progress = 0

    class Runtime:
        async def resume(self, _run_id):
            nonlocal progress
            started.set()
            try:
                while True:
                    await asyncio.sleep(0)
                    progress += 1
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async def unavailable(_ownership, _lease_seconds):
        await started.wait()
        raise OSError("database unavailable")

    monkeypatch.setattr(postgres_storage.runtime, "renew_lease", unavailable)
    worker = DistributedRunWorker(
        store=postgres_storage.runtime,
        runtime_factory=lambda _ownership: Runtime(),
        worker_id="worker-a",
        lease_seconds=5,
        heartbeat_interval_seconds=0.01,
    )

    async def run():
        with pytest.raises(OwnershipLostError):
            await worker.run_once()
        assert cancelled.is_set()
        stopped_at = progress
        await asyncio.sleep(0)
        assert progress == stopped_at

    asyncio.run(run())


@pytest.mark.postgres
def test_expired_run_deadline_is_not_claimable(postgres_storage):
    state = Checkpoint.create(thread_id="thread-deadline", run_id="run-deadline", input="x")
    state.budget_policy = {"max_wall_time_seconds": 1}
    asyncio.run(postgres_storage.runtime.save(state))
    with postgres_storage._close.connection() as conn:
        conn.execute(
            "update runs set created_at = current_timestamp - interval '2 seconds' "
            "where run_id = %s",
            (state.run_id,),
        )
    with pytest.raises(ValueError, match="not eligible"):
        asyncio.run(postgres_storage.runtime.mark_runnable(state.run_id))


@pytest.mark.postgres
def test_cancelled_ancestor_makes_child_unclaimable(postgres_storage):
    parent = Checkpoint.create(thread_id="thread-tree", run_id="parent", input="parent")
    parent.status = RunStatus.CANCELLED
    child = Checkpoint.create(
        thread_id=parent.thread_id,
        run_id="child",
        input="child",
        parent_run_id=parent.run_id,
    )
    asyncio.run(postgres_storage.runtime.save(parent))
    asyncio.run(postgres_storage.runtime.save(child))
    with pytest.raises(ValueError, match="not eligible"):
        asyncio.run(postgres_storage.runtime.mark_runnable(child.run_id))


@pytest.mark.postgres
def test_root_ownership_authorizes_inline_child_while_parent_waits(postgres_storage):
    parent = asyncio.run(_runnable(postgres_storage, "parent-owned"))
    owner = asyncio.run(
        postgres_storage.runtime.claim_run(parent.run_id, "worker-a", 30)
    )
    assert owner is not None
    parent.status = RunStatus.WAITING_CHILD
    asyncio.run(postgres_storage.runtime.save(parent, ownership=owner))
    renewed = asyncio.run(postgres_storage.runtime.renew_lease(owner, 30))
    assert renewed is not None
    child = Checkpoint.create(
        thread_id=parent.thread_id,
        run_id="child-inline",
        input="child",
        parent_run_id=parent.run_id,
    )
    asyncio.run(postgres_storage.runtime.save(child, ownership=renewed))
    assert asyncio.run(postgres_storage.runtime.load(child.run_id)) is not None


def test_distributed_worker_rejects_sqlite_and_invalid_lease_config(tmp_path):
    storage = create_durable_storage(
        StorageConfig(), default_sqlite_path=tmp_path / "runtime.db"
    )
    with pytest.raises(DistributedWorkerConfigurationError, match="requires PostgreSQL"):
        DistributedRunWorker(store=storage.runtime, runtime_factory=lambda _: object())
    with pytest.raises(ValueError, match="requires storage.backend=postgres"):
        load_config(
            project_root=tmp_path,
            env={"AXIOM_DISTRIBUTED_WORKER": "true", "AXIOM_STORAGE_BACKEND": "sqlite"},
        )
