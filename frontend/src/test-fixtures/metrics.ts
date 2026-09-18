import { runMetricsSchema } from "../api/dto/metrics";
import { adaptRunMetrics, type RunMetrics } from "../api/adapters/metrics";

/** Minimal valid RunMetrics DTO fixture — mirrors `RunMetrics.to_dict()`. */
export function makeRunMetricsDto(overrides: Record<string, unknown> = {}) {
  return {
    trace_id: "trace_1",
    run_id: "run_abc123",
    status: "COMPLETED",
    duration_ms: 5000,
    step_count: 4,
    llm_calls: 3,
    tool_calls: 2,
    prompt_tokens: 6200,
    completion_tokens: 1980,
    total_tokens: 8180,
    tool_successes: 2,
    tool_failures: 0,
    tool_success_rate: 1,
    checkpoint_count: 9,
    interrupt_count: 0,
    resume_count: 0,
    retry_count: 0,
    retried_model_calls: 0,
    retried_tool_calls: 0,
    dependency_timeouts: 0,
    rate_limit_failures: 0,
    retry_exhausted_count: 0,
    retry_backoff_ms: 0,
    cached_input_tokens: 0,
    reasoning_tokens: 0,
    cost_usd: null,
    cost_known: false,
    elapsed_seconds: 5,
    budget_policy: {},
    budget_usage: {},
    aggregate_budget_usage: {},
    budget_remaining: {},
    aggregate_budget_remaining: {},
    budget_utilization: {},
    aggregate_budget_utilization: {},
    budget_soft_limit_reached: false,
    budget_hard_limit_reached: false,
    budget_exceeded_dimension: null,
    progress_action_repeat_count: 0,
    progress_error_repeat_count: 0,
    progress_stagnant_steps: 0,
    progress_detected: false,
    progress_detector_type: null,
    progress_cycle_length: null,
    progress_recovery_attempts: 0,
    progress_last_progress_step: 0,
    completion_verified: null,
    verification_status: "not_applicable",
    verification_attempts: 0,
    verification_failed_checks: [],
    ...overrides,
  };
}

/** Adapted view-model fixture for metrics-grouping tests. */
export function makeRunMetrics(overrides: Record<string, unknown> = {}): RunMetrics {
  return adaptRunMetrics(runMetricsSchema.parse(makeRunMetricsDto(overrides)));
}
