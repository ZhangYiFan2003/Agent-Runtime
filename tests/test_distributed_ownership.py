from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Iterator
from copy import deepcopy
from io import BytesIO
from uuid import uuid4

import pytest

import axiom.runtime.postgres as postgres_module
from axiom.agent import QueryEngine
from axiom.config import AxiomConfig, StorageConfig, load_config
from axiom.runtime.api import RuntimeApiServer, RuntimeTurnContext
from axiom.runtime.capacity import AdmissionRejectedError
from axiom.runtime.control_plane import ApiError
from axiom.runtime.durable import DurableAgentRuntime
from axiom.runtime.models import (
    BudgetLedgerRecord,
    Checkpoint,
    Interrupt,
    RunError,
    RunStatus,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolRetryState,
)
from axiom.runtime.ownership import (
    DistributedRunWorker,
    DistributedWorkerConfigurationError,
    OwnershipLostError,
)
from axiom.runtime.postgres import RUN_DELIVERY_EXHAUSTED, initialize_postgres_schema
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


class _ApiRequest:
    def __init__(
        self,
        path: str,
        *,
        key: str,
        payload: dict | None = None,
        method: str = "POST",
    ):
        body = json.dumps(payload or {}).encode()
        self.command = method
        self.path = path
        self.headers = {
            "content-length": str(len(body)),
            "x-api-key": "test-key",
            "Idempotency-Key": key,
        }
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status = 0

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _name: str, _value: str) -> None:
        return

    def end_headers(self) -> None:
        return

    def json(self):
        return json.loads(self.wfile.getvalue())


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


async def _exhaust(storage: DurableStorage, run_id: str, *, attempts: int = 1):
    await _runnable(storage, run_id)
    owner = None
    for index in range(attempts):
        owner = await storage.runtime.claim_run(
            run_id,
            f"worker-exhaust-{index}",
            30,
            max_run_delivery_attempts=attempts,
        )
        assert owner is not None
        _expire(storage, run_id)
    assert (
        await storage.runtime.claim_run(
            run_id,
            "worker-exhaust-final",
            30,
            max_run_delivery_attempts=attempts,
        )
        is None
    )
    return owner, await storage.runtime.load(run_id)


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
            row[0].decode() if isinstance(row[0], bytes) else row[0]
            for row in conn.execute(
                """
                select column_name from information_schema.columns
                where table_schema = current_schema()
                  and table_name = 'runtime_capacity_coordination'
                """
            ).fetchall()
        }

    assert version == 5
    assert columns == {"singleton", "created_at"}


@pytest.mark.postgres
def test_schema_v3_additively_migrates_delivery_metadata(postgres_storage):
    with postgres_storage._close.connection() as conn:
        conn.execute("drop index if exists idx_runs_failure_queue")
        for column in (
            "failure_queued_at",
            "last_delivery_worker_id",
            "last_delivery_fencing_token",
            "last_delivery_failed_at",
            "last_delivery_failure",
            "delivery_attempt",
        ):
            conn.execute(f"alter table runs drop column {column}")
        conn.execute(
            "update axiom_schema_versions set version = 3 where component = 'runtime'"
        )

    initialize_postgres_schema(postgres_storage._close)

    with postgres_storage._close.connection() as conn:
        version = conn.execute(
            "select version from axiom_schema_versions where component = 'runtime'"
        ).fetchone()[0]
        columns = {
            row[0].decode() if isinstance(row[0], bytes) else row[0]
            for row in conn.execute(
                """
                select column_name from information_schema.columns
                where table_schema = current_schema() and table_name = 'runs'
                """
            ).fetchall()
        }
    assert version == 5
    assert {
        "delivery_attempt",
        "failure_queued_at",
        "last_delivery_failure",
        "last_delivery_failed_at",
        "last_delivery_worker_id",
        "last_delivery_fencing_token",
    } <= columns


