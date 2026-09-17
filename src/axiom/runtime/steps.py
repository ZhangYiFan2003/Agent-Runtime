from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from axiom.runtime.models import RunState, RunStatus
from axiom.runtime.ownership import RunOwnership


class NextAction(StrEnum):
    """Continuation requested after one ephemeral Runtime iteration."""

    CONTINUE = "CONTINUE"
    COMPLETE = "COMPLETE"
    WAIT = "WAIT"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class StepContext:
    """In-memory inputs for one Runtime execution iteration.

    The RunState reference remains the durable authority. This object is never
    serialized or stored independently.
    """

    run_id: str
    step_index: int
    strategy: str
    run_state: RunState
    control_state: RunStatus
    ownership_context: RunOwnership | None = None


@dataclass(frozen=True, slots=True)
class StepResult:
    """In-memory outcome of one Runtime execution iteration."""

    step_index: int
    run_state: RunState
    next_action: NextAction | None
    tool_invocation_ids: tuple[str, ...] = ()

    @classmethod
    def from_run_state(
        cls,
        *,
        step_index: int,
        run_state: RunState,
        tool_invocation_ids: tuple[str, ...] = (),
    ) -> StepResult:
        return cls(
            step_index=step_index,
            run_state=run_state,
            next_action=_next_action(run_state.status),
            tool_invocation_ids=tool_invocation_ids,
        )


def _next_action(status: RunStatus) -> NextAction | None:
    if status == RunStatus.RUNNING:
        return NextAction.CONTINUE
    if status == RunStatus.COMPLETED:
        return NextAction.COMPLETE
    if status == RunStatus.FAILED:
        return NextAction.FAIL
    if status in {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_CHILD}:
        return NextAction.WAIT
    # CANCELLED and INTERRUPTED remain authoritative Run control states rather
    # than ordinary strategy continuation decisions.
    return None
