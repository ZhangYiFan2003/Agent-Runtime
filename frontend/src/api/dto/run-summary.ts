import { z } from "zod";

/** List rows deliberately exclude full checkpoint, output and control state. */
export const runSummarySchema = z.object({
  run_id: z.string().min(1),
  thread_id: z.string().min(1),
  turn_id: z.string().nullable(),
  run_kind: z.string(),
  execution_strategy: z.string(),
  status: z.string(),
  parent_run_id: z.string().nullable(),
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
  started_at: z.string(),
  completed_at: z.string().nullable(),
  children_count: z.number().int().nonnegative(),
  active_children_count: z.number().int().nonnegative(),
}).catchall(z.unknown());

export const runSummaryPageSchema = z.object({
  runs: z.array(runSummarySchema),
  next_cursor: z.string().min(1).nullable(),
  has_more: z.boolean(),
}).catchall(z.unknown()).refine(
  (page) => page.has_more === (page.next_cursor !== null) &&
    (!page.has_more || page.runs.length > 0),
  { message: "Invalid Run page cursor boundary" },
);

export type RunSummaryDto = z.infer<typeof runSummarySchema>;
