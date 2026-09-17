from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from axiom.runtime.completion import CompletionVerificationStatus
from axiom.runtime.models import RunState, RunStatus
from axiom.runtime.ownership import RunOwnership
from axiom.runtime.progress import ProgressDecisionType


class NextAction(StrEnum):
    """Continuation requested after one ephemeral Runtime iteration."""

    CONTINUE = "CONTINUE"
    COMPLETE = "COMPLETE"
    WAIT = "WAIT"
    FAIL = "FAIL"


class CompletionPolicy:
    """Choose the Runtime continuation from small, structured evidence.

    The policy is deliberately pure: detectors and verifiers produce evidence,
    while the Runtime remains responsible for applying and persisting the
    resulting RunState transition.
    """

    def decide(
        self,
        *,
        run_status: RunStatus,
        proposed_action: NextAction | None = None,
        progress_decision: ProgressDecisionType | None = None,
        verification_status: CompletionVerificationStatus | None = None,
        allow_completion_correction: bool = False,
        verification_attempt: int = 0,
        max_correction_attempts: int = 0,
        budget_exhausted: bool = False,
        fatal_error: bool = False,
    ) -> NextAction | None:
        # Durable control state is authoritative and is not represented as a
        # normal continuation action.
        if run_status in {RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
            return None
        if budget_exhausted or fatal_error or run_status == RunStatus.FAILED:
            return NextAction.FAIL
        if progress_decision == ProgressDecisionType.TERMINATE:
            return NextAction.FAIL
        if progress_decision == ProgressDecisionType.RECOVER:
            return NextAction.CONTINUE

        action = proposed_action or _action_for_status(run_status)
        if action == NextAction.COMPLETE and verification_status is not None:
            if verification_status in {
                CompletionVerificationStatus.VERIFIED,
                CompletionVerificationStatus.NOT_APPLICABLE,
            }:
                return NextAction.COMPLETE
            if (
                verification_status == CompletionVerificationStatus.NOT_VERIFIED
                and allow_completion_correction
                and verification_attempt <= max_correction_attempts
            ):
                return NextAction.CONTINUE
            return NextAction.FAIL
        return action


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
            next_action=CompletionPolicy().decide(run_status=run_state.status),
            tool_invocation_ids=tool_invocation_ids,
        )


def _action_for_status(status: RunStatus) -> NextAction | None:
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
