import { describe, expect, it } from "vitest";
import {
  eventCategory,
  replayStatusLabel,
  summarizeEvent,
  type ReplayStatus,
} from "./events-view";
import type { RuntimeEvent } from "../api/adapters/event";

function makeEvent(
  eventType: string,
  payload: Record<string, unknown> = {},
  overrides: Partial<RuntimeEvent> = {},
): RuntimeEvent {
  return {
    eventId: 1,
    threadId: "thread_1",
    turnId: "turn_1",
    runId: "run_1",
    parentRunId: null,
    parentStepId: null,
    assignmentId: null,
    eventType,
    timestamp: "2026-09-19T10:00:00+00:00",
    payload,
    ...overrides,
  };
}

describe("eventCategory", () => {
  it("maps known prefixes to the closed category set", () => {
    expect(eventCategory("run.started")).toBe("RUN");
    expect(eventCategory("step.started")).toBe("STEP");
    expect(eventCategory("agent.step.completed")).toBe("STEP");
    expect(eventCategory("llm.completed")).toBe("LLM");
    expect(eventCategory("tool.started")).toBe("TOOL");
    expect(eventCategory("tool_call")).toBe("TOOL");
    expect(eventCategory("tool_result")).toBe("TOOL");
    expect(eventCategory("checkpoint.saved")).toBe("CHECKPOINT");
    expect(eventCategory("interrupt.created")).toBe("INTERRUPT");
    expect(eventCategory("plan.task_started")).toBe("PLAN");
    expect(eventCategory("multi_agent.worker_started")).toBe("WORKER");
    expect(eventCategory("worker.finished")).toBe("WORKER");
    expect(eventCategory("error")).toBe("ERROR");
  });

  it("falls back to OTHER for unknown event types", () => {
    expect(eventCategory("synthesis.completed")).toBe("OTHER");
    expect(eventCategory("review.requested")).toBe("OTHER");
    expect(eventCategory("")).toBe("OTHER");
  });
});

describe("summarizeEvent", () => {
  it("summarizes tool events from tool_name / name only", () => {
    expect(summarizeEvent(makeEvent("tool.started", { tool_name: "search_code" }))).toBe(
      "search_code",
    );
    expect(summarizeEvent(makeEvent("tool_call", { name: "read_file" }))).toBe("read_file");
  });

  it("summarizes llm.completed with provider, tokens and latency", () => {
    const summary = summarizeEvent(
      makeEvent("llm.completed", {
        provider: "deepseek",
        model: "deepseek-chat",
        total_tokens: 834,
        latency_ms: 612,
      }),
    );
    expect(summary).toBe("deepseek · 834 tokens · 612 ms");
  });

  it("summarizes run failures from error / message fields", () => {
    expect(summarizeEvent(makeEvent("run.failed", { error: "budget exceeded" }))).toBe(
      "budget exceeded",
    );
    expect(summarizeEvent(makeEvent("error", { message: "boom" }))).toBe("boom");
  });

  it("returns an empty string when no real fields exist — nothing invented", () => {
    expect(summarizeEvent(makeEvent("llm.started"))).toBe("");
    expect(summarizeEvent(makeEvent("llm.started", { model: "" }))).toBe("");
    expect(summarizeEvent(makeEvent("custom.future_type", { nested: { a: 1 } }))).toBe("");
  });
});

describe("replayStatusLabel", () => {
  const cases: Array<[ReplayStatus, string | undefined, string]> = [
    ["connecting", "RUNNING", "Connecting…"],
    ["following", "RUNNING", "Following"],
    ["following", "COMPLETED", "Catching up"],
    ["following", undefined, "Following"],
    ["reconnecting", "RUNNING", "Reconnecting…"],
    ["stopped", "COMPLETED", "Stopped"],
    ["auth-error", "RUNNING", "Authentication failed"],
    ["not-found", "RUNNING", "Thread not found"],
  ];
  it("maps every status to its tiny status-line label", () => {
    for (const [status, runStatus, label] of cases) {
      expect(replayStatusLabel(status, runStatus)).toBe(label);
    }
  });
});
