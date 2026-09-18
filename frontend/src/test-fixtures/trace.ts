import { spanSchema, traceSchema } from "../api/dto/trace";
import { adaptSpan, adaptTrace, type Span, type Trace } from "../api/adapters/trace";

/** Minimal valid span DTO fixture. Tests override fields on top. */
export function makeSpanDto(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    span_id: "span_1",
    trace_id: "trace_1",
    parent_span_id: null,
    span_type: "agent",
    name: "agent.run",
    started_at: "2026-09-17T10:00:00.000+00:00",
    ended_at: "2026-09-17T10:00:04.000+00:00",
    status: "SUCCEEDED",
    latency_ms: 4000,
    attributes: {},
    ...overrides,
  };
}

export function makeTraceDto(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    trace_id: "trace_1",
    run_id: "run_abc123",
    thread_id: "thread_1",
    turn_id: "turn_1",
    started_at: "2026-09-17T10:00:00.000+00:00",
    ended_at: "2026-09-17T10:00:05.000+00:00",
    status: "SUCCEEDED",
    total_latency_ms: 5000,
    ...overrides,
  };
}

/** Adapted view-model fixtures for view-layer tests (tree/geometry). */
export function makeSpan(overrides: Record<string, unknown> = {}): Span {
  return adaptSpan(spanSchema.parse(makeSpanDto(overrides)));
}

export function makeTrace(overrides: Record<string, unknown> = {}): Trace {
  return adaptTrace(traceSchema.parse(makeTraceDto(overrides)));
}