@pytest.mark.postgres
def test_schema_v4_additively_migrates_submission_idempotency(postgres_storage):
    with postgres_storage._close.connection() as conn:
        conn.execute("drop table submission_idempotency")
        conn.execute(
            "update axiom_schema_versions set version = 4 where component = 'runtime'"
        )

    initialize_postgres_schema(postgres_storage._close)

    with postgres_storage._close.connection() as conn:
        version = conn.execute(
            "select version from axiom_schema_versions where component = 'runtime'"
        ).fetchone()[0]
        columns = {
            row[0].decode() if isinstance(row[0], bytes) else row[0]
            for row in conn.execute(
                """
                select column_name from information_schema.columns
                where table_schema = current_schema()
                  and table_name = 'submission_idempotency'
                """
            ).fetchall()
        }
    assert version == 5
    assert columns == {
        "thread_id",
        "idempotency_key",
        "request_fingerprint",
        "run_id",
        "created_at",
    }


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
    resumed_owner = asyncio.run(
        postgres_storage.runtime.claim_run(
            waiting.run_id, "worker-c", 30, max_active_runs=1
        )
    )
    assert resumed_owner is not None and resumed_owner.delivery_attempt == 1


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


def test_claim_database_failure_does_not_start_execution():
    started = False

    class UnavailableStore:
        backend = "postgres"

        async def claim_next(self, *_args):
            raise ConnectionError("database unavailable")

    def runtime_factory(_ownership):
        nonlocal started
        started = True
        raise AssertionError("execution must not start without a durable claim")

    worker = DistributedRunWorker(
        store=UnavailableStore(),
        runtime_factory=runtime_factory,
    )

    with pytest.raises(ConnectionError, match="database unavailable"):
        asyncio.run(worker.run_once())
    assert started is False


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
def test_delivery_attempt_counts_initial_and_expired_takeover_only(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage, "run-delivery-count"))
    first = asyncio.run(
        postgres_storage.runtime.claim_run(
            state.run_id, "worker-a", 30, max_run_delivery_attempts=3
        )
    )
    assert first is not None and first.delivery_attempt == 1 and not first.takeover

    assert asyncio.run(postgres_storage.runtime.release_lease(first, runnable=True))
    resumed = asyncio.run(
        postgres_storage.runtime.claim_run(
            state.run_id, "worker-a", 30, max_run_delivery_attempts=3
        )
    )
    assert resumed is not None and resumed.delivery_attempt == 1 and not resumed.takeover

    _expire(postgres_storage, state.run_id)
    redelivered = asyncio.run(
        postgres_storage.runtime.claim_run(
            state.run_id, "worker-b", 30, max_run_delivery_attempts=3
        )
    )
    assert redelivered is not None
    assert redelivered.delivery_attempt == 2 and redelivered.takeover
    loaded = asyncio.run(postgres_storage.runtime.load(state.run_id))
    assert loaded is not None and loaded.delivery_attempt == 2
    assert loaded.last_delivery_failure == "lease_expired"
    assert loaded.last_delivery_worker_id == "worker-a"
    assert loaded.last_delivery_fencing_token == resumed.fencing_token


@pytest.mark.postgres
def test_approval_resume_does_not_consume_redelivery_allowance(
    postgres_storage, tmp_path
):
    state = asyncio.run(_runnable(postgres_storage, "run-approval-resume"))
    first = asyncio.run(
        postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30, None, 2)
    )
    assert first is not None and first.delivery_attempt == 1
    state.status = RunStatus.WAITING_APPROVAL
    state.interrupt = Interrupt(
        kind="tool_approval",
        reason="approval required",
        invocation_id="approval-call",
    )
    asyncio.run(postgres_storage.runtime.save(state, ownership=first))
    assert asyncio.run(postgres_storage.runtime.release_lease(first, runnable=False))

    config = AxiomConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    runtime = DurableAgentRuntime(
        llm_client=_TakeoverLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=config,
        store=postgres_storage.runtime,
    )
    asyncio.run(runtime.queue_resume(state.run_id, decision="approve"))
    resumed = asyncio.run(
        postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30, None, 2)
    )
    assert resumed is not None and resumed.delivery_attempt == 1


@pytest.mark.postgres
@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.FAILED])
def test_authoritative_terminal_outcome_is_not_redelivered(postgres_storage, terminal):
    state = asyncio.run(_runnable(postgres_storage, f"run-{terminal.value.lower()}"))
    owner = asyncio.run(
        postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30, None, 1)
    )
    assert owner is not None
    state.status = terminal
    state.error = (
        RunError(type="DETERMINISTIC_FAILURE", message="task outcome")
        if terminal == RunStatus.FAILED
        else None
    )
    asyncio.run(postgres_storage.runtime.save(state, ownership=owner))

    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30, None, 1)
        )
        is None
    )
    loaded = asyncio.run(postgres_storage.runtime.load(state.run_id))
    assert loaded is not None and loaded.failure_queued_at is None


