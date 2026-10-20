import { describe, expect, it } from "vitest";
import { fetchThreadMessages, type ThreadMessage, type ThreadMessagesPage } from "../api/messages";
import { mergeMessagePage } from "./use-thread-messages";
import { composeDurableWorkbenchItems } from "../lib/workbench-projection";
import type { RuntimeEvent } from "../api/adapters/event";

const message = (id: number): ThreadMessage => ({ id, thread_id: "thread", run_id: `run${Math.ceil(id / 2)}`,
  turn_id: `turn${Math.ceil(id / 2)}`, role: id % 2 ? "user" : "assistant", text: `message${id}`, created_at: "same timestamp" });
const page = (ids: number[], older: string | null, after: string): ThreadMessagesPage => ({ messages: ids.map(message),
  older_cursor: older, after_cursor: after, has_more: older !== null, current_run: { run_id: "run60", status: "COMPLETED" } });

describe("durable conversation pages", () => {
  it("merges equal-timestamp pages by stable IDs, preserving live cursor when an old page resolves later", () => {
    let data = mergeMessagePage(undefined, page([5, 6, 7, 8], "before5", "after8"), "initial");
    data = mergeMessagePage(data, page([8, 9, 10], "irrelevant", "after10"), "after");
    data = mergeMessagePage(data, page([1, 2, 3, 4, 5], null, "after5"), "older");
    expect(data.messages.map((entry) => entry.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(data.afterCursor).toBe("after10");
    expect(data.olderCursor).toBeNull();
    expect(data.currentRun?.run_id).toBe("run60");
  });

  it("does not lose older pages when live data resolves after them or advance an empty increment", () => {
    let data = mergeMessagePage(undefined, page([5, 6], "before5", "after6"), "initial");
    data = mergeMessagePage(data, page([1, 2, 3, 4], null, "after4"), "older");
    data = mergeMessagePage(data, { ...page([], null, "after6"), has_more: false }, "after");
    expect(data.messages.map((entry) => entry.id)).toEqual([1, 2, 3, 4, 5, 6]);
    expect(data.afterCursor).toBe("after6");
    expect(data.olderCursor).toBeNull();
  });

  it("keeps durable conversation after 1500 later debug events and after a fresh page load", () => {
    const initial = page([1, 2], null, "after2");
    const events: RuntimeEvent[] = Array.from({ length: 1500 }, (_, index) => ({ eventId: index + 100, threadId: "thread",
      runId: "run1", turnId: "turn1", parentRunId: null, parentStepId: null, assignmentId: null,
      eventType: "step.started", timestamp: "same", payload: {} }));
    const pending = { threadId: "thread", prompt: null, responseText: null, runId: null, turnId: null };
    for (const data of [mergeMessagePage(undefined, initial, "initial"), mergeMessagePage(undefined, initial, "initial")]) {
      const items = composeDurableWorkbenchItems(data.messages, events, pending, null);
      expect(items.map((item) => item.kind)).toEqual(["user", "assistant"]);
    }
  });

  it("replaces a preview and local echoes with exactly one durable final answer by Run/Turn", () => {
    const items = composeDurableWorkbenchItems([message(1), message(2)], [], {
      threadId: "thread", prompt: "prompt", responseText: "final from POST", runId: "run1", turnId: "turn1",
    }, { runId: "run1", turnId: "turn1", eventId: 100, timestamp: "same", text: "partial", incomplete: false });
    expect(items).toHaveLength(2);
    expect(items[1]).toMatchObject({ kind: "assistant", text: "message2", fromResponse: false });
  });

  it("passes independent cursors and rejects invalid successful responses instead of empty history", async () => {
    const urls: string[] = [];
    const fetchImpl: typeof fetch = async (input) => { urls.push(String(input)); return Response.json(page([1, 2], null, "after2")); };
    const result = await fetchThreadMessages({ baseUrl: "http://runtime", apiKey: "" }, "thread", { before: "opaque==" }, { fetchImpl });
    expect(result.messages).toHaveLength(2);
    expect(new URL(urls[0]).searchParams.get("before")).toBe("opaque==");
    expect(new URL(urls[0]).searchParams.has("after")).toBe(false);
    expect(new URL(urls[0]).searchParams.get("limit")).toBe("50");
    for (const body of [[], {}, page([2, 1], null, "after2"), { ...page([1], null, "a"), messages: [{ ...message(1), thread_id: "other" }] }]) {
      await expect(fetchThreadMessages({ baseUrl: "", apiKey: "" }, "thread", {}, { fetchImpl: async () => Response.json(body) })).rejects.toThrow("Invalid conversation page");
    }
  });
});
