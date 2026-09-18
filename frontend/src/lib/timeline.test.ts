import { describe, expect, it } from "vitest";
import { computeTraceWindow, spanBarGeometry } from "./timeline";
import { makeSpan } from "../test-fixtures/trace";

const T0 = Date.parse("2026-09-17T10:00:00.000+00:00");
const iso = (ms: number) => new Date(T0 + ms).toISOString();
const NOW = T0 + 10_000;

describe("computeTraceWindow", () => {
  it("spans min(startedAt) → max(endedAt)", () => {
    const window = computeTraceWindow(
      [
        makeSpan({ started_at: iso(1000), ended_at: iso(4000) }),
        makeSpan({ started_at: iso(500), ended_at: iso(2000) }),
      ],
      NOW,
    );
    expect(window).toEqual({ startMs: T0 + 500, endMs: T0 + 4000, durationMs: 3500 });
  });

  it("uses now for open spans", () => {
    const window = computeTraceWindow(
      [makeSpan({ started_at: iso(0), ended_at: null, latency_ms: null })],
      NOW,
    );
    expect(window?.endMs).toBe(NOW);
    expect(window?.durationMs).toBe(10_000);
  });

  it("returns null when no span has a parseable start", () => {
    expect(computeTraceWindow([], NOW)).toBeNull();
    expect(computeTraceWindow([makeSpan({ started_at: "garbage" })], NOW)).toBeNull();
  });

  it("ignores malformed rows but keeps valid ones", () => {
    const window = computeTraceWindow(
      [makeSpan({ started_at: "garbage" }), makeSpan({ started_at: iso(100), ended_at: iso(900) })],
      NOW,
    );
    expect(window?.startMs).toBe(T0 + 100);
  });

  it("clamps an end before the start (clock skew) to a zero window", () => {
    const window = computeTraceWindow(
      [makeSpan({ started_at: iso(5000), ended_at: iso(1000) })],
      NOW,
    );
    expect(window?.durationMs).toBe(0);
  });
});

describe("spanBarGeometry", () => {
  const window = { startMs: T0, endMs: T0 + 10_000, durationMs: 10_000 };

  it("computes clamped percentages", () => {
    const bar = spanBarGeometry(
      makeSpan({ started_at: iso(2000), ended_at: iso(7000) }),
      window,
      NOW,
    );
    expect(bar).toEqual({ leftPct: 20, widthPct: 50, openEnded: false });
  });

  it("marks open spans and sizes them up to now", () => {
    const bar = spanBarGeometry(
      makeSpan({ started_at: iso(8000), ended_at: null, latency_ms: null }),
      window,
      NOW,
    );
    expect(bar?.openEnded).toBe(true);
    expect(bar?.leftPct).toBe(80);
    expect(bar?.widthPct).toBe(20);
  });

  it("returns zero width inside a zero-duration window instead of NaN", () => {
    const zero = { startMs: T0, endMs: T0, durationMs: 0 };
    const bar = spanBarGeometry(makeSpan({ started_at: iso(0), ended_at: iso(0) }), zero, NOW);
    expect(bar).toEqual({ leftPct: 0, widthPct: 0, openEnded: false });
  });

  it("clamps spans extending past the window", () => {
    const bar = spanBarGeometry(makeSpan({ started_at: iso(-5000), ended_at: iso(20000) }), window, NOW);
    expect(bar?.leftPct).toBe(0);
    expect(bar?.widthPct).toBe(100);
  });

  it("returns null for an unparseable start", () => {
    expect(spanBarGeometry(makeSpan({ started_at: "garbage" }), window, NOW)).toBeNull();
  });

  it("clamps negative duration (end before start) to zero width", () => {
    const bar = spanBarGeometry(makeSpan({ started_at: iso(3000), ended_at: iso(1000) }), window, NOW);
    expect(bar?.leftPct).toBe(30);
    expect(bar?.widthPct).toBe(0);
  });
});