@pytest.mark.postgres
def test_parent_and_child_delivery_attempts_are_independent(postgres_storage):
    parent = asyncio.run(_runnable(postgres_storage, "delivery-parent"))
    child = Checkpoint.create(
        thread_id=parent.thread_id,
        run_id="delivery-child",
        input="child",
        parent_run_id=parent.run_id,
    )
    asyncio.run(postgres_storage.runtime.save(child))
    asyncio.run(postgres_storage.runtime.mark_runnable(child.run_id))
    parent_owner = asyncio.run(
        postgres_storage.runtime.claim_run(parent.run_id, "parent-worker", 30, None, 3)
    )
    child_owner = asyncio.run(
        postgres_storage.runtime.claim_run(child.run_id, "child-worker-a", 30, None, 3)
    )
    assert parent_owner is not None and parent_owner.delivery_attempt == 1
    assert child_owner is not None and child_owner.delivery_attempt == 1
    _expire(postgres_storage, child.run_id)
    child_takeover = asyncio.run(
        postgres_storage.runtime.claim_run(child.run_id, "child-worker-b", 30, None, 3)
    )
    loaded_parent = asyncio.run(postgres_storage.runtime.load(parent.run_id))
    runs = asyncio.run(postgres_storage.runtime.list(parent.thread_id))
    assert child_takeover is not None and child_takeover.delivery_attempt == 2
    assert loaded_parent is not None and loaded_parent.delivery_attempt == 1
    assert [run.run_id for run in runs].count(child.run_id) == 1


@pytest.mark.postgres
def test_delivery_exhaustion_is_terminal_and_leaves_no_capacity(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage, "run-delivery-exhausted"))
    owner = None
    for attempt in range(1, 4):
        owner = asyncio.run(
            postgres_storage.runtime.claim_run(
                state.run_id,
                f"worker-{attempt}",
                30,
                max_run_delivery_attempts=3,
            )
        )
        assert owner is not None and owner.delivery_attempt == attempt
        _expire(postgres_storage, state.run_id)

    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                state.run_id, "worker-4", 30, max_run_delivery_attempts=3
            )
        )
        is None
    )
    exhausted = asyncio.run(postgres_storage.runtime.load(state.run_id))
    assert exhausted is not None and exhausted.status == RunStatus.FAILED
    assert exhausted.error is not None and exhausted.error.type == RUN_DELIVERY_EXHAUSTED
    assert exhausted.delivery_attempt == 3 and exhausted.failure_queued_at is not None
    snapshot = asyncio.run(postgres_storage.runtime.capacity_snapshot())
    assert snapshot.queued_runs == snapshot.active_runs == 0
    assert snapshot.failure_queued_runs == 1
    assert asyncio.run(postgres_storage.runtime.claim_next("worker-5", 30, None, 3)) is None


@pytest.mark.postgres
def test_exhaustion_transition_is_not_blocked_by_active_capacity(postgres_storage):
    poison = asyncio.run(_runnable(postgres_storage, "run-poison-at-capacity"))
    holder = asyncio.run(_runnable(postgres_storage, "run-capacity-holder"))
    poison_owner = asyncio.run(
        postgres_storage.runtime.claim_run(poison.run_id, "worker-poison", 30, None, 1)
    )
    holder_owner = asyncio.run(
        postgres_storage.runtime.claim_run(holder.run_id, "worker-holder", 30, None, 1)
    )
    assert poison_owner is not None and holder_owner is not None
    _expire(postgres_storage, poison.run_id)

    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                poison.run_id,
                "worker-next",
                30,
                max_active_runs=1,
                max_run_delivery_attempts=1,
            )
        )
        is None
    )
    exhausted = asyncio.run(postgres_storage.runtime.load(poison.run_id))
    snapshot = asyncio.run(
        postgres_storage.runtime.capacity_snapshot(max_active_runs=1)
    )
    assert exhausted is not None and exhausted.status == RunStatus.FAILED
    assert snapshot.active_runs == 1
    assert snapshot.queued_runs == 0
    assert snapshot.failure_queued_runs == 1


