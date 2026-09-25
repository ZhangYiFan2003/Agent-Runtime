import { z } from "zod";

/**
 * DTO for `GET /v1/runs/{run_id}/metrics` (RunMetrics, 404 when no trace).
 * Field set mirrors `RunMetrics.to_dict()` in the runtime. Aggregate
 * dicts stay open records so backend extensions do not break parsing.
 */
const numberOrNull = z.number().nullable();
const intOrNull = z.number().int().nullable();
const stringOrNull = z.string().nullable();
const boolOrNull = z.boolean().nullable();
const openRecord = z.record(z.string(), z.unknown());
const numberRecord = z.record(z.string(), z.number());

export const runMetricsSchema = z
  .object({
    trace_id: z.string(),
    run_id: z.string(),
    status: z.string(),
    duration_ms: numberOrNull,
    step_count: z.number().int(),
    llm_calls: z.number().int(),
    tool_calls: z.number().int(),
    prompt_tokens: z.number().int(),
    completion_tokens: z.number().int(),
    total_tokens: z.number().int(),
    tool_successes: z.number().int(),
    tool_failures: z.number().int(),
    tool_success_rate: numberOrNull,
    checkpoint_count: z.number().int(),
    interrupt_count: z.number().int(),
    resume_count: z.number().int(),
    retry_count: z.number().int(),
    retried_model_calls: intOrNull,
    retried_tool_calls: intOrNull,
    dependency_timeouts: intOrNull,
    rate_limit_failures: intOrNull,
    retry_exhausted_count: intOrNull,
    retry_backoff_ms: z.number().nullable(),
    cached_input_tokens: z.number().int(),
    reasoning_tokens: z.number().int(),
    cost_usd: z.union([z.string(), z.number()]).nullable(),
    cost_known: z.boolean(),
    elapsed_seconds: z.number().nullable(),
    budget_policy: openRecord,
    budget_usage: openRecord,
    aggregate_budget_usage: openRecord,
    budget_remaining: openRecord,
    aggregate_budget_remaining: openRecord,
    budget_utilization: numberRecord,
    aggregate_budget_utilization: numberRecord,
    budget_soft_limit_reached: z.boolean(),
    budget_hard_limit_reached: z.boolean(),
    budget_exceeded_dimension: stringOrNull,
    progress_action_repeat_count: z.number().int(),
    progress_error_repeat_count: z.number().int(),
    progress_stagnant_steps: z.number().int(),
    progress_detected: z.boolean(),
    progress_detector_type: stringOrNull,
    progress_cycle_length: intOrNull,
    progress_recovery_attempts: z.number().int(),
    progress_last_progress_step: z.number().int(),
    completion_verified: boolOrNull,
    verification_status: z.string(),
    verification_attempts: z.number().int(),
    verification_failed_checks: z.array(z.string()),
  })
  .catchall(z.unknown());

export type RunMetricsDto = z.infer<typeof runMetricsSchema>;
