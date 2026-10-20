import { describe, expect, it } from "vitest";
import { EventProtocolError, fetchRuntimeEvents } from "./event-replay-client";
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

function sseResponse(body: string, afterId = 0): Response {
  const last = [...body.matchAll(/^id: (\d+)$/gm)].at(-1)?.[1] ?? afterId;
  return new Response(body, { headers: { "Content-Type": "text/event-stream",
    "X-Axiom-Events-After-Id": String(last), "X-Axiom-Events-Watermark": String(last), "X-Axiom-Events-Has-More": "false" } });
}

describe("fetchRuntimeEvents", () => {
  it("fetches the replay endpoint with auth and parses the real frame format", async () => {
    let seenUrl = "";
    let seenAuth = "";
    const fetchImpl: typeof fetch = async (input, init) => {
      seenUrl = String(input);
      seenAuth = String((init?.headers as Record<string, string> | undefined)?.Authorization ?? "");
      return sseResponse(
        frame(1, "llm.started", { model: "deepseek-chat" }) +
          frame(2, "tool.completed", { tool_name: "search_code" }),
      );
    };
    const { events } = await fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1" },
      { fetchImpl },
    );
    expect(seenUrl).toBe("http://runtime/v1/threads/thread_1/events?run_id=run_1&limit=200");
    expect(seenAuth).toBe("Bearer key");
    // Ordering is part of the protocol and must be deterministic.
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
      return sseResponse("", Number(new URL(String(input)).searchParams.get("after_id") ?? 0));
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

  it("rejects malformed frames without advancing any part of the batch", async () => {
    const fetchImpl: typeof fetch = async () =>
      sseResponse(
        frame(1, "run.started") + "id: 2\ndata: {broken\n\n" + frame(2, "run.completed"),
      );
    await expect(fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1" },
      { fetchImpl },
    )).rejects.toBeInstanceOf(EventProtocolError);
  });

  it("rejects frames whose JSON is valid but not a valid envelope", async () => {
    const fetchImpl: typeof fetch = async () =>
      sseResponse(
        'id: 1\ndata: {"event_id": "not-a-number", "event_type": "x", "timestamp": "t"}\n\n' +
          frame(2, "run.started"),
      );
    await expect(fetchRuntimeEvents(
      CONFIG,
      { threadId: "thread_1", runId: "run_1" },
      { fetchImpl },
    )).rejects.toBeInstanceOf(EventProtocolError);
  });

  it("rejects missing metadata, duplicate IDs and out-of-order frames", async () => {
    for (const response of [new Response(frame(1, "run.started")), sseResponse(frame(2, "x") + frame(1, "x")), sseResponse(frame(1, "x") + frame(1, "x"))]) {
      await expect(fetchRuntimeEvents(CONFIG, { threadId: "thread_1" }, { fetchImpl: async () => response })).rejects.toBeInstanceOf(EventProtocolError);
    }
  });

  it("starts at the newest bounded debug page when explicitly requested", async () => {
    let url = "";
    const page = await fetchRuntimeEvents(CONFIG, { threadId: "thread_1", latest: true, limit: 50 }, {
      fetchImpl: async (input) => { url = String(input); return sseResponse(frame(999, "run.completed")); },
    });
    expect(url).toContain("latest=1");
    expect(url).toContain("limit=50");
    expect(page).toMatchObject({ afterId: 999, hasMore: false, watermark: 999 });
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
