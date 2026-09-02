from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

CancellationStatus = Literal[
    "signalled",
    "not_active",
    "already_done",
    "loop_unavailable",
]


class ActiveRunRegistrationError(ValueError):
    """Raised when a live execution already owns a run in this process."""


@dataclass(slots=True)
class ExecutionHandle:
    """A process-local reference to one running durable execution."""

    run_id: str
    thread_id: str
    turn_id: str
    parent_run_id: str | None
    run_kind: str
    event_loop: asyncio.AbstractEventLoop
    asyncio_task: asyncio.Task[Any]
    owner_thread_id: int
    registered_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    cancellation_requested: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def inspection_view(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "parent_run_id": self.parent_run_id,
            "run_kind": self.run_kind,
            "execution_strategy": self.metadata.get("execution_strategy"),
            "registered_at": self.registered_at,
            "owner_thread_id": self.owner_thread_id,
            "task_done": self.asyncio_task.done(),
            "cancellation_requested": self.cancellation_requested,
        }


@dataclass(frozen=True, slots=True)
class CancellationRequestResult:
    run_id: str
    status: CancellationStatus

    @property
    def signalled(self) -> bool:
        return self.status == "signalled"


@dataclass(frozen=True, slots=True)
class CancelManyResult:
    requested: tuple[str, ...]
    signalled: tuple[str, ...]
    not_active: tuple[str, ...]
    already_done: tuple[str, ...]
    loop_unavailable: tuple[str, ...]


class ActiveRunSupervisor:
    """Thread-safe registry for executions active in the current process only."""

    def __init__(self) -> None:
        self._handles: dict[str, ExecutionHandle] = {}
        self._condition = threading.Condition(threading.RLock())

    def register(self, handle: ExecutionHandle) -> ExecutionHandle:
        with self._condition:
            existing = self._handles.get(handle.run_id)
            if existing is not None:
                if not existing.asyncio_task.done():
                    raise ActiveRunRegistrationError(
                        f"run already has an active execution: {handle.run_id}"
                    )
                del self._handles[handle.run_id]
            self._handles[handle.run_id] = handle
            self._condition.notify_all()
            return handle

    def unregister(
        self,
        run_id: str,
        *,
        expected: ExecutionHandle | None = None,
    ) -> ExecutionHandle | None:
        with self._condition:
            current = self._handles.get(run_id)
            if current is None or (expected is not None and current is not expected):
                return None
            removed = self._handles.pop(run_id)
            self._condition.notify_all()
            return removed

    def get(self, run_id: str) -> ExecutionHandle | None:
        with self._condition:
            handle = self._handles.get(run_id)
            if handle is not None and handle.asyncio_task.done():
                del self._handles[run_id]
                self._condition.notify_all()
                return None
            return handle

    def is_active(self, run_id: str) -> bool:
        return self.get(run_id) is not None

    def list_active(self) -> tuple[ExecutionHandle, ...]:
        with self._condition:
            self._remove_done_locked()
            return tuple(self._handles.values())

    def inspect_active(self) -> list[dict[str, object]]:
        return [handle.inspection_view() for handle in self.list_active()]

    def request_cancel(self, run_id: str) -> CancellationRequestResult:
        with self._condition:
            handle = self._handles.get(run_id)
            if handle is None:
                return CancellationRequestResult(run_id, "not_active")
            if handle.asyncio_task.done():
                del self._handles[run_id]
                self._condition.notify_all()
                return CancellationRequestResult(run_id, "already_done")
            handle.cancellation_requested = True
            if handle.event_loop.is_closed():
                return CancellationRequestResult(run_id, "loop_unavailable")
            try:
                handle.event_loop.call_soon_threadsafe(_cancel_if_pending, handle.asyncio_task)
            except RuntimeError:
                return CancellationRequestResult(run_id, "loop_unavailable")
            return CancellationRequestResult(run_id, "signalled")

    def request_cancel_many(self, run_ids: list[str] | tuple[str, ...]) -> CancelManyResult:
        requested = tuple(dict.fromkeys(run_ids))
        buckets: dict[CancellationStatus, list[str]] = {
            "signalled": [],
            "not_active": [],
            "already_done": [],
            "loop_unavailable": [],
        }
        for run_id in requested:
            result = self.request_cancel(run_id)
            buckets[result.status].append(run_id)
        return CancelManyResult(
            requested=requested,
            signalled=tuple(buckets["signalled"]),
            not_active=tuple(buckets["not_active"]),
            already_done=tuple(buckets["already_done"]),
            loop_unavailable=tuple(buckets["loop_unavailable"]),
        )

    def drain(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            while True:
                self._remove_done_locked()
                if not self._handles:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def shutdown(self, timeout: float) -> bool:
        handles = self.list_active()
        self.request_cancel_many(tuple(handle.run_id for handle in handles))
        return self.drain(timeout)

    def _remove_done_locked(self) -> None:
        done = [run_id for run_id, handle in self._handles.items() if handle.asyncio_task.done()]
        for run_id in done:
            del self._handles[run_id]
        if done:
            self._condition.notify_all()


def _cancel_if_pending(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
