from __future__ import annotations

import asyncio

import pytest

from axiom.config import AxiomConfig
from axiom.entrypoints.cli import _run_distributed_worker
from axiom.runtime.api import RuntimeApiServer


class _PostgresStore:
    backend = "postgres"


def _server_for_worker(config: AxiomConfig) -> RuntimeApiServer:
    server = object.__new__(RuntimeApiServer)
    server.config = config
    server.storage_backend = "postgres"
    server.checkpoint_store = _PostgresStore()
    return server


def test_worker_composition_uses_existing_distributed_config():
    config = AxiomConfig()
    config.worker.distributed_enabled = True
    config.worker.lease_seconds = 45
    config.worker.heartbeat_interval_seconds = 12
    config.worker.poll_interval_seconds = 0.75
    config.worker.max_run_delivery_attempts = 4
    config.capacity.max_active_runs = 8
    config.capacity.max_active_runs_per_principal = 3

    worker = _server_for_worker(config).build_distributed_worker(worker_id="worker-test")

    assert worker.worker_id == "worker-test"
    assert worker.lease_seconds == 45
    assert worker.heartbeat_interval_seconds == 12
    assert worker.poll_interval_seconds == 0.75
    assert worker.max_active_runs == 8
    assert worker.max_active_runs_per_principal == 3
    assert worker.max_run_delivery_attempts == 4


def test_worker_composition_refuses_disabled_or_non_postgres_mode():
    config = AxiomConfig()
    server = _server_for_worker(config)
    with pytest.raises(ValueError, match="distributed_enabled=true"):
        server.build_distributed_worker()

    config.worker.distributed_enabled = True
    server.storage_backend = "sqlite"
    with pytest.raises(ValueError, match="storage.backend=postgres"):
        server.build_distributed_worker()


def test_worker_process_shutdown_stops_active_loop():
    class Worker:
        def __init__(self):
            self.started = asyncio.Event()
            self.stopping = False
            self.shutdown_calls = 0

        async def run_forever(self):
            self.started.set()
            while not self.stopping:
                await asyncio.sleep(0)

        async def shutdown(self):
            self.shutdown_calls += 1
            self.stopping = True

    async def scenario():
        worker = Worker()
        stop = asyncio.Event()
        process = asyncio.create_task(
            _run_distributed_worker(
                worker,
                stop_event=stop,
                install_signal_handlers=False,
            )
        )
        await worker.started.wait()
        stop.set()
        await process
        assert worker.shutdown_calls == 1
        assert worker.stopping

    asyncio.run(scenario())
