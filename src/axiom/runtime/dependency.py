from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from axiom.config import DependencyConfig

DEPENDENCY_DEADLINE_EXCEEDED = "DEPENDENCY_DEADLINE_EXCEEDED"
DEPENDENCY_RETRY_EXHAUSTED = "DEPENDENCY_RETRY_EXHAUSTED"
DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"


class DependencyFailureCategory(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    TRANSIENT_SERVER_ERROR = "transient_server_error"
    CONNECTION_ERROR = "connection_error"
    VALIDATION_ERROR = "validation_error"
    AUTH_ERROR = "auth_error"
    POLICY_DENIED = "policy_denied"
    PERMANENT_ERROR = "permanent_error"
    UNKNOWN = "unknown"


class RetrySafety(StrEnum):
    SAFE = "safe"
    IDEMPOTENT = "idempotent"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class OperationDeadline:
    configured_timeout_seconds: float
    remaining_run_seconds: float | None = None

    @property
    def effective_timeout_seconds(self) -> float:
        configured = max(0.0, float(self.configured_timeout_seconds))
        if self.remaining_run_seconds is None:
            return configured
        return min(configured, max(0.0, float(self.remaining_run_seconds)))

    @property
    def exhausted(self) -> bool:
        return self.effective_timeout_seconds <= 0


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0
    jitter_enabled: bool = True

    def __post_init__(self) -> None:
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must be non-negative")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must be non-negative")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be at least base_delay_seconds")

    def delay_seconds(
        self,
        failure_number: int,
        *,
        random_source: Callable[[], float],
    ) -> float:
        upper = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2 ** max(0, failure_number - 1)),
        )
        if not self.jitter_enabled:
            return upper
        sample = min(1.0, max(0.0, float(random_source())))
        return upper * sample


@dataclass(frozen=True, slots=True)
class RetryDecision:
    retry: bool
    category: DependencyFailureCategory
    reason: str
    delay_seconds: float = 0.0
    exhausted: bool = False
    blocked_by_deadline: bool = False
    unsafe_suppressed: bool = False


