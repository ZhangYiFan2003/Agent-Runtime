from __future__ import annotations

from dataclasses import dataclass

QUEUE_CAPACITY_EXCEEDED = "QUEUE_CAPACITY_EXCEEDED"


class AdmissionRejectedError(RuntimeError):
    """Raised before persistence when the durable runnable backlog is full."""

    def __init__(self, *, queued_runs: int, max_queued_runs: int) -> None:
        self.reason = QUEUE_CAPACITY_EXCEEDED
        self.queued_runs = queued_runs
        self.max_queued_runs = max_queued_runs
        super().__init__("durable runnable backlog is at capacity")


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    """Derived scheduling capacity plus process-local operational counters."""

    queued_runs: int
    active_runs: int
    max_queued_runs: int | None
    max_active_runs: int | None
    admission_rejections: int = 0
    capacity_blocked_claims: int = 0
    oldest_queued_age_seconds: float | None = None
    failure_queued_runs: int = 0
    mean_delivery_attempts: float = 0.0
    max_delivery_attempts: int = 0
    idempotent_submission_replays: int = 0
    idempotency_conflicts: int = 0
    manual_requeues: int = 0