@pytest.mark.postgres
def test_final_allowed_delivery_is_claimed_once_under_concurrency(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage, "run-final-delivery"))
    first = asyncio.run(
        postgres_storage.runtime.claim_run(state.run_id, "worker-a", 30, None, 3)
    )
    assert first is not None
    _expire(postgres_storage, state.run_id)
    second = asyncio.run(
        postgres_storage.runtime.claim_run(state.run_id, "worker-b", 30, None, 3)
    )
    assert second is not None and second.delivery_attempt == 2
    _expire(postgres_storage, state.run_id)

    async def race():
        return await asyncio.gather(
            postgres_storage.runtime.claim_run(state.run_id, "worker-c", 30, None, 3),
            postgres_storage.runtime.claim_run(state.run_id, "worker-d", 30, None, 3),
        )

    winners = [owner for owner in asyncio.run(race()) if owner is not None]
    assert len(winners) == 1 and winners[0].delivery_attempt == 3


@pytest.mark.postgres
def test_concurrent_exhaustion_converges_once_and_fences_stale_worker(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage, "run-exhaustion-race"))
    owners = []
    for index in range(3):
        owner = asyncio.run(
            postgres_storage.runtime.claim_run(
                state.run_id, f"worker-{index}", 30, None, 3
            )
        )
        assert owner is not None
        owners.append(owner)
        _expire(postgres_storage, state.run_id)
    stale = deepcopy(state)

    async def race():
        return await asyncio.gather(
            postgres_storage.runtime.claim_run(state.run_id, "worker-x", 30, None, 3),
            postgres_storage.runtime.claim_run(state.run_id, "worker-y", 30, None, 3),
        )

    assert asyncio.run(race()) == [None, None]
    exhausted = asyncio.run(postgres_storage.runtime.load(state.run_id))
    assert exhausted is not None and exhausted.status == RunStatus.FAILED
    assert exhausted.sequence == state.sequence + 1
    with pytest.raises(OwnershipLostError):
        asyncio.run(postgres_storage.runtime.save(stale, ownership=owners[-1]))


@pytest.mark.postgres
def test_worker_failures_converge_to_delivery_exhaustion(postgres_storage):
    state = asyncio.run(_runnable(postgres_storage, "run-worker-poison"))

    class CrashingRuntime:
        async def resume(self, _run_id):
            raise RuntimeError("simulated Worker process failure")

    for worker_id in ("worker-a", "worker-b"):
        worker = DistributedRunWorker(
            store=postgres_storage.runtime,
            runtime_factory=lambda _ownership: CrashingRuntime(),
            worker_id=worker_id,
            lease_seconds=30,
            heartbeat_interval_seconds=10,
            max_run_delivery_attempts=2,
        )
        with pytest.raises(RuntimeError, match="simulated Worker"):
            asyncio.run(worker.run_once())
        _expire(postgres_storage, state.run_id)

    final_worker = DistributedRunWorker(
        store=postgres_storage.runtime,
        runtime_factory=lambda _ownership: pytest.fail("exhausted Run executed again"),
        worker_id="worker-c",
        lease_seconds=30,
        heartbeat_interval_seconds=10,
        max_run_delivery_attempts=2,
    )
    assert asyncio.run(final_worker.run_once()) is None
    exhausted = asyncio.run(postgres_storage.runtime.load(state.run_id))
    assert exhausted is not None
    assert exhausted.status == RunStatus.FAILED
    assert exhausted.error is not None
    assert exhausted.error.type == RUN_DELIVERY_EXHAUSTED


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
def test_submission_idempotency_replays_and_conflicts(postgres_storage, tmp_path):
    runtime = DurableAgentRuntime(
        llm_client=_TakeoverLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=postgres_storage.runtime,
        execution_strategy="react",
    )

    first, created = asyncio.run(
        runtime.submit_idempotent(
            thread_id="thread-idempotent",
            run_id="run-idempotent-a",
            turn_id="turn-idempotent-a",
            input="same work",
            idempotency_key="request-1",
            max_queued_runs=5,
        )
    )
    replay, replay_created = asyncio.run(
        runtime.submit_idempotent(
            thread_id="thread-idempotent",
            run_id="run-idempotent-b",
            turn_id="turn-idempotent-b",
            input="same work",
            idempotency_key="request-1",
            max_queued_runs=5,
        )
    )

    assert created is True and replay_created is False
    assert replay.run_id == first.run_id == "run-idempotent-a"
    with pytest.raises(ApiError) as exc_info:
        asyncio.run(
            runtime.submit_idempotent(
                thread_id="thread-idempotent",
                run_id="run-idempotent-c",
                input="different work",
                idempotency_key="request-1",
                max_queued_runs=5,
            )
        )
    assert exc_info.value.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert exc_info.value.http_status == 409
    snapshot = asyncio.run(postgres_storage.runtime.capacity_snapshot())
    assert snapshot.idempotent_submission_replays == 1
    assert snapshot.idempotency_conflicts == 1


