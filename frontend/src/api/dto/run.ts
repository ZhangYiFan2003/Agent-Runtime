import { z } from "zod";

/**
 * DTOs for Run projections: `GET /v1/runs`, `GET /v1/runs/{id}`,
 * `GET /v1/runs/{id}/children`.
 *
 * Status / strategy / run_kind are deliberately typed as plain strings:
 * the backend enums are open sets, and an unknown member must never fail
 * parsing. Classification into known groups happens in the adapter layer.
 */

export const interruptSummarySchema = z
  .object({
    run_id: z.string(),
    invocation_id: z.string(),
    interrupt_type: z.string(),
    tool_name: z.string().nullable(),
    reason: z.string(),
    created_at: z.string(),
  })
  .catchall(z.unknown());

export const runErrorSchema = z
  .object({
    type: z.string(),
    message: z.string(),
    step: z.string().nullable().optional(),
    metadata: z.record(z.string(), z.unknown()).optional(),
  })
  .catchall(z.unknown());

export const runViewSchema = z
  .object({
    run_id: z.string(),
    thread_id: z.string(),
    turn_id: z.string().nullable(),
    run_kind: z.string(),
    execution_strategy: z.string(),
    status: z.string(),
    waiting_reason: z.string().nullable(),
    recovery_action: z.string().nullable(),
    parent_run_id: z.string().nullable(),
    parent_step_id: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
    started_at: z.string(),
    completed_at: z.string().nullable(),
    output: z.string().nullable(),
    error: runErrorSchema.nullable(),
    interrupt: interruptSummarySchema.nullable(),
    children_count: z.number().int(),
    active_children_count: z.number().int(),
    waiting_children_count: z.number().int(),
    terminal_children_count: z.number().int(),
    active_child_run_ids: z.array(z.string()),
    pending_interrupts: z.array(interruptSummarySchema),
    assignment: z.record(z.string(), z.unknown()).nullable(),
    allowed_operations: z.array(z.string()),
  })
  .catchall(z.unknown());

export const runListSchema = z.object({
  runs: z.array(runViewSchema),
});

export const childRunSchema = z
  .object({
    child_run_id: z.string(),
    run_kind: z.string(),
    status: z.string(),
    parent_run_id: z.string().nullable(),
    parent_step_id: z.string().nullable(),
    assignment_id: z.string().nullable(),
    worker_role: z.string().nullable(),
    attempt: z.number().int().nullable(),
    interrupt: interruptSummarySchema.nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .catchall(z.unknown());

export const childrenResponseSchema = z.object({
  parent_run_id: z.string(),
  children: z.array(childRunSchema),
});

export type InterruptSummaryDto = z.infer<typeof interruptSummarySchema>;
export type RunErrorDto = z.infer<typeof runErrorSchema>;
export type RunViewDto = z.infer<typeof runViewSchema>;
export type RunListDto = z.infer<typeof runListSchema>;
export type ChildRunDto = z.infer<typeof childRunSchema>;
export type ChildrenResponseDto = z.infer<typeof childrenResponseSchema>;
