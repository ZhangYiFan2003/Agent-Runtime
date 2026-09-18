import type { TraceDto, SpanDto } from "../dto/trace";

/** Adapter: trace/span DTOs → camelCase view models. Span type and status
 *  stay raw strings (open enums); known kinds get a group for rendering. */
export const KNOWN_SPAN_TYPES = [
  "agent",
  "llm",
  "tool",
  "checkpoint",
  "interrupt",
  "policy",
  "verification",
] as const;

export type SpanKind = (typeof KNOWN_SPAN_TYPES)[number] | "other";

export function classifySpanType(spanType: string): SpanKind {
  return (KNOWN_SPAN_TYPES as readonly string[]).includes(spanType)
    ? (spanType as SpanKind)
    : "other";
}

export interface Trace {
  traceId: string;
  runId: string;
  threadId: string;
  turnId: string;
  startedAt: string;
  endedAt: string | null;
  status: string;
  totalLatencyMs: number | null;
}

export interface Span {
  spanId: string;
  traceId: string;
  parentSpanId: string | null;
  spanType: string;
  spanKind: SpanKind;
  name: string;
  startedAt: string;
  endedAt: string | null;
  status: string;
  latencyMs: number | null;
  attributes: Record<string, unknown>;
}

export function adaptTrace(dto: TraceDto): Trace {
  return {
    traceId: dto.trace_id,
    runId: dto.run_id,
    threadId: dto.thread_id,
    turnId: dto.turn_id,
    startedAt: dto.started_at,
    endedAt: dto.ended_at,
    status: dto.status,
    totalLatencyMs: dto.total_latency_ms,
  };
}

export function adaptSpan(dto: SpanDto): Span {
  return {
    spanId: dto.span_id,
    traceId: dto.trace_id,
    parentSpanId: dto.parent_span_id,
    spanType: dto.span_type,
    spanKind: classifySpanType(dto.span_type),
    name: dto.name,
    startedAt: dto.started_at,
    endedAt: dto.ended_at,
    status: dto.status,
    latencyMs: dto.latency_ms,
    attributes: dto.attributes,
  };
}
