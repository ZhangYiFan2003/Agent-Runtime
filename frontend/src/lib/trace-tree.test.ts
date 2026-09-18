import { describe, expect, it } from "vitest";
import { buildSpanTree, flattenSpanTree } from "./trace-tree";
import { makeSpan } from "../test-fixtures/trace";

const T = "2026-09-17T10:00:00.000+00:00";
const later = (seconds: number) =>
  new Date(Date.parse(T) + seconds * 1000).toISOString();

describe("buildSpanTree", () => {
  it("nests children by parent_span_id and sorts by started_at", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "llm2", parent_span_id: "root", started_at: later(3) }),
      makeSpan({ span_id: "root", started_at: T }),
      makeSpan({ span_id: "llm1", parent_span_id: "root", started_at: later(1) }),
    ]);
    expect(roots).toHaveLength(1);
    expect(roots[0].span.spanId).toBe("root");
    expect(roots[0].children.map((c) => c.span.spanId)).toEqual(["llm1", "llm2"]);
  });

  it("supports arbitrary depth", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "a", started_at: T }),
      makeSpan({ span_id: "b", parent_span_id: "a", started_at: later(1) }),
      makeSpan({ span_id: "c", parent_span_id: "b", started_at: later(2) }),
    ]);
    expect(roots[0].children[0].children[0].span.spanId).toBe("c");
    expect(roots[0].children[0].children[0].depth).toBe(2);
  });

  it("treats spans with a missing parent as orphan roots", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "a", started_at: T }),
      makeSpan({ span_id: "orphan", parent_span_id: "span_gone", started_at: later(1) }),
    ]);
    expect(roots.map((r) => r.span.spanId)).toEqual(["a", "orphan"]);
  });

  it("accepts open spans (ended_at null)", () => {
    const roots = buildSpanTree([makeSpan({ span_id: "open", ended_at: null, latency_ms: null })]);
    expect(roots).toHaveLength(1);
    expect(roots[0].span.endedAt).toBeNull();
  });

  it("keeps input order for equal timestamps (stable sort)", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "first", started_at: T }),
      makeSpan({ span_id: "second", started_at: T }),
    ]);
    expect(roots.map((r) => r.span.spanId)).toEqual(["first", "second"]);
  });

  it("survives duplicate span ids — first owns the id, duplicates become roots", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "dup", started_at: T }),
      makeSpan({ span_id: "child", parent_span_id: "dup", started_at: later(1) }),
      makeSpan({ span_id: "dup", started_at: later(2) }),
    ]);
    expect(roots).toHaveLength(2);
    expect(roots[0].children.map((c) => c.span.spanId)).toEqual(["child"]);
    expect(roots[1].span.spanId).toBe("dup");
  });

  it("survives parent cycles without dropping or looping forever", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "a", parent_span_id: "b", started_at: T }),
      makeSpan({ span_id: "b", parent_span_id: "a", started_at: later(1) }),
    ]);
    expect(roots).toHaveLength(2);
    expect(flattenSpanTree(roots)).toHaveLength(2);
  });

  it("does not mutate the input array or spans", () => {
    const spans = [
      makeSpan({ span_id: "b", started_at: later(1) }),
      makeSpan({ span_id: "a", started_at: T }),
    ];
    const snapshot = spans.map((s) => s.spanId);
    buildSpanTree(spans);
    expect(spans.map((s) => s.spanId)).toEqual(snapshot);
  });

  it("handles unparseable timestamps without crashing (they sort last)", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "bad", started_at: "not-a-date" }),
      makeSpan({ span_id: "good", started_at: T }),
    ]);
    expect(roots.map((r) => r.span.spanId)).toEqual(["good", "bad"]);
  });
});

describe("flattenSpanTree", () => {
  it("flattens depth-first with depth info", () => {
    const roots = buildSpanTree([
      makeSpan({ span_id: "root", started_at: T }),
      makeSpan({ span_id: "child", parent_span_id: "root", started_at: later(1) }),
      makeSpan({ span_id: "grandchild", parent_span_id: "child", started_at: later(2) }),
      makeSpan({ span_id: "second-root", started_at: later(3) }),
    ]);
    const rows = flattenSpanTree(roots);
    expect(rows.map((r) => `${r.span.spanId}@${r.depth}`)).toEqual([
      "root@0",
      "child@1",
      "grandchild@2",
      "second-root@0",
    ]);
  });

  it("returns an empty list for no spans", () => {
    expect(flattenSpanTree(buildSpanTree([]))).toEqual([]);
  });
});
