import { describe, expect, it } from "vitest";
import type { RuntimeEvent } from "../api/adapters/event";
import {
  composeWorkbenchItems,
  composerState,
  discoverActiveRunId,
  latestRunId,
  promptAcknowledged,
  projectWorkbenchEvents,
  responseAcknowledged,
} from "./workbench-projection";

let seq = 0;
function makeEvent(
  eventType: string,
  payload: Record<string, unknown> = {},
  overrides: Partial<RuntimeEvent> = {},
): RuntimeEvent {
  seq += 1;
  return {
    eventId: seq,
    threadId: "thread_1",
    turnId: "turn_1",
    runId: "run_1",
    parentRunId: null,
    parentStepId: null,
    assignmentId: null,
    eventType,
    timestamp: `2026-09-20T10:00:${String(seq % 60).padStart(2, "0")}.000+00:00`,
    payload,
    ...overrides,
  };
}

describe("projectWorkbenchEvents", () => {
  it("projects messages, pairs tool call/result, and keeps lifecycle markers while filtering chatter", () => {
    const events = [
      makeEvent("run.started", {}, { runId: "run_a" }),
      makeEvent("user.message", { text: "find the bug" }, { runId: "run_a" }),
      makeEvent("llm.started", { provider: "deepseek" }, { runId: "run_a" }),
      makeEvent(
        "tool_call",
        { name: "search_code", input: { query: "parse" }, tool_call_id: "call_1" },
        { runId: "run_a" },
      ),
      makeEvent(
        "tool_result",
        { name: "search_code", result: "3 hits", is_error: false, tool_call_id: "call_1" },
        { runId: "run_a" },
      ),
      makeEvent("checkpoint.saved", { step: 2 }, { runId: "run_a" }),
      makeEvent("assistant.message", { text: "Found it." }),
      makeEvent("run.completed", {}, { runId: "run_a" }),
      makeEvent("turn.completed", { total_tokens: 1234 }, { runId: "run_a" }),
      // Unknown future event type: compact generic row, never a crash.
      makeEvent("quantum.entangled", { message: "spooky" }),
    ];

    const items = projectWorkbenchEvents(events);
    const kinds = items.map((item) => item.kind);
    // chatter (llm.started, checkpoint.saved) is filtered
    expect(kinds).toEqual([
      "lifecycle",
      "user",
      "tool",
      "assistant",
      "lifecycle",
      "lifecycle",
      "other",
    ]);

    const tool = items.find((item) => item.kind === "tool");
    expect(tool).toMatchObject({
      name: "search_code",
      status: "completed",
      isError: false,
      input: { query: "parse" },
      result: "3 hits",
    });
    if (tool?.kind === "tool") expect(tool.durationMs).not.toBeNull();

    expect(items[0]).toMatchObject({ kind: "lifecycle", label: "Run started · run_a" });
    expect(items[1]).toMatchObject({ kind: "user", text: "find the bug", pending: false });
    expect(items[3]).toMatchObject({ kind: "assistant", text: "Found it." });
    expect(items[3]).toMatchObject({ runId: "run_1" });
    expect(items[4]).toMatchObject({ kind: "lifecycle", label: "Run completed · run_a" });
    expect(items[5]).toMatchObject({ kind: "lifecycle", label: "Turn completed · 1234 tokens" });
    expect(items[6]).toMatchObject({ kind: "other", eventType: "quantum.entangled" });
  });

  it("keeps an unpaired tool_call as running and renders orphan results as their own row", () => {
    const items = projectWorkbenchEvents([
      makeEvent("tool_call", { name: "shell", tool_call_id: "call_x" }),
      makeEvent("tool_result", { name: "orphan_tool", result: "x", is_error: true }),
    ]);
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "tool", name: "shell", status: "running" });
    expect(items[1]).toMatchObject({ kind: "tool", name: "orphan_tool", status: "error" });
  });

  it("replaces the pending local echo when the real user.message event arrives (no duplicate)", () => {
    const pending = {
      threadId: "thread_1", prompt: "hello", responseText: null,
      runId: "run_1", turnId: "turn_1",
    };

    // Before the event arrives: local pending echo is shown.
    const before = composeWorkbenchItems([], pending);
    expect(before).toHaveLength(1);
    expect(before[0]).toMatchObject({ kind: "user", text: "hello", pending: true });

    // After the real event lands: one row, not pending.
    const events = [makeEvent("user.message", { text: "hello" })];
    const after = composeWorkbenchItems(events, pending);
    expect(after).toHaveLength(1);
    expect(after[0]).toMatchObject({ kind: "user", text: "hello", pending: false });

    // An identical older message belongs to a different Run and must not ack.
    const oldEvents = [makeEvent("user.message", { text: "hello" }, { runId: "run_old" })];
    const stale = composeWorkbenchItems(oldEvents, pending);
    expect(stale).toHaveLength(2);
  });

  it("does not duplicate assistant text present in both the POST response and the event", () => {
    const events = [makeEvent("assistant.message", { text: "All done." })];
    const items = composeWorkbenchItems(events, {
      threadId: "thread_1", runId: "run_1", turnId: "turn_1",
      prompt: null,
      responseText: "All done.",
    });
    expect(items.filter((item) => item.kind === "assistant")).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: "assistant", fromResponse: false });

    // Without the event (e.g. still in flight), the response text is the fallback.
    const fallback = composeWorkbenchItems([], {
      threadId: "thread_1", runId: "run_1", turnId: "turn_1",
      prompt: null,
      responseText: "All done.",
    });
    expect(fallback).toHaveLength(1);
    expect(fallback[0]).toMatchObject({ kind: "assistant", text: "All done.", fromResponse: true });

    const linkedFallback = composeWorkbenchItems([], {
      threadId: "thread_1", runId: "run_confirmed", turnId: "turn_confirmed",
      prompt: null,
      responseText: "Supported [claim:clm_abc]",
    });
    expect(linkedFallback[0]).toMatchObject({ kind: "assistant", runId: "run_confirmed" });
  });

  it("deduplicates final messages by identity while preserving equal text from another turn and raw legacy messages", () => {
    const events = [
      makeEvent("user.message", { text: "repeat" }),
      makeEvent("user.message", { text: "repeat" }),
      makeEvent("assistant.message", { text: "answer" }),
      makeEvent("assistant.message", { text: "answer" }),
      makeEvent("user.message", { text: "repeat" }, { runId: "run_2", turnId: "turn_2" }),
      makeEvent("assistant.message", { text: "answer" }, { runId: "run_2", turnId: "turn_2" }),
      makeEvent("user.message", { text: "unlinked legacy record" }, { runId: null, turnId: null }),
      makeEvent("assistant.message", { text: "unlinked legacy answer" }, { runId: null, turnId: null }),
      makeEvent("assistant.message", { text: "child output" }, { runId: "child", parentRunId: "run_2" }),
    ];
    const items = projectWorkbenchEvents(events);
    expect(items.filter((item) => item.kind === "user")).toHaveLength(3);
    expect(items.filter((item) => item.kind === "assistant")).toHaveLength(3);
    expect(items.at(-1)).toMatchObject({ kind: "other", eventType: "child.assistant.message", summary: "child output" });
  });

  it("acknowledges only root messages with matching Thread, Run and Turn, independently of text or event position", () => {
    const wrongIdentities = [
      makeEvent("user.message", { text: "same text" }, { threadId: "other_thread" }),
      makeEvent("user.message", { text: "same text" }, { runId: "other_run" }),
      makeEvent("user.message", { text: "same text" }, { turnId: "other_turn" }),
      makeEvent("user.message", { text: "same text" }, { parentRunId: "root" }),
      makeEvent("assistant.message", { text: "same text" }, { runId: "other_run" }),
      makeEvent("assistant.message", { text: "same text" }, { turnId: "other_turn" }),
      makeEvent("assistant.message", { text: "same text" }, { threadId: "other_thread" }),
      makeEvent("assistant.message", { text: "same text" }, { parentRunId: "root" }),
    ];
    expect(promptAcknowledged(wrongIdentities, "thread_1", "run_1", "turn_1")).toBe(false);
    expect(responseAcknowledged(wrongIdentities, "thread_1", "run_1", "turn_1")).toBe(false);
    const matched = [
      ...wrongIdentities,
      makeEvent("user.message", { text: "normalized prompt" }, { eventId: 1 }),
      makeEvent("assistant.message", { text: "server answer" }, { eventId: 2 }),
    ];
    expect(promptAcknowledged(matched, "thread_1", "run_1", "turn_1")).toBe(true);
    expect(responseAcknowledged(matched, "thread_1", "run_1", "turn_1")).toBe(true);
    expect(promptAcknowledged(matched, "thread_1", null, null)).toBe(false);
  });

  it("pairs reused tool invocation IDs only within their originating Run and Turn", () => {
    const items = projectWorkbenchEvents([
      makeEvent("tool_call", { tool_call_id: "call_0", name: "root_tool" }),
      makeEvent("tool_call", { tool_call_id: "call_0", name: "child_tool" }, { runId: "child", parentRunId: "run_1" }),
      makeEvent("tool_result", { tool_call_id: "call_0", result: "child result" }, { runId: "child", parentRunId: "run_1" }),
      makeEvent("tool_result", { tool_call_id: "call_0", result: "root result" }),
    ]);
    expect(items).toHaveLength(2);
    expect(items[0]).toMatchObject({ kind: "tool", name: "root_tool", result: "root result" });
    expect(items[1]).toMatchObject({ kind: "tool", name: "child_tool", result: "child result" });
  });

  it("discovers the active run from turn.started/user.message run_ids and excludes terminal runs", () => {
    const events = [
      makeEvent("run.started", {}, { runId: "run_old" }),
      makeEvent("run.completed", {}, { runId: "run_old" }),
      // new turn: run_id arrives immediately on turn.started / user.message
      makeEvent("turn.started", { message_chars: 5 }, { runId: "run_new", turnId: "turn_2" }),
      makeEvent("user.message", { text: "again" }, { runId: "run_new", turnId: "turn_2" }),
    ];
    expect(discoverActiveRunId(events)).toBe("run_new");
    expect(latestRunId(events)).toBe("run_new");

    // Once the run terminates there is no active run, but it stays "latest".
    const done = [...events, makeEvent("run.completed", {}, { runId: "run_new" })];
    expect(discoverActiveRunId(done)).toBeNull();
    expect(latestRunId(done)).toBe("run_new");
    const withChild = [...done, makeEvent("run.started", {}, { runId: "run_child", parentRunId: "run_new" })];
    expect(discoverActiveRunId(withChild)).toBeNull();
    expect(latestRunId(withChild)).toBe("run_new");
  });
});

