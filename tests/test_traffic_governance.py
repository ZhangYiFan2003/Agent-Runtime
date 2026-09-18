from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest

from axiom.runtime.models import Checkpoint
from axiom.runtime.postgres import PostgresRuntimeStore
from axiom.runtime.storage import DurableStorage, StorageConfig, create_durable_storage


@contextmanager
def _postgres_storage(tmp_path) -> Iterator[DurableStorage]:
    dsn = os.environ.get("AXIOM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("AXIOM_TEST_POSTGRES_DSN is not configured")
    psycopg = pytest.importorskip("psycopg")
    sql = pytest.importorskip("psycopg.sql")
    conninfo = pytest.importorskip("psycopg.conninfo")
    schema = f"axiom_governance_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
    isolated = conninfo.make_conninfo(dsn, options=f"-c search_path={schema}")
    storage = create_durable_storage(
        StorageConfig(backend="postgres", postgres_dsn=isolated),
        default_sqlite_path=tmp_path / "unused.db",
    )
    try:
        yield storage
    finally:
        storage.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("drop schema {} cascade").format(sql.Identifier(schema)))


@pytest.fixture
def postgres_storage(tmp_path) -> Iterator[DurableStorage]:
    with _postgres_storage(tmp_path) as storage:
        yield storage


def _state(run_id: str, *, principal: str = "default", priority: int = 1) -> Checkpoint:
    state = Checkpoint.create(thread_id=f"thread-{run_id}", run_id=run_id, input="work")
    state.principal_key = principal
    state.base_priority = priority
    return state


@pytest.mark.postgres
def test_global_and_principal_token_buckets_are_shared(postgres_storage):
    store: PostgresRuntimeStore = postgres_storage.runtime
    def consume():
        return asyncio.run(
            store.consume_submission_tokens(
                "principal-a",
                global_rate=1.0,
                global_burst=2,
                principal_rate=1.0,
                principal_burst=2,
            )
        )

    assert consume() is None
    assert consume() is None
    assert consume() is not None


@pytest.mark.postgres
def test_token_bucket_refills_from_database_timestamp(postgres_storage):
    store: PostgresRuntimeStore = postgres_storage.runtime
    assert asyncio.run(
        store.consume_submission_tokens(
            "principal-refill",
            global_rate=10.0,
            global_burst=1,
            principal_rate=None,
            principal_burst=None,
        )
    ) is None
    with store.pool.connection() as conn:
        conn.execute(
            """
            update traffic_rate_buckets
            set last_refill_at = current_timestamp - interval '2 seconds'
            where scope = 'global' and scope_key = 'root_submission'
            """
        )
    assert asyncio.run(
        store.consume_submission_tokens(
            "principal-refill",
            global_rate=1.0,
            global_burst=1,
            principal_rate=None,
            principal_burst=None,
        )
    ) is None


@pytest.mark.postgres
def test_principal_queue_quota_is_atomic_and_does_not_bind_rejected_key(postgres_storage):
    store = postgres_storage.runtime

    async def submit(index: int):
        state = _state(f"quota-{index}", principal="noisy")
        return await store.admit_submission(
            state,
            idempotency_key=f"key-{index}",
            request_fingerprint="same",
            max_queued_runs=20,
            max_queued_runs_per_principal=2,
        )

    async def race():
        return await asyncio.gather(*(submit(index) for index in range(8)), return_exceptions=True)

    results = asyncio.run(race())
    admitted = [item for item in results if not isinstance(item, Exception)]
    assert len(admitted) == 2
    assert sum(type(item).__name__ == "ApiError" for item in results) == 6


@pytest.mark.postgres
def test_priority_and_aging_order_is_deterministic(postgres_storage):
    store = postgres_storage.runtime
    for run_id, priority in (("low", 0), ("normal", 1), ("high", 2)):
        state = _state(run_id, priority=priority)
        asyncio.run(store.admit_run(state, max_queued_runs=10))
    first = asyncio.run(store.claim_next("worker-high", 30))
    assert first is not None and first.run_id == "high"

    with store.pool.connection() as conn:
        conn.execute(
            """
            update runs
            set state_json = jsonb_set(
                state_json, ARRAY['runnable_since'],
                to_jsonb((current_timestamp - interval '1000 seconds')::text), true
            )
            where run_id = 'low'
            """
        )
    second = asyncio.run(store.claim_next("worker-old-low", 30))
    assert second is not None and second.run_id == "low"


@pytest.mark.postgres
def test_active_quota_isolates_noisy_neighbor(postgres_storage):
    store = postgres_storage.runtime
    for run_id, principal in (("a-1", "a"), ("a-2", "a"), ("b-1", "b")):
        state = _state(run_id, principal=principal)
        asyncio.run(store.admit_run(state, max_queued_runs=10))
    first = asyncio.run(
        store.claim_next("worker-1", 30, max_active_runs=2, max_active_runs_per_principal=1)
    )
    second = asyncio.run(
        store.claim_next("worker-2", 30, max_active_runs=2, max_active_runs_per_principal=1)
    )
    assert {first.run_id if first else None, second.run_id if second else None} == {"a-1", "b-1"}


@pytest.mark.postgres
def test_idempotent_replay_does_not_create_another_run_after_rate_check(postgres_storage):
    store = postgres_storage.runtime
    first = _state("idem-a", principal="default")
    replay = _state("idem-b", principal="default")
    replay.thread_id = first.thread_id
    created, is_new = asyncio.run(
        store.admit_submission(
            first,
            idempotency_key="same-key",
            request_fingerprint="fingerprint",
            max_queued_runs=10,
        )
    )
    replayed, replay_is_new = asyncio.run(
        store.admit_submission(
            replay,
            idempotency_key="same-key",
            request_fingerprint="fingerprint",
            max_queued_runs=10,
        )
    )
    assert is_new is True and replay_is_new is False
    assert replayed.run_id == created.run_id
