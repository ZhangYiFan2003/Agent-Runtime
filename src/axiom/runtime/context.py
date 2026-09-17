"""Compatibility exports for the runtime context-management API."""

from axiom.context import (
    CONTEXT_BUDGET_EXCEEDED,
    DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW,
    ApproximateTokenEstimator,
    ContextBudget,
    ContextBudgetExceededError,
    ContextBudgetPolicy,
    ContextBuilder,
    ContextBuildResult,
    ContextCompactionResult,
    ContextManager,
    RuntimeContextSummary,
    TokenEstimator,
    apply_compaction_to_strategy_state,
    compaction_count_from_strategy_state,
    context_policy_from_config,
    summary_from_strategy_state,
)

__all__ = [
    "CONTEXT_BUDGET_EXCEEDED",
    "DEFAULT_UNKNOWN_MODEL_CONTEXT_WINDOW",
    "ApproximateTokenEstimator",
    "ContextBudget",
    "ContextBudgetExceededError",
    "ContextBudgetPolicy",
    "ContextBuilder",
    "ContextBuildResult",
    "ContextCompactionResult",
    "ContextManager",
    "RuntimeContextSummary",
    "TokenEstimator",
    "apply_compaction_to_strategy_state",
    "compaction_count_from_strategy_state",
    "context_policy_from_config",
    "summary_from_strategy_state",
]