@pytest.mark.postgres
def test_ten_concurrent_idempotent_submissions_create_one_run(postgres_storage, tmp_path):
    runtime = DurableAgentRuntime(
        llm_client=_TakeoverLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=postgres_storage.runtime,
        execution_strategy="react",
    )

    async def submit(index: int):
        return await runtime.submit_idempotent(
            thread_id="thread-race",
            run_id=f"run-race-{index}",
            turn_id=f"turn-race-{index}",
            input="one logical request",
            idempotency_key="request-race",
            max_queued_runs=20,
        )

    async def race():
        return await asyncio.gather(*(submit(index) for index in range(10)))

    results = asyncio.run(race())
    assert len({state.run_id for state, _created in results}) == 1
    assert sum(created for _state, created in results) == 1
    assert len(asyncio.run(postgres_storage.runtime.list("thread-race"))) == 1


@pytest.mark.postgres
def test_rejected_idempotent_submission_does_not_bind_key(postgres_storage, tmp_path):
    asyncio.run(_admit(postgres_storage, "queue-holder", limit=1))
    runtime = DurableAgentRuntime(
        llm_client=_TakeoverLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=postgres_storage.runtime,
    )
    with pytest.raises(AdmissionRejectedError):
        asyncio.run(
            runtime.submit_idempotent(
                thread_id="thread-retry",
                run_id="run-rejected",
                input="work",
                idempotency_key="retryable-key",
                max_queued_runs=1,
            )
        )
    holder = asyncio.run(postgres_storage.runtime.load("queue-holder"))
    assert holder is not None
    holder.status = RunStatus.CANCELLED
    asyncio.run(postgres_storage.runtime.save(holder))
    accepted, created = asyncio.run(
        runtime.submit_idempotent(
            thread_id="thread-retry",
            run_id="run-accepted",
            input="work",
            idempotency_key="retryable-key",
            max_queued_runs=1,
        )
    )
    assert created is True and accepted.run_id == "run-accepted"
    assert asyncio.run(postgres_storage.runtime.load("run-rejected")) is None


@pytest.mark.postgres
def test_submission_transaction_failure_rolls_back_mapping(
    postgres_storage, tmp_path, monkeypatch
):
    runtime = DurableAgentRuntime(
        llm_client=_TakeoverLlm(),
        tool_registry=ToolRegistry(),
        system_prompt="test",
        cwd=str(tmp_path),
        config=AxiomConfig(),
        store=postgres_storage.runtime,
    )
    original = postgres_module._insert_initial_run

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("transaction fault")

    monkeypatch.setattr(postgres_module, "_insert_initial_run", fail_after_insert)
    with pytest.raises(RuntimeError, match="transaction fault"):
        asyncio.run(
            runtime.submit_idempotent(
                thread_id="thread-rollback",
                run_id="run-rolled-back",
                input="work",
                idempotency_key="rollback-key",
            )
        )
    assert asyncio.run(postgres_storage.runtime.load("run-rolled-back")) is None
    monkeypatch.setattr(postgres_module, "_insert_initial_run", original)
    accepted, created = asyncio.run(
        runtime.submit_idempotent(
            thread_id="thread-rollback",
            run_id="run-after-rollback",
            input="work",
            idempotency_key="rollback-key",
        )
    )
    assert created is True and accepted.run_id == "run-after-rollback"

    duplicate = Checkpoint.create(
        thread_id="thread-rollback", run_id=accepted.run_id, input="explicit duplicate"
    )
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(postgres_storage.runtime.admit_run(duplicate))


