from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from axiom.config import ProgressConfig

NO_PROGRESS = "NO_PROGRESS"
_MAX_ERROR_CHARS = 500
_DEFAULT_HISTORY_LIMIT = 32


class ProgressDetectorType(StrEnum):
    IDENTICAL_ACTION_LOOP = "IDENTICAL_ACTION_LOOP"
    REPEATED_ERROR = "REPEATED_ERROR"
    ACTION_CYCLE = "ACTION_CYCLE"
    STATE_STAGNATION = "STATE_STAGNATION"


class ProgressDecisionType(StrEnum):
    CONTINUE = "CONTINUE"
    RECOVER = "RECOVER"
    TERMINATE = "TERMINATE"


@dataclass(frozen=True, slots=True)
class ProgressPolicy:
    enabled: bool = True
    max_identical_actions: int = 4
    max_identical_errors: int = 3
    max_cycle_repetitions: int = 3
    max_stagnant_steps: int = 8
    max_recovery_attempts: int = 1
    history_limit: int = _DEFAULT_HISTORY_LIMIT

    def __post_init__(self) -> None:
        for name in (
            "max_identical_actions",
            "max_identical_errors",
            "max_cycle_repetitions",
            "max_stagnant_steps",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 2 or value > 100:
                raise ValueError(f"{name} must be an integer between 2 and 100")
        if not isinstance(self.max_recovery_attempts, int) or not (
            0 <= self.max_recovery_attempts <= 10
        ):
            raise ValueError("max_recovery_attempts must be between 0 and 10")
        if not isinstance(self.history_limit, int) or not 8 <= self.history_limit <= 256:
            raise ValueError("history_limit must be between 8 and 256")
        required = max(
            self.max_identical_actions,
            self.max_identical_errors,
            self.max_stagnant_steps,
            self.max_cycle_repetitions * 4,
        )
        if self.history_limit < required:
            raise ValueError("history_limit is too small for the configured thresholds")

    @classmethod
    def from_config(cls, config: ProgressConfig) -> ProgressPolicy:
        return cls(
            enabled=config.enabled,
            max_identical_actions=config.max_identical_actions,
            max_identical_errors=config.max_identical_errors,
            max_cycle_repetitions=config.max_cycle_repetitions,
            max_stagnant_steps=config.max_stagnant_steps,
            max_recovery_attempts=config.max_recovery_attempts,
            history_limit=config.history_limit,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressPolicy:
        return cls(
            enabled=bool(data.get("enabled", True)),
            max_identical_actions=int(data.get("max_identical_actions", 4)),
            max_identical_errors=int(data.get("max_identical_errors", 3)),
            max_cycle_repetitions=int(data.get("max_cycle_repetitions", 3)),
            max_stagnant_steps=int(data.get("max_stagnant_steps", 8)),
            max_recovery_attempts=int(data.get("max_recovery_attempts", 1)),
            history_limit=int(data.get("history_limit", _DEFAULT_HISTORY_LIMIT)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_identical_actions": self.max_identical_actions,
            "max_identical_errors": self.max_identical_errors,
            "max_cycle_repetitions": self.max_cycle_repetitions,
            "max_stagnant_steps": self.max_stagnant_steps,
            "max_recovery_attempts": self.max_recovery_attempts,
            "history_limit": self.history_limit,
        }


@dataclass(frozen=True, slots=True)
class ProgressObservation:
    operation_id: str
    step: int
    action_fingerprint: str | None = None
    error_fingerprint: str | None = None
    state_fingerprint: str | None = None
    eligible_for_stagnation: bool = True


@dataclass(slots=True)
class ProgressState:
    action_history: list[str] = field(default_factory=list)
    error_history: list[str] = field(default_factory=list)
    progress_history: list[str] = field(default_factory=list)
    processed_operations: list[str] = field(default_factory=list)
    action_repeat_count: int = 0
    error_repeat_count: int = 0
    stagnant_steps: int = 0
    recovery_attempts: int = 0
    recovery_signal_pending: bool = False
    last_progress_step: int = 0
    detected: bool = False
    detector_type: str | None = None
    cycle_length: int | None = None
    repetition_count: int = 0
    last_action_fingerprint: str | None = None
    last_error_fingerprint: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProgressState:
        return cls(
            action_history=_strings(data.get("action_history")),
            error_history=_strings(data.get("error_history")),
            progress_history=_strings(data.get("progress_history")),
            processed_operations=_strings(data.get("processed_operations")),
            action_repeat_count=int(data.get("action_repeat_count") or 0),
            error_repeat_count=int(data.get("error_repeat_count") or 0),
            stagnant_steps=int(data.get("stagnant_steps") or 0),
            recovery_attempts=int(data.get("recovery_attempts") or 0),
            recovery_signal_pending=bool(data.get("recovery_signal_pending")),
            last_progress_step=int(data.get("last_progress_step") or 0),
            detected=bool(data.get("detected")),
            detector_type=_optional_text(data.get("detector_type")),
            cycle_length=_optional_int(data.get("cycle_length")),
            repetition_count=int(data.get("repetition_count") or 0),
            last_action_fingerprint=_optional_text(data.get("last_action_fingerprint")),
            last_error_fingerprint=_optional_text(data.get("last_error_fingerprint")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_history": list(self.action_history),
            "error_history": list(self.error_history),
            "progress_history": list(self.progress_history),
            "processed_operations": list(self.processed_operations),
            "action_repeat_count": self.action_repeat_count,
            "error_repeat_count": self.error_repeat_count,
            "stagnant_steps": self.stagnant_steps,
            "recovery_attempts": self.recovery_attempts,
            "recovery_signal_pending": self.recovery_signal_pending,
            "last_progress_step": self.last_progress_step,
            "detected": self.detected,
            "detector_type": self.detector_type,
            "cycle_length": self.cycle_length,
            "repetition_count": self.repetition_count,
            "last_action_fingerprint": self.last_action_fingerprint,
            "last_error_fingerprint": self.last_error_fingerprint,
        }

    def observability_attributes(self) -> dict[str, object]:
        return {
            "progress.action_repeat_count": self.action_repeat_count,
            "progress.error_repeat_count": self.error_repeat_count,
            "progress.stagnant_steps": self.stagnant_steps,
            "progress.detected": self.detected,
            "progress.detector_type": self.detector_type,
            "progress.cycle_length": self.cycle_length,
            "progress.recovery_attempts": self.recovery_attempts,
            "progress.recovery_triggered": self.recovery_signal_pending,
            "progress.last_progress_step": self.last_progress_step,
        }


@dataclass(frozen=True, slots=True)
class ProgressDecision:
    decision: ProgressDecisionType
    detector_type: ProgressDetectorType | None = None
    repetition_count: int = 0
    cycle_length: int | None = None
    duplicate: bool = False

    @property
    def detected(self) -> bool:
        return self.detector_type is not None


class NoProgressError(RuntimeError):
    code = NO_PROGRESS

    def __init__(
        self,
        *,
        run_id: str,
        step: int,
        state: ProgressState,
    ) -> None:
        self.run_id = run_id
        self.step = step
        self.progress_state = state
        super().__init__(f"no progress after bounded recovery: {state.detector_type or 'unknown'}")

    def metadata(self) -> dict[str, Any]:
        return {
            "reason": self.progress_state.detector_type,
            "detector_type": self.progress_state.detector_type,
            "repetition_count": self.progress_state.repetition_count,
            "cycle_length": self.progress_state.cycle_length,
            "action_fingerprint": self.progress_state.last_action_fingerprint,
            "error_fingerprint": self.progress_state.last_error_fingerprint,
            "recovery_attempts": self.progress_state.recovery_attempts,
            "run_id": self.run_id,
            "step": self.step,
        }


class ProgressDetector:
    def __init__(self, policy: ProgressPolicy | None = None) -> None:
        self.policy = policy or ProgressPolicy()

    def observe(self, state: ProgressState, observation: ProgressObservation) -> ProgressDecision:
        if not self.policy.enabled:
            return ProgressDecision(ProgressDecisionType.CONTINUE)
        if observation.operation_id in state.processed_operations:
            return ProgressDecision(ProgressDecisionType.CONTINUE, duplicate=True)
        _append_bounded(
            state.processed_operations,
            observation.operation_id,
            self.policy.history_limit,
        )

        meaningful_progress = bool(
            observation.state_fingerprint
            and observation.state_fingerprint not in state.progress_history
        )
        if meaningful_progress:
            _append_bounded(
                state.progress_history,
                str(observation.state_fingerprint),
                self.policy.history_limit,
            )
            state.stagnant_steps = 0
            state.last_progress_step = observation.step
            state.recovery_attempts = 0
            state.recovery_signal_pending = False
            state.detected = False
            state.detector_type = None
            state.cycle_length = None
            state.repetition_count = 0
            state.action_history.clear()
            state.error_history.clear()
        elif observation.eligible_for_stagnation:
            state.stagnant_steps += 1

        if observation.action_fingerprint:
            action = observation.action_fingerprint
            state.action_repeat_count = (
                state.action_repeat_count + 1
                if state.last_action_fingerprint == action and not meaningful_progress
                else 1
            )
            state.last_action_fingerprint = action
            _append_bounded(state.action_history, action, self.policy.history_limit)
        else:
            state.action_repeat_count = 0

        if observation.error_fingerprint:
            error = observation.error_fingerprint
            state.error_repeat_count = (
                state.error_repeat_count + 1
                if state.last_error_fingerprint == error and not meaningful_progress
                else 1
            )
            state.last_error_fingerprint = error
            _append_bounded(state.error_history, error, self.policy.history_limit)
        else:
            state.error_repeat_count = 0
            state.last_error_fingerprint = None

        detector_type: ProgressDetectorType | None = None
        repetitions = 0
        cycle_length: int | None = None
        if state.error_repeat_count >= self.policy.max_identical_errors:
            detector_type = ProgressDetectorType.REPEATED_ERROR
            repetitions = state.error_repeat_count
        elif state.action_repeat_count >= self.policy.max_identical_actions:
            detector_type = ProgressDetectorType.IDENTICAL_ACTION_LOOP
            repetitions = state.action_repeat_count
        else:
            cycle_length, cycle_repetitions = _detect_cycle(
                state.action_history,
                self.policy.max_cycle_repetitions,
            )
            if cycle_length is not None:
                detector_type = ProgressDetectorType.ACTION_CYCLE
                repetitions = cycle_repetitions
            elif state.stagnant_steps >= self.policy.max_stagnant_steps:
                detector_type = ProgressDetectorType.STATE_STAGNATION
                repetitions = state.stagnant_steps

        if detector_type is None:
            return ProgressDecision(ProgressDecisionType.CONTINUE)

        state.detected = True
        state.detector_type = detector_type.value
        state.cycle_length = cycle_length
        state.repetition_count = repetitions
        if state.recovery_attempts < self.policy.max_recovery_attempts:
            state.recovery_attempts += 1
            state.recovery_signal_pending = True
            self._reset_detection_window(state)
            return ProgressDecision(
                ProgressDecisionType.RECOVER,
                detector_type,
                repetitions,
                cycle_length,
            )
        state.recovery_signal_pending = False
        return ProgressDecision(
            ProgressDecisionType.TERMINATE,
            detector_type,
            repetitions,
            cycle_length,
        )

    def mark_recovery_delivered(self, state: ProgressState) -> None:
        state.recovery_signal_pending = False

    @staticmethod
    def _reset_detection_window(state: ProgressState) -> None:
        state.action_history.clear()
        state.error_history.clear()
        state.action_repeat_count = 0
        state.error_repeat_count = 0
        state.stagnant_steps = 0
        state.last_action_fingerprint = None
        state.last_error_fingerprint = None


def action_fingerprint(tool_name: str, arguments: dict[str, Any]) -> str:
    return stable_fingerprint(
        {
            "kind": "tool",
            "tool": tool_name.strip(),
            "arguments": _canonical_value(arguments),
        }
    )


def error_fingerprint(
    error_type: str,
    message: str,
    *,
    tool_name: str | None = None,
    category: str | None = None,
) -> str:
    return stable_fingerprint(
        {
            "type": error_type.strip().casefold(),
            "tool": (tool_name or "").strip().casefold(),
            "category": (category or "").strip().casefold(),
            "message": normalize_error_message(message),
        }
    )


def state_fingerprint(value: dict[str, Any]) -> str:
    return stable_fingerprint(_canonical_value(value))


def evidence_fingerprint(content: str) -> str:
    bounded = " ".join(content.split())[:2_000]
    return stable_fingerprint({"evidence": bounded})


def normalize_error_message(message: str) -> str:
    value = message[:2_000].casefold()
    value = re.sub(r"\b\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?z?\b", "<time>", value)
    value = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "<uuid>",
        value,
    )
    value = re.sub(r"\b0x[0-9a-f]+\b", "<address>", value)
    value = re.sub(r"\b(?:request|trace|run|turn|call)[-_ ]?id[=: ]+[a-z0-9_-]+", "<id>", value)
    value = re.sub(r"\b\d{10,}\b", "<number>", value)
    return " ".join(value.split())[:_MAX_ERROR_CHARS]


def stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def recovery_message(state: ProgressState) -> str:
    reason = state.detector_type or "stagnation"
    return (
        "[Runtime recovery signal: derived model context]\n"
        "Previous actions are not producing observable progress "
        f"({reason}). Do not repeat the same action. Reassess assumptions, "
        "use existing evidence, and choose a materially different approach."
    )


def _detect_cycle(history: list[str], required_repetitions: int) -> tuple[int | None, int]:
    for length in range(2, 5):
        needed = length * required_repetitions
        if len(history) < needed:
            continue
        tail = history[-needed:]
        unit = tail[:length]
        if all(tail[index : index + length] == unit for index in range(0, needed, length)):
            return length, required_repetitions
    return None, 0


def _append_bounded(values: list[str], value: str, limit: int) -> None:
    values.append(value)
    if len(values) > limit:
        del values[: len(values) - limit]


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return str(value)


def _strings(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None
