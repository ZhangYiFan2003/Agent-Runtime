import { describe, expect, it } from "vitest";
import {
  buildOverviewSections,
  deriveRunActions,
  deriveRunBanners,
  formatCompactNumber,
  formatCount,
  formatPercent,
  groupMetrics,
  isTerminalRunStatus,
  outputState,
  parseRunDetailSearch,
  promotedSpanAttributes,
  remainingSpanAttributes,
  runDetailRefetchInterval,
  DETAIL_REFETCH_INTERVAL_MS,
  RUN_REFETCH_INTERVAL_MS,
} from "./run-detail-view";
import { makeRunView } from "../test-fixtures/run";
import { makeRunMetrics } from "../test-fixtures/metrics";
import { makeSpan } from "../test-fixtures/trace";

describe("terminal status / polling", () => {
  it("classifies terminal statuses", () => {
    expect(isTerminalRunStatus("COMPLETED")).toBe(true);
    expect(isTerminalRunStatus("FAILED")).toBe(true);
    expect(isTerminalRunStatus("CANCELLED")).toBe(true);
    expect(isTerminalRunStatus("RUNNING")).toBe(false);
    expect(isTerminalRunStatus("WAITING_APPROVAL")).toBe(false);
    expect(isTerminalRunStatus("WAITING_CHILD")).toBe(false);
    expect(isTerminalRunStatus("INTERRUPTED")).toBe(false);
  });

  it("polls while non-terminal, stops on terminal, waits when unknown", () => {
    expect(runDetailRefetchInterval("RUNNING", RUN_REFETCH_INTERVAL_MS)).toBe(2000);
    expect(runDetailRefetchInterval("WAITING_APPROVAL", DETAIL_REFETCH_INTERVAL_MS)).toBe(3000);
    expect(runDetailRefetchInterval("COMPLETED", RUN_REFETCH_INTERVAL_MS)).toBe(false);
    expect(runDetailRefetchInterval("FAILED", DETAIL_REFETCH_INTERVAL_MS)).toBe(false);
    expect(runDetailRefetchInterval(undefined, RUN_REFETCH_INTERVAL_MS)).toBe(false);
    // unknown future statuses are treated as non-terminal (keep watching)
    expect(runDetailRefetchInterval("SUSPENDED_V2", RUN_REFETCH_INTERVAL_MS)).toBe(2000);
  });
});

describe("parseRunDetailSearch", () => {
  it("keeps a string span param, drops everything else", () => {
    expect(parseRunDetailSearch({ span: "span_1" })).toEqual({ span: "span_1" });
    expect(parseRunDetailSearch({})).toEqual({});
    expect(parseRunDetailSearch({ span: "" })).toEqual({});
    expect(parseRunDetailSearch({ span: 42 })).toEqual({});
    expect(parseRunDetailSearch({ span: ["a"] })).toEqual({});
    expect(parseRunDetailSearch({ span: "span_1", other: "noise" })).toEqual({ span: "span_1" });
  });
});

describe("deriveRunActions", () => {
  it("maps known operations to labels and tones", () => {
    const actions = deriveRunActions(["resume", "approve", "reject", "cancel"]);
    expect(actions.map((a) => a.label)).toEqual(["Resume", "Approve", "Reject", "Cancel"]);
    expect(actions.map((a) => a.tone)).toEqual(["default", "primary", "danger", "danger"]);
    expect(actions.every((a) => a.known)).toBe(true);
  });

  it("degrades unknown future operations to a generic action without crashing", () => {
    const actions = deriveRunActions(["pause_execution"]);
    expect(actions[0]).toEqual({
      operation: "pause_execution",
      label: "Pause execution",
      tone: "default",
      known: false,
    });
  });

  it("handles an empty operation list", () => {
    expect(deriveRunActions([])).toEqual([]);
  });
});

describe("deriveRunBanners", () => {
  it("renders an approval banner per pending interrupt with tool/reason/invocation", () => {
    const run = makeRunView({
      status: "WAITING_APPROVAL",
      waiting_reason: "approval",
      pending_interrupts: [
        {
          run_id: "run_abc123",
          invocation_id: "run_abc123:call_0",
          interrupt_type: "tool_approval",
          tool_name: "shell",
          reason: "policy requires approval",
          created_at: "2026-09-17T10:00:03+00:00",
        },
      ],
    });
    const banners = deriveRunBanners(run);
    expect(banners).toHaveLength(1);
    expect(banners[0].kind).toBe("approval");
    expect(banners[0].tone).toBe("warn");
    expect(banners[0].title).toBe("Approval required");
    expect(banners[0].fields).toEqual([
      { label: "Tool", value: "shell" },
      { label: "Reason", value: "policy requires approval" },
      { label: "Invocation", value: "run_abc123:call_0" },
    ]);
  });

  it("renders a waiting-for-children banner", () => {
    const run = makeRunView({
      status: "WAITING_CHILD",
      waiting_reason: "child",
      children_count: 3,
      active_children_count: 2,
      terminal_children_count: 1,
    });
    const banners = deriveRunBanners(run);
    expect(banners[0]).toMatchObject({ kind: "waiting", tone: "info", title: "Waiting for child runs" });
    expect(banners[0].fields).toEqual([{ label: "Children", value: "2 active · 1 terminal" }]);
  });

  it("renders the client_resume recovery banner verbatim", () => {
    const run = makeRunView({ status: "RUNNING", recovery_action: "client_resume" });
    const banners = deriveRunBanners(run);
    expect(banners[0].kind).toBe("recovery");
    expect(banners[0].message).toBe(
      "This run has no active execution handle. Resume is required for recovery.",
    );
  });

  it("returns nothing for a plain running run", () => {
    expect(deriveRunBanners(makeRunView({ status: "RUNNING" }))).toEqual([]);
  });
});