@pytest.mark.postgres
def test_manual_requeue_starts_new_cycle_and_fences_stale_owner(postgres_storage):
    stale_owner, exhausted = asyncio.run(_exhaust(postgres_storage, "run-requeue"))
    assert stale_owner is not None and exhausted is not None
    historical_sequence = exhausted.sequence
    stale = deepcopy(exhausted)

    requeued = asyncio.run(
        postgres_storage.runtime.requeue_delivery_exhausted(exhausted.run_id)
    )

    assert requeued.run_id == exhausted.run_id
    assert requeued.status == RunStatus.RUNNING
    assert requeued.sequence == historical_sequence + 1
    assert requeued.delivery_attempt == 0
    assert requeued.error is None and requeued.failure_queued_at is None
    assert asyncio.run(postgres_storage.runtime.capacity_snapshot()).manual_requeues == 1
    with pytest.raises(OwnershipLostError):
        asyncio.run(postgres_storage.runtime.save(stale, ownership=stale_owner))
    claimed = asyncio.run(
        postgres_storage.runtime.claim_run(
            requeued.run_id, "worker-new-cycle", 30, max_run_delivery_attempts=1
        )
    )
    assert claimed is not None and claimed.delivery_attempt == 1


@pytest.mark.postgres
def test_requeued_run_still_respects_active_capacity(postgres_storage):
    holder = asyncio.run(_runnable(postgres_storage, "run-active-holder"))
    active = asyncio.run(
        postgres_storage.runtime.claim_run(holder.run_id, "worker-holder", 30)
    )
    assert active is not None
    _owner, exhausted = asyncio.run(_exhaust(postgres_storage, "run-requeue-capacity"))
    assert exhausted is not None
    asyncio.run(postgres_storage.runtime.requeue_delivery_exhausted(exhausted.run_id))

    blocked = asyncio.run(
        postgres_storage.runtime.claim_run(
            exhausted.run_id,
            "worker-blocked",
            30,
            max_active_runs=1,
            max_run_delivery_attempts=1,
        )
    )

    assert blocked is None
    still_queued = asyncio.run(postgres_storage.runtime.load(exhausted.run_id))
    assert still_queued is not None and still_queued.delivery_attempt == 0


@pytest.mark.postgres
def test_manual_requeue_preserves_tool_execution_evidence(postgres_storage):
    _owner, exhausted = asyncio.run(_exhaust(postgres_storage, "run-tools-requeue"))
    assert exhausted is not None
    succeeded = ToolExecutionRecord(
        invocation_id="inv-success",
        run_id=exhausted.run_id,
        tool_call_id="call-success",
        tool_name="write",
        arguments_hash="hash-success",
        status=ToolExecutionStatus.SUCCEEDED,
        attempt=1,
        result="done",
    )
    unknown = ToolExecutionRecord(
        invocation_id="inv-unknown",
        run_id=exhausted.run_id,
        tool_call_id="call-unknown",
        tool_name="unsafe-write",
        arguments_hash="hash-unknown",
        status=ToolExecutionStatus.UNKNOWN,
        attempt=1,
        retry_state=ToolRetryState.RETRY_SUPPRESSED,
        retry_suppressed_reason="unsafe ambiguous outcome",
    )
    asyncio.run(postgres_storage.runtime.save_tool_execution(succeeded))
    asyncio.run(postgres_storage.runtime.save_tool_execution(unknown))

    asyncio.run(postgres_storage.runtime.requeue_delivery_exhausted(exhausted.run_id))

    restored_success = asyncio.run(
        postgres_storage.runtime.load_tool_execution(succeeded.invocation_id)
    )
    restored_unknown = asyncio.run(
        postgres_storage.runtime.load_tool_execution(unknown.invocation_id)
    )
    assert restored_success is not None
    assert restored_success.status == ToolExecutionStatus.SUCCEEDED
    assert restored_success.result == "done" and restored_success.attempt == 1
    assert restored_unknown is not None
    assert restored_unknown.status == ToolExecutionStatus.UNKNOWN
    assert restored_unknown.retry_state == ToolRetryState.RETRY_SUPPRESSED
    assert restored_unknown.retry_suppressed_reason == "unsafe ambiguous outcome"


@pytest.mark.postgres
@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.COMPLETED, RunStatus.CANCELLED])
def test_manual_requeue_rejects_non_failure_queue_states(postgres_storage, status):
    state = Checkpoint.create(
        thread_id="thread-ineligible", run_id=f"run-{status.value.lower()}", input="work"
    )
    state.status = status
    if status == RunStatus.FAILED:
        state.error = RunError(type="NO_PROGRESS", message="deterministic failure")
    asyncio.run(postgres_storage.runtime.save(state))
    with pytest.raises(ValueError, match="not in the delivery failure queue"):
        asyncio.run(postgres_storage.runtime.requeue_delivery_exhausted(state.run_id))


