from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from axiom.runtime.models import Checkpoint

if TYPE_CHECKING:
    from axiom.runtime.checkpoints import DistributedRuntimeStore


class OwnershipLostError(RuntimeError):
    """Raised when a Worker can no longer prove authority over a Run."""


class DistributedWorkerConfigurationError(ValueError):
    """Raised for an unsafe distributed Worker configuration."""


@dataclass(frozen=True, slots=True)
class RunOwnership:
    run_id: str
    worker_id: str
    fencing_token: int
    lease_until: datetime
    takeover: bool = False
    delivery_attempt: int = 0


def new_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"


RuntimeFactory = Callable[[RunOwnership], object]
OwnershipEventSink = Callable[[str, dict[str, object]], Awaitable[None]]


class DistributedRunWorker:
    """Minimal PostgreSQL-backed claim/heartbeat/execute loop."""

    def __init__(
        self,
        *,
        store: DistributedRuntimeStore,
        runtime_factory: RuntimeFactory,
        lease_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 10.0,
        poll_interval_seconds: float = 0.5,
        max_active_runs: int | None = None,
        max_active_runs_per_principal: int | None = None,
        max_run_delivery_attempts: int | None = None,
        worker_id: str | None = None,
        event_sink: OwnershipEventSink | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if getattr(store, "backend", None) != "postgres":
            raise DistributedWorkerConfigurationError(
                "distributed Worker ownership requires PostgreSQL storage"
            )
        if lease_seconds <= 0 or heartbeat_interval_seconds <= 0:
            raise DistributedWorkerConfigurationError("lease and heartbeat must be positive")
        if heartbeat_interval_seconds >= lease_seconds:
            raise DistributedWorkerConfigurationError(
                "heartbeat interval must be shorter than the lease duration"
            )
        if poll_interval_seconds <= 0:
            raise DistributedWorkerConfigurationError("poll interval must be positive")
        if max_active_runs is not None and max_active_runs <= 0:
            raise DistributedWorkerConfigurationError(
                "max_active_runs must be positive or null"
            )
        if max_run_delivery_attempts is not None and max_run_delivery_attempts <= 0:
            raise DistributedWorkerConfigurationError(
                "max_run_delivery_attempts must be positive or null"
            )
        if max_active_runs_per_principal is not None and max_active_runs_per_principal <= 0:
            raise DistributedWorkerConfigurationError(
                "max_active_runs_per_principal must be positive or null"
            )
        self.store = store
        self.runtime_factory = runtime_factory
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_active_runs = max_active_runs
        self.max_active_runs_per_principal = max_active_runs_per_principal
        self.max_run_delivery_attempts = max_run_delivery_attempts
        self.worker_id = worker_id or new_worker_id()
        self.event_sink = event_sink
        self.sleep = sleep
        self._stopping = False
        self._active: dict[str, asyncio.Task[Checkpoint]] = {}

    async def run_once(self) -> Checkpoint | None:
        if self._stopping:
            return None
        if self.max_run_delivery_attempts is None:
            if self.max_active_runs_per_principal is None:
                ownership = await self.store.claim_next(
                    self.worker_id, self.lease_seconds, self.max_active_runs
                )
            else:
                ownership = await self.store.claim_next(
                    self.worker_id,
                    self.lease_seconds,
                    self.max_active_runs,
                    max_active_runs_per_principal=self.max_active_runs_per_principal,
                )
        else:
            if self.max_active_runs_per_principal is None:
                ownership = await self.store.claim_next(
                    self.worker_id,
                    self.lease_seconds,
                    self.max_active_runs,
                    self.max_run_delivery_attempts,
                )
            else:
                ownership = await self.store.claim_next(
                    self.worker_id,
                    self.lease_seconds,
                    self.max_active_runs,
                    self.max_run_delivery_attempts,
                    max_active_runs_per_principal=self.max_active_runs_per_principal,
                )
        if ownership is None:
            return None
        await self._emit(
            "run.taken_over" if ownership.takeover else "run.claimed",
            ownership,
        )
        runtime = self.runtime_factory(ownership)
        execute = asyncio.create_task(runtime.resume(ownership.run_id))
        self._active[ownership.run_id] = execute
        heartbeat = asyncio.create_task(self._heartbeat(ownership, execute))
        try:
            result = await execute
            await self.store.release_lease(
                ownership,
                runnable=(
                    not result.finished
                    and result.status.value == "RUNNING"
                ),
            )
            await self._emit("run.released", ownership)
            return result
        except asyncio.CancelledError:
            if heartbeat.done():
                error = heartbeat.exception()
                if isinstance(error, OwnershipLostError):
                    raise error from None
            await self.store.release_lease(ownership, runnable=True)
            await self._emit("run.released", ownership)
            raise
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._active.pop(ownership.run_id, None)

    async def run_forever(self) -> None:
        while not self._stopping:
            result = await self.run_once()
            if result is None and not self._stopping:
                await self.sleep(self.poll_interval_seconds)

    async def shutdown(self) -> None:
        self._stopping = True
        tasks = tuple(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat(
        self,
        ownership: RunOwnership,
        execute: asyncio.Task[Checkpoint],
    ) -> None:
        while not execute.done():
            await self.sleep(self.heartbeat_interval_seconds)
            if execute.done():
                return
            try:
                renewed = await self.store.renew_lease(ownership, self.lease_seconds)
            except Exception as exc:
                execute.cancel()
                await self._emit("run.ownership_lost", ownership)
                raise OwnershipLostError(
                    f"ownership renewal unavailable for {ownership.run_id}"
                ) from exc
            if renewed is None:
                execute.cancel()
                await self._emit("run.ownership_lost", ownership)
                raise OwnershipLostError(f"ownership lost for {ownership.run_id}")
            ownership = renewed

    async def _emit(self, event_type: str, ownership: RunOwnership) -> None:
        if self.event_sink is None:
            return
        await self.event_sink(
            event_type,
            {
                "run_id": ownership.run_id,
                "worker_id": ownership.worker_id,
                "fencing_token": ownership.fencing_token,
                "takeover": ownership.takeover,
                "delivery_attempt": ownership.delivery_attempt,
            },
        )