RetryableError = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded per-operation retry policy.

    ``backoff_seconds`` is retained as a compatibility alias for the old fixed-delay
    policy. When supplied it becomes the exponential base and cap.
    """

    max_attempts: int = 3
    base_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 4.0
    jitter_enabled: bool = True
    retryable_error: RetryableError | None = None
    backoff_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        base = self._base_delay
        maximum = self._max_delay
        BackoffPolicy(base, maximum, self.jitter_enabled)

    @classmethod
    def from_config(cls, config: DependencyConfig) -> RetryPolicy:
        return cls(
            max_attempts=config.max_attempts,
            base_backoff_seconds=config.base_backoff_seconds,
            max_backoff_seconds=config.max_backoff_seconds,
            jitter_enabled=config.jitter_enabled,
        )

    @property
    def _base_delay(self) -> float:
        return (
            self.backoff_seconds
            if self.backoff_seconds is not None
            else self.base_backoff_seconds
        )

    @property
    def _max_delay(self) -> float:
        return (
            self.backoff_seconds
            if self.backoff_seconds is not None
            else self.max_backoff_seconds
        )

    def can_retry(self, error: str, attempt: int) -> bool:
        """Compatibility helper; Runtime decisions additionally require a safe category."""
        if attempt >= self.max_attempts:
            return False
        return self.retryable_error(error) if self.retryable_error else True

    def decide(
        self,
        *,
        category: DependencyFailureCategory,
        attempt: int,
        safety: RetrySafety,
        error: str,
        random_source: Callable[[], float],
        remaining_seconds: float | None = None,
        retry_after_seconds: float | None = None,
    ) -> RetryDecision:
        if category not in _RETRYABLE_CATEGORIES:
            return RetryDecision(False, category, f"{category.value} is not retryable")
        if safety == RetrySafety.UNSAFE:
            return RetryDecision(
                False,
                category,
                "operation is not safe to retry after an ambiguous failure",
                unsafe_suppressed=True,
            )
        if self.retryable_error is not None and not self.retryable_error(error):
            return RetryDecision(False, category, "custom retry classifier rejected the failure")
        if attempt >= self.max_attempts:
            return RetryDecision(
                False,
                category,
                (
                    "retry allowance exhausted"
                    if self.max_attempts > 1
                    else "retry is disabled for this operation"
                ),
                exhausted=self.max_attempts > 1,
            )
        delay = BackoffPolicy(
            self._base_delay,
            self._max_delay,
            self.jitter_enabled,
        ).delay_seconds(attempt, random_source=random_source)
        if retry_after_seconds is not None:
            delay = max(delay, max(0.0, retry_after_seconds))
        if remaining_seconds is not None and delay >= max(0.0, remaining_seconds):
            return RetryDecision(
                False,
                category,
                "retry backoff cannot fit inside the remaining Run deadline",
                delay_seconds=delay,
                blocked_by_deadline=True,
            )
        return RetryDecision(True, category, "bounded retry allowed", delay_seconds=delay)


class RetryClassifier:
    def classify(self, error: BaseException | str | dict[str, Any]) -> DependencyFailureCategory:
        if isinstance(error, dict):
            explicit = error.get("failure_category") or error.get("category")
            if explicit:
                try:
                    return DependencyFailureCategory(str(explicit))
                except ValueError:
                    pass
            status = _optional_status(error.get("status_code"))
            if status is not None:
                return _classify_status(status)
            return self._classify_text(str(error.get("error_type") or error.get("error") or ""))

        if isinstance(error, BaseException):
            current: BaseException | None = error
            seen: set[int] = set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                if isinstance(current, (asyncio.TimeoutError, TimeoutError)):
                    return DependencyFailureCategory.TIMEOUT
                if isinstance(current, ConnectionError):
                    return DependencyFailureCategory.CONNECTION_ERROR
                status = _status_code(current)
                if status is not None:
                    return _classify_status(status)
                current = current.__cause__ or current.__context__
            return self._classify_text(str(error))
        return self._classify_text(str(error))

    def retry_after_seconds(self, error: BaseException | dict[str, Any]) -> float | None:
        if isinstance(error, dict):
            return _optional_positive_float(error.get("retry_after_seconds"))
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            response = getattr(current, "response", None)
            headers = getattr(response, "headers", None)
            if headers is not None:
                value = headers.get("retry-after") or headers.get("Retry-After")
                parsed = _optional_positive_float(value)
                if parsed is not None:
                    return parsed
            current = current.__cause__ or current.__context__
        return None

    @staticmethod
    def _classify_text(message: str) -> DependencyFailureCategory:
        normalized = message.casefold()
        if "timed out" in normalized or "timeout" in normalized:
            return DependencyFailureCategory.TIMEOUT
        if "rate limit" in normalized or "too many requests" in normalized:
            return DependencyFailureCategory.RATE_LIMITED
        if (
            "connectionerror" in normalized
            or "connection reset" in normalized
            or "connection refused" in normalized
        ):
            return DependencyFailureCategory.CONNECTION_ERROR
        if any(
            marker in normalized
            for marker in ("unauthorized", "authentication", "invalid api key")
        ):
            return DependencyFailureCategory.AUTH_ERROR
        if "policy" in normalized and ("denied" in normalized or "rejected" in normalized):
            return DependencyFailureCategory.POLICY_DENIED
        if any(
            marker in normalized
            for marker in (
                "validation",
                "invalid request",
                "context too large",
                "unsupported model",
            )
        ):
            return DependencyFailureCategory.VALIDATION_ERROR
        return DependencyFailureCategory.UNKNOWN


def _classify_status(status: int) -> DependencyFailureCategory:
    if status == 429:
        return DependencyFailureCategory.RATE_LIMITED
    if status in {408, 504}:
        return DependencyFailureCategory.TIMEOUT
    if status in {500, 502, 503}:
        return DependencyFailureCategory.TRANSIENT_SERVER_ERROR
    if status in {401, 403}:
        return DependencyFailureCategory.AUTH_ERROR
    if status in {400, 404, 409, 413, 422}:
        return DependencyFailureCategory.VALIDATION_ERROR
    return DependencyFailureCategory.PERMANENT_ERROR


def _status_code(error: BaseException) -> int | None:
    direct = _optional_status(getattr(error, "status_code", None))
    if direct is not None:
        return direct
    return _optional_status(getattr(getattr(error, "response", None), "status_code", None))


def _optional_status(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 100 <= parsed <= 599 else None


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


_RETRYABLE_CATEGORIES = frozenset(
    {
        DependencyFailureCategory.TIMEOUT,
        DependencyFailureCategory.RATE_LIMITED,
        DependencyFailureCategory.TRANSIENT_SERVER_ERROR,
        DependencyFailureCategory.CONNECTION_ERROR,
    }
)