@pytest.mark.postgres
def test_operational_control_flow_keeps_one_run_and_requeues_same_state(
    postgres_storage, tmp_path
):
    config = AxiomConfig()
    config.worker.distributed_enabled = True
    config.capacity.max_queued_runs = 10
    registry = ToolRegistry()

    def factory(context: RuntimeTurnContext) -> QueryEngine:
        return QueryEngine(
            llm_client=_TakeoverLlm(),
            tool_registry=registry,
            config=context.config,
            cwd=context.cwd,
        )

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-key",
        workers=0,
        data_dir=tmp_path / "api",
        engine_factory=factory,
        durable_storage=postgres_storage,
    )
    health = _ApiRequest("/health", key="unused-for-health", method="GET")
    server._handle(health)
    diagnostics = health.json()["distributed_runtime"]
    assert diagnostics["storage_backend"] == "postgres"
    assert diagnostics["schema_version"] == 5
    assert diagnostics["ownership_enabled"] is True
    assert diagnostics["redelivery_bounded"] is False
    assert "postgres_dsn" not in json.dumps(diagnostics)
    thread_id = server.repository.create_thread()
    first_request = _ApiRequest(
        f"/v1/threads/{thread_id}/turns",
        key="submit-key",
        payload={"message": "control flow"},
    )
    replay_request = _ApiRequest(
        f"/v1/threads/{thread_id}/turns",
        key="submit-key",
        payload={"message": "control flow"},
    )
    server._handle(first_request)
    server._handle(replay_request)
    first = first_request.json()
    replay = replay_request.json()
    assert first_request.status == replay_request.status == 202
    assert replay["run_id"] == first["run_id"]
    assert len(asyncio.run(postgres_storage.runtime.list(thread_id))) == 1
    tool = ToolExecutionRecord(
        invocation_id="inv-control-flow",
        run_id=first["run_id"],
        tool_call_id="call-control-flow",
        tool_name="safe-read",
        arguments_hash="hash-control-flow",
        status=ToolExecutionStatus.SUCCEEDED,
        attempt=1,
        result="preserved",
    )
    asyncio.run(postgres_storage.runtime.save_tool_execution(tool))
    live = _ApiRequest(
        f"/v1/threads/{thread_id}/events?run_id={first['run_id']}&follow=true",
        key="unused-for-sse",
        method="GET",
    )
    follower = threading.Thread(target=server._handle, args=(live,))
    follower.start()
    deadline = time.monotonic() + 2
    while server._sse_health()["active_followers"] != 1:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    owner = asyncio.run(
        postgres_storage.runtime.claim_run(
            first["run_id"], "worker-control", 30, max_run_delivery_attempts=1
        )
    )
    assert owner is not None
    streamed_event_id = server.repository.append_event(
        thread_id, "worker.claimed", {"run_id": first["run_id"]}
    )
    _expire(postgres_storage, first["run_id"])
    assert (
        asyncio.run(
            postgres_storage.runtime.claim_run(
                first["run_id"], "worker-after-loss", 30, max_run_delivery_attempts=1
            )
        )
        is None
    )
    follower.join(timeout=2)
    assert not follower.is_alive()
    assert f"id: {streamed_event_id}\n".encode() in live.wfile.getvalue()
    request = _ApiRequest(f"/v1/runs/{first['run_id']}/requeue", key="requeue-key")
    server._handle(request)
    duplicate = _ApiRequest(f"/v1/runs/{first['run_id']}/requeue", key="requeue-key")
    server._handle(duplicate)

    assert request.status == duplicate.status == 202
    assert request.json()["run_id"] == duplicate.json()["run_id"] == first["run_id"]
    requeued = asyncio.run(postgres_storage.runtime.load(first["run_id"]))
    assert requeued is not None and requeued.delivery_attempt == 0
    assert len(
        [
            event
            for event in server.repository.list_events(thread_id, run_id=first["run_id"])
            if event.type == "run.requeued"
        ]
    ) == 1
    reclaimed = asyncio.run(
        postgres_storage.runtime.claim_run(
            first["run_id"], "worker-requeued", 30, max_run_delivery_attempts=1
        )
    )
    assert reclaimed is not None and reclaimed.delivery_attempt == 1
    restored = asyncio.run(
        postgres_storage.runtime.load_tool_execution(tool.invocation_id)
    )
    assert restored is not None and restored.result == "preserved"


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
