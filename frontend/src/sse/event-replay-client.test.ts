import { describe, expect, it } from "vitest";
import { fetchRuntimeEvents } from "./event-replay-client";
import { ApiError } from "../api/client";

const CONFIG = { baseUrl: "http://runtime", apiKey: "key" };

function frame(id: number, type: string, extra: Record<string, unknown> = {}): string {
  const data = JSON.stringify({
    ...extra,
    event_id: id,
    thread_id: "thread_1",
    turn_id: "turn_1",
    run_id: "run_1",
    parent_run_id: null,
    parent_step_id: null,
    assignment_id: null,
    event_type: type,
    timestamp: "2026-09-19T10:00:00+00:00",
  });
  return `id: ${id}\nevent: ${type}\ndata: ${data}\n\n`;
}

function sseResponse(body: string, status = 200): Response {
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

describe("fetchRuntimeEvents", () => {
  it("fetches the replay endpoint with auth and parses the real frame format", async () => {
    let seenUrl = "";
    let seenAuth = "";
    const fetchImpl: typeof fetch = async (input, init) => {
      seenUrl = String(input);
      seenAuth = String((init?.headers as Record<string, string> | undefined)?.Authorization ?? "");
      return sseResponse(
        frame(2, "tool.completed", { tool_name: "search_code" }) +
          frame(1, "llm.started", { model: "deepseek-chat" }),
      );
    };
    const events = await fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1" },
      { fetchImpl },
    );
    expect(seenUrl).toBe("http://runtime/v1/threads/thread_1/events?run_id=run_1");
    expect(seenAuth).toBe("Bearer key");
    // Sorted by event_id ascending even though the body was not.
    expect(events.map((e) => e.eventId)).toEqual([1, 2]);
    expect(events[0].eventType).toBe("llm.started");
    expect(events[0].payload).toEqual({ model: "deepseek-chat" });
    expect(events[1].payload).toEqual({ tool_name: "search_code" });
    // Envelope keys are not duplicated into the payload.
    expect(events[0].payload).not.toHaveProperty("event_id");
    expect(events[0].threadId).toBe("thread_1");
    expect(events[0].runId).toBe("run_1");
  });

  it("sends after_id as an exclusive integer cursor only when known", async () => {
    const urls: string[] = [];
    const fetchImpl: typeof fetch = async (input) => {
      urls.push(String(input));
      return sseResponse("");
    };
    await fetchRuntimeEvents(CONFIG, { threadId: "thread_1", runId: "run_1" }, { fetchImpl });
    expect(urls[0]).not.toContain("after_id");
    await fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1", afterId: 41 },
      { fetchImpl },
    );
    expect(urls[1]).toContain("after_id=41");
    await fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1", afterId: 0 },
      { fetchImpl },
    );
    expect(urls[2]).toContain("after_id=0");
  });

  it("skips malformed frames without failing the batch", async () => {
    const fetchImpl: typeof fetch = async () =>
      sseResponse(
        frame(1, "run.started") + "id: 2\ndata: {broken\n\n" + frame(2, "run.completed"),
      );
    const events = await fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1" },
      { fetchImpl },
    );
    expect(events.map((e) => e.eventId)).toEqual([1, 2]);
  });

  it("skips frames whose JSON is valid but not a valid envelope", async () => {
    const fetchImpl: typeof fetch = async () =>
      sseResponse(
        'id: 1\ndata: {"event_id": "not-a-number", "event_type": "x", "timestamp": "t"}\n\n' +
          frame(2, "run.started"),
      );
    const events = await fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1" },
      { fetchImpl },
    );
    expect(events.map((e) => e.eventId)).toEqual([2]);
  });

  it("throws ApiError on non-ok responses (plain thread-not-found shape)", async () => {
    const fetchImpl: typeof fetch = async () =>
      new Response(JSON.stringify({ error: "thread not found" }), { status: 404 });
    const error: unknown = await fetchRuntimeEvents(
      CONFIG,
      { threadId: "missing", runId: "run_1" },
      { fetchImpl },
    ).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    if (error instanceof ApiError) {
      expect(error.status).toBe(404);
      expect(error.message).toBe("thread not found");
    }
  });

  it("wraps network failures in ApiError with status 0", async () => {
    const fetchImpl: typeof fetch = async () => {
      throw new TypeError("connection refused");
    };
    const error: unknown = await fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1" },
      { fetchImpl },
    ).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
  });
});
