import type { RunMetricsDto } from "../dto/metrics";

/**
 * Adapter: RunMetrics DTO (snake_case) → camelCase view model.
 * Aggregate records stay open (`Record<string, unknown>`) so backend
 * extensions flow through without adapter changes.
 */
export interface RunMetrics {
  traceId: string;
  runId: string;
  status: string;
  durationMs: number | null;
  stepCount: number;
  llmCalls: number;
  toolCalls: number;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  toolSuccesses: number;
  toolFailures: number;
  toolSuccessRate: number | null;
  checkpointCount: number;
  interruptCount: number;
  resumeCount: number;
  retryCount: number;
  retriedModelCalls: number | null;
  retriedToolCalls: number | null;
  dependencyTimeouts: number | null;
  rateLimitFailures: number | null;
  retryExhaustedCount: number | null;
  retryBackoffMs: number | null;
  cachedInputTokens: number;
  reasoningTokens: number;
  costUsd: number | null;
  costKnown: boolean;
  elapsedSeconds: number | null;
  budgetPolicy: Record<string, unknown>;
  budgetUsage: Record<string, unknown>;
  aggregateBudgetUsage: Record<string, unknown>;
  budgetRemaining: Record<string, unknown>;
  aggregateBudgetRemaining: Record<string, unknown>;
  budgetUtilization: Record<string, number>;
  aggregateBudgetUtilization: Record<string, number>;
  budgetSoftLimitReached: boolean;
  budgetHardLimitReached: boolean;
  budgetExceededDimension: string | null;
  progressActionRepeatCount: number;
  progressErrorRepeatCount: number;
  progressStagnantSteps: number;
  progressDetected: boolean;
  progressDetectorType: string | null;
  progressCycleLength: number | null;
  progressRecoveryAttempts: number;
  progressLastProgressStep: number;
  completionVerified: boolean | null;
  verificationStatus: string;
  verificationAttempts: number;
  verificationFailedChecks: string[];
}

export function adaptRunMetrics(dto: RunMetricsDto): RunMetrics {
  const parsedCostUsd = dto.cost_usd === null ? null : Number(dto.cost_usd);

  return {
    traceId: dto.trace_id,
    runId: dto.run_id,
    status: dto.status,
    durationMs: dto.duration_ms,
    stepCount: dto.step_count,
    llmCalls: dto.llm_calls,
    toolCalls: dto.tool_calls,
    promptTokens: dto.prompt_tokens,
    completionTokens: dto.completion_tokens,
    totalTokens: dto.total_tokens,
    toolSuccesses: dto.tool_successes,
    toolFailures: dto.tool_failures,
    toolSuccessRate: dto.tool_success_rate,
    checkpointCount: dto.checkpoint_count,
    interruptCount: dto.interrupt_count,
    resumeCount: dto.resume_count,
    retryCount: dto.retry_count,
    retriedModelCalls: dto.retried_model_calls,
    retriedToolCalls: dto.retried_tool_calls,
    dependencyTimeouts: dto.dependency_timeouts,
    rateLimitFailures: dto.rate_limit_failures,
    retryExhaustedCount: dto.retry_exhausted_count,
    retryBackoffMs: dto.retry_backoff_ms,
    cachedInputTokens: dto.cached_input_tokens,
    reasoningTokens: dto.reasoning_tokens,
    costUsd: parsedCostUsd !== null && Number.isFinite(parsedCostUsd) ? parsedCostUsd : null,
    costKnown: dto.cost_known,
    elapsedSeconds: dto.elapsed_seconds,
    budgetPolicy: dto.budget_policy,
    budgetUsage: dto.budget_usage,
    aggregateBudgetUsage: dto.aggregate_budget_usage,
    budgetRemaining: dto.budget_remaining,
    aggregateBudgetRemaining: dto.aggregate_budget_remaining,
    budgetUtilization: dto.budget_utilization,
    aggregateBudgetUtilization: dto.aggregate_budget_utilization,
    budgetSoftLimitReached: dto.budget_soft_limit_reached,
    budgetHardLimitReached: dto.budget_hard_limit_reached,
    budgetExceededDimension: dto.budget_exceeded_dimension,
    progressActionRepeatCount: dto.progress_action_repeat_count,
    progressErrorRepeatCount: dto.progress_error_repeat_count,
    progressStagnantSteps: dto.progress_stagnant_steps,
    progressDetected: dto.progress_detected,
    progressDetectorType: dto.progress_detector_type,
    progressCycleLength: dto.progress_cycle_length,
    progressRecoveryAttempts: dto.progress_recovery_attempts,
    progressLastProgressStep: dto.progress_last_progress_step,
    completionVerified: dto.completion_verified,
    verificationStatus: dto.verification_status,
    verificationAttempts: dto.verification_attempts,
    verificationFailedChecks: dto.verification_failed_checks,
  };
}
