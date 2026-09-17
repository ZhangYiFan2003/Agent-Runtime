from __future__ import annotations

from axiom.runtime import (
    CompletionPolicy,
    CompletionVerificationStatus,
    NextAction,
    ProgressDecisionType,
    RunStatus,
)


def test_normal_strategy_evidence_continues() -> None:
    assert (
        CompletionPolicy().decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.CONTINUE,
        )
        == NextAction.CONTINUE
    )


def test_verified_or_not_applicable_completion_completes() -> None:
    policy = CompletionPolicy()

    for status in (
        CompletionVerificationStatus.VERIFIED,
        CompletionVerificationStatus.NOT_APPLICABLE,
    ):
        assert (
            policy.decide(
                run_status=RunStatus.RUNNING,
                proposed_action=NextAction.COMPLETE,
                verification_status=status,
            )
            == NextAction.COMPLETE
        )


def test_not_applicable_does_not_create_completion_candidate() -> None:
    assert (
        CompletionPolicy().decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.CONTINUE,
            verification_status=CompletionVerificationStatus.NOT_APPLICABLE,
        )
        == NextAction.CONTINUE
    )


def test_unverified_completion_has_one_bounded_corrective_continuation() -> None:
    policy = CompletionPolicy()

    assert (
        policy.decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.COMPLETE,
            verification_status=CompletionVerificationStatus.NOT_VERIFIED,
            allow_completion_correction=True,
            verification_attempt=1,
            max_correction_attempts=1,
        )
        == NextAction.CONTINUE
    )
    assert (
        policy.decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.COMPLETE,
            verification_status=CompletionVerificationStatus.NOT_VERIFIED,
            allow_completion_correction=True,
            verification_attempt=2,
            max_correction_attempts=1,
        )
        == NextAction.FAIL
    )


def test_verification_error_fails_closed() -> None:
    assert (
        CompletionPolicy().decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.COMPLETE,
            verification_status=CompletionVerificationStatus.ERROR,
        )
        == NextAction.FAIL
    )


def test_terminal_no_progress_fails_but_recovery_continues() -> None:
    policy = CompletionPolicy()

    assert (
        policy.decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.CONTINUE,
            progress_decision=ProgressDecisionType.TERMINATE,
        )
        == NextAction.FAIL
    )
    assert (
        policy.decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.CONTINUE,
            progress_decision=ProgressDecisionType.RECOVER,
        )
        == NextAction.CONTINUE
    )


def test_strategy_wait_is_preserved() -> None:
    assert (
        CompletionPolicy().decide(
            run_status=RunStatus.WAITING_CHILD,
            proposed_action=NextAction.WAIT,
        )
        == NextAction.WAIT
    )


def test_cancel_and_interrupt_outrank_verified_completion() -> None:
    policy = CompletionPolicy()

    for status in (RunStatus.CANCELLED, RunStatus.INTERRUPTED):
        assert (
            policy.decide(
                run_status=status,
                proposed_action=NextAction.COMPLETE,
                verification_status=CompletionVerificationStatus.VERIFIED,
            )
            is None
        )


def test_hard_budget_and_fatal_error_outrank_continue() -> None:
    policy = CompletionPolicy()

    assert (
        policy.decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.CONTINUE,
            budget_exhausted=True,
        )
        == NextAction.FAIL
    )
    assert (
        policy.decide(
            run_status=RunStatus.RUNNING,
            proposed_action=NextAction.CONTINUE,
            fatal_error=True,
        )
        == NextAction.FAIL
    )


def test_policy_is_deterministic_and_does_not_mutate_inputs() -> None:
    policy = CompletionPolicy()
    arguments = {
        "run_status": RunStatus.RUNNING,
        "proposed_action": NextAction.COMPLETE,
        "verification_status": CompletionVerificationStatus.VERIFIED,
    }

    first = policy.decide(**arguments)
    second = policy.decide(**arguments)

    assert first == second == NextAction.COMPLETE
    assert arguments["run_status"] == RunStatus.RUNNING