describe("outputState", () => {
  it("shows output for completed runs", () => {
    const state = outputState(makeRunView({ status: "COMPLETED", output: "done\nall good" }));
    expect(state).toEqual({ kind: "output", text: "done\nall good" });
  });

  it("shows error (type/message/step) for failed runs", () => {
    const state = outputState(
      makeRunView({
        status: "FAILED",
        error: { type: "RuntimeFailure", message: "boom", step: "llm" },
      }),
    );
    expect(state).toEqual({ kind: "error", errorType: "RuntimeFailure", message: "boom", step: "llm" });
  });

  it("is empty while in flight", () => {
    expect(outputState(makeRunView({ status: "RUNNING" }))).toEqual({ kind: "empty" });
  });
});

describe("metric formatting", () => {
  it("formats compact token counts", () => {
    expect(formatCompactNumber(8180)).toBe("8.2k");
    expect(formatCompactNumber(8000)).toBe("8k");
    expect(formatCompactNumber(12000)).toBe("12k");
    expect(formatCompactNumber(1_500_000)).toBe("1.5M");
    expect(formatCompactNumber(999)).toBe("999");
    expect(formatCompactNumber(0)).toBe("0");
    expect(formatCompactNumber(null)).toBe("—");
  });

  it("formats percentages and counts", () => {
    expect(formatPercent(0.924)).toBe("92.4%");
    expect(formatPercent(1)).toBe("100.0%");
    expect(formatPercent(null)).toBe("—");
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe("—");
  });
});

describe("groupMetrics", () => {
  it("groups the real RunMetrics fields with formatting", () => {
    const groups = groupMetrics(
      makeRunMetrics({ total_tokens: 8180, tool_success_rate: 0.924, duration_ms: 5000 }),
    );
    expect(groups.map((g) => g.title)).toEqual([
      "Execution",
      "Model",
      "Tools",
      "Retry",
      "Budget",
      "Progress",
      "Verification",
    ]);
    const model = groups[1];
    expect(model.entries.find((e) => e.label === "Total tokens")?.value).toBe("8.2k");
    const tools = groups[2];
    expect(tools.entries.find((e) => e.label === "Success rate")?.value).toBe("92.4%");
    const execution = groups[0];
    expect(execution.entries.find((e) => e.label === "Duration")?.value).toBe("5.0 s");
    // zero renders as "0", null renders as "—"
    expect(execution.entries.find((e) => e.label === "Interrupts")?.value).toBe("0");
    const retry = groups[3];
    expect(retry.entries.find((e) => e.label === "Retried model calls")?.value).toBe("0");
  });

  it("renders budget utilization dimensions as percentages", () => {
    const groups = groupMetrics(
      makeRunMetrics({ budget_utilization: { tokens: 0.5 }, budget_soft_limit_reached: true }),
    );
    const budget = groups.find((g) => g.title === "Budget");
    expect(budget?.entries).toContainEqual({ label: "utilization · tokens", value: "50.0%" });
    expect(budget?.entries).toContainEqual({ label: "Soft limit reached", value: "yes" });
  });
});

describe("buildOverviewSections", () => {
  it("includes metrics rows only when metrics are available", () => {
    const run = makeRunView({ status: "COMPLETED", completed_at: "2026-09-17T10:00:05+00:00" });
    const withMetrics = buildOverviewSections(run, makeRunMetrics());
    const withoutMetrics = buildOverviewSections(run, null);
    const execWith = withMetrics.find((s) => s.title === "Execution")!;
    const execWithout = withoutMetrics.find((s) => s.title === "Execution")!;
    expect(execWith.rows.map((r) => r.label)).toContain("Steps");
    expect(execWithout.rows.map((r) => r.label)).not.toContain("Steps");
  });

  it("adds a Recovery section only when waiting/recovery/children data exists", () => {
    const plain = buildOverviewSections(makeRunView({ status: "RUNNING" }), null);
    expect(plain.find((s) => s.title === "Recovery")).toBeUndefined();
    const waiting = buildOverviewSections(
      makeRunView({ status: "WAITING_APPROVAL", waiting_reason: "approval" }),
      null,
    );
    expect(waiting.find((s) => s.title === "Recovery")?.rows).toContainEqual({
      label: "Waiting",
      value: "approval",
    });
  });
});

describe("span attribute promotion", () => {
  it("promotes known high-value attributes that actually exist", () => {
    const span = makeSpan({
      span_type: "llm",
      attributes: {
        provider: "deepseek",
        model: "deepseek-chat",
        prompt_tokens: 6200,
        ttft_ms: 320,
        retry_count: 1,
        "context.compaction_count": 0,
      },
    });
    const promoted = promotedSpanAttributes(span);
    expect(promoted).toEqual([
      { label: "Provider", value: "deepseek" },
      { label: "Model", value: "deepseek-chat" },
      { label: "Prompt tokens", value: "6.2k" },
      { label: "TTFT", value: "320 ms" },
      { label: "Retries", value: "1" },
    ]);
    const remaining = remainingSpanAttributes(span);
    expect(remaining).toEqual({ "context.compaction_count": 0 });
  });

  it("promotes nothing for spans without known attributes", () => {
    const span = makeSpan({ attributes: { custom: "x" } });
    expect(promotedSpanAttributes(span)).toEqual([]);
    expect(remainingSpanAttributes(span)).toEqual({ custom: "x" });
  });

  it("leaves non-primitive promoted values in the raw viewer", () => {
    const span = makeSpan({ attributes: { provider: { nested: true } } });
    expect(promotedSpanAttributes(span)).toEqual([]);
    expect(remainingSpanAttributes(span)).toEqual({ provider: { nested: true } });
  });
});
