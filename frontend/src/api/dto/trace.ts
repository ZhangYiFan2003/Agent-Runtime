import { z } from "zod";

/**
 * DTOs for `GET /v1/runs/{run_id}/trace` → `{trace, spans}`.
 * `span_type` / `status` are open enums → plain strings, classified later
 * by adapters. Span attributes are fully forward-compatible.
 */
export const traceSchema = z
  .object({
    schema_version: z.number().int(),
    trace_id: z.string(),
    run_id: z.string(),
    thread_id: z.string(),
    turn_id: z.string(),
    started_at: z.string(),
    ended_at: z.string().nullable(),
    status: z.string(),
    total_latency_ms: z.number().nullable(),
  })
  .catchall(z.unknown());

export const spanSchema = z
  .object({
    schema_version: z.number().int(),
    span_id: z.string(),
    trace_id: z.string(),
    parent_span_id: z.string().nullable(),
    span_type: z.string(),
    name: z.string(),
    started_at: z.string(),
    ended_at: z.string().nullable(),
    status: z.string(),
    latency_ms: z.number().nullable(),
    attributes: z.record(z.string(), z.unknown()),
  })
  .catchall(z.unknown());

export const traceResponseSchema = z.object({
  trace: traceSchema,
  spans: z.array(spanSchema),
});

export type TraceDto = z.infer<typeof traceSchema>;
export type SpanDto = z.infer<typeof spanSchema>;
export type TraceResponseDto = z.infer<typeof traceResponseSchema>;