describe("composerState", () => {
  it("disables Send when disconnected, submitting, or a run is non-terminal", () => {
    expect(composerState({ connected: false, submitting: false, activeRunStatus: null })).toEqual({
      disabled: true,
      reason: "Runtime service is unreachable.",
    });
    expect(
      composerState({ connected: true, submitting: true, activeRunStatus: null }).disabled,
    ).toBe(true);
    expect(
      composerState({ connected: true, submitting: false, activeRunStatus: "RUNNING" }),
    ).toEqual({ disabled: true, reason: "A turn is already running in this thread." });
    expect(
      composerState({ connected: true, submitting: false, activeRunStatus: "WAITING_APPROVAL" })
        .disabled,
    ).toBe(true);
    expect(
      composerState({ connected: true, submitting: false, activeRunStatus: "COMPLETED" }).disabled,
    ).toBe(false);
    expect(
      composerState({ connected: true, submitting: false, activeRunStatus: null }).disabled,
    ).toBe(false);
  });

  it("unlocks an event-inferred Run after repeated 404s with a diagnostic, without claiming completion", () => {
    expect(composerState({
      connected: true, submitting: false, activeRunStatus: "RUNNING", runLookupMissing: true,
    })).toEqual({
      disabled: false,
      reason: "Run lookup repeatedly returned 404. Retry lookup or submit to check the Thread's real availability.",
    });
    expect(composerState({
      connected: true, submitting: false, activeRunStatus: "RUNNING", runLookupMissing: true, unconfirmed: true,
    }).disabled).toBe(true);
  });

  it.each(["WAITING_APPROVAL", "WAITING_CHILD", "INTERRUPTED"])("keeps occupied state %s busy", (status) => {
    expect(composerState({ connected: true, submitting: false, activeRunStatus: status }).disabled).toBe(true);
  });
});
