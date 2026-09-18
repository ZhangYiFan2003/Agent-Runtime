import type { Span } from "../api/adapters/trace";

/**
 * Pure waterfall geometry: trace window computation and per-span bar
 * positions. Percentages are clamped to 0–100; zero-length windows and
 * malformed timestamps never produce NaN or fabricated latency — a
 * zero-width bar relies on CSS `min-width` to stay clickable.
 */

export interface TraceWindow {
  startMs: number;
  endMs: number;
  durationMs: number;
}

export interface SpanBarGeometry {
  leftPct: number;
  widthPct: number;
  /** Span has no endedAt — still in flight; `now` was used as the end. */
  openEnded: boolean;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function spanEndMs(span: Span, now: number): number | null {
  if (span.endedAt === null) return now;
  const end = Date.parse(span.endedAt);
  return Number.isNaN(end) ? null : end;
}

/**
 * Trace window: min(span.startedAt) → max(span.endedAt ?? now).
 * Returns null when no span has a parseable start time.
 */
export function computeTraceWindow(spans: readonly Span[], now: number): TraceWindow | null {
  let start = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  for (const span of spans) {
    const s = Date.parse(span.startedAt);
    if (Number.isNaN(s)) continue;
    const e = spanEndMs(span, now) ?? s;
    if (s < start) start = s;
    if (e > end) end = e;
  }
  if (!Number.isFinite(start)) return null;
  if (end < start) end = start;
  return { startMs: start, endMs: end, durationMs: end - start };
}

/**
 * Bar position for one span inside the window. Returns null for spans with
 * an unparseable start time (they render label-only, no bar).
 */
export function spanBarGeometry(
  span: Span,
  window: TraceWindow,
  now: number,
): SpanBarGeometry | null {
  const s = Date.parse(span.startedAt);
  if (Number.isNaN(s)) return null;
  const openEnded = span.endedAt === null;
  let e = spanEndMs(span, now) ?? s;
  if (e < s) e = s;
  if (window.durationMs <= 0) return { leftPct: 0, widthPct: 0, openEnded };
  const leftPct = clamp(((s - window.startMs) / window.durationMs) * 100, 0, 100);
  const widthPct = clamp(((e - s) / window.durationMs) * 100, 0, 100 - leftPct);
  return { leftPct, widthPct, openEnded };
}
