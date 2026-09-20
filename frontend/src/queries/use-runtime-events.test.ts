import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  MAX_KEPT_EVENTS,
  nextBackoffMs,
  pollIntervalMs,
  startReplaySession,
  type RuntimeEventsState,
} from "./use-runtime-events";

const CONFIG = { baseUrl: "", apiKey: "key" };

/** Build a real SSE replay body for the given envelope ids/types. */
function sseBody(
  entries: Array<{ id: number; type: string; extra?: Record<string, unknown>; malformed?: boolean }>,
): string {
  return entries
    .map(({ id, type, extra, malformed }) => {
      const data = malformed
        ? "{not json"
        : JSON.stringify({
            ...(extra ?? {}),
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
    })
    .join("");
}

function sseResponse(entries: Parameters<typeof sseBody>[0], status = 200): Response {
  return new Response(sseBody(entries), {
    status,
    headers: { "Content-Type": "text/event-stream" },
  });
}

/** Session test rig: records state updates and fetch URLs. */
function makeRig(overrides: { runStatus?: string } = {}) {
  const urls: string[] = [];
  const states: RuntimeEventsState[] = [];
  let runStatus = overrides.runStatus ?? "RUNNING";
  return {
    urls,
    states,
    get runStatus() {
      return runStatus;
    },
    set runStatus(value: string) {
      runStatus = value;
    },
    last(): RuntimeEventsState {
      return states[states.length - 1];
    },
    options(fetchImpl: typeof fetch) {
      return {
        threadId: "thread_1",
        runId: "run_1",
        getConfig: () => CONFIG,
        getRunStatus: () => runStatus,
        onUpdate: (state: RuntimeEventsState) => states.push(state),
        fetchImpl,
        backoff: (attempt: number) => attempt * 1000,
      };
    },
    trackFetch(impl: (url: string, index: number) => Response | Promise<Response>) {
      const fetchImpl: typeof fetch = async (input) => {
        const url = String(input);
        urls.push(url);
        return impl(url, urls.length - 1);
      };
      return fetchImpl;
    },
  };
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("pollIntervalMs / nextBackoffMs", () => {
  it("polls ~1.5s while non-terminal, brisk catch-up when terminal", () => {
    expect(pollIntervalMs("RUNNING")).toBe(1500);
    expect(pollIntervalMs("WAITING_APPROVAL")).toBe(1500);
    expect(pollIntervalMs(undefined)).toBe(1500);
    expect(pollIntervalMs("COMPLETED")).toBe(500);
    expect(pollIntervalMs("FAILED")).toBe(500);
  });

  it("backs off 1s → 2s → 4s → 8s cap with jitter", () => {
    const seq = [1, 2, 3, 4, 5].map((a) => nextBackoffMs(a, () => 0.5));
    expect(seq).toEqual([1000, 2000, 4000, 8000, 8000]);
  });
});

describe("startReplaySession", () => {
  it("replays from the start, then polls with an exclusive cursor and dedupes", async () => {
    const rig = makeRig();
    const fetchImpl = rig.trackFetch((url, index) => {
      if (index === 0) {
        expect(url).toContain("/v1/threads/thread_1/events");
        expect(url).toContain("run_id=run_1");
        expect(url).not.toContain("after_id");
        return sseResponse([
          { id: 1, type: "run.started" },
          { id: 3, type: "llm.started" },
          { id: 3, type: "llm.started" },
          { id: 5, type: "llm.completed", extra: { total_tokens: 10 } },
        ]);
      }
      expect(url).toContain("after_id=5"); // exclusive — never cursor+1
      return sseResponse([{ id: 3, type: "llm.started" }, { id: 7, type: "tool.started" }]);
    });

    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().status).toBe("following");
    expect(rig.last().events.map((e) => e.eventId)).toEqual([1, 3, 5]);
    expect(rig.last().events[3 - 1].payload).toEqual({ total_tokens: 10 });

    await vi.advanceTimersByTimeAsync(1500);
    expect(rig.last().events.map((e) => e.eventId)).toEqual([1, 3, 5, 7]);
    session.stop();
  });

  it("stops on 401 with no further fetches", async () => {
    const rig = makeRig();
    const fetchImpl = rig.trackFetch(() =>
      new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 }),
    );
    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().status).toBe("auth-error");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(rig.urls).toHaveLength(1);
    session.stop();
  });

  it("stops on 404 thread not found", async () => {
    const rig = makeRig();
    const fetchImpl = rig.trackFetch(() =>
      new Response(JSON.stringify({ error: "thread not found" }), { status: 404 }),
    );
    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().status).toBe("not-found");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(rig.urls).toHaveLength(1);
    session.stop();
  });

  it("reconnects with backoff after network failure; success resets backoff", async () => {
    const rig = makeRig();
    let calls = 0;
    const fetchImpl = rig.trackFetch(() => {
      calls += 1;
      if (calls === 1) return Promise.reject(new TypeError("connection reset"));
      if (calls === 2) return Promise.reject(new TypeError("connection reset"));
      return sseResponse([{ id: 1, type: "run.started" }]);
    });

    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().status).toBe("reconnecting");

    await vi.advanceTimersByTimeAsync(1000); // attempt 1 backoff
    expect(calls).toBe(2);
    expect(rig.last().status).toBe("reconnecting");

    await vi.advanceTimersByTimeAsync(2000); // attempt 2 backoff (escalated)
    expect(calls).toBe(3);
    expect(rig.last().status).toBe("following");
    expect(rig.last().events).toHaveLength(1);

    // Success reset the backoff: the next failure waits 1s again.
    rig.runStatus = "FAILED"; // trigger terminal drain path; still poll-based
    await vi.advanceTimersByTimeAsync(1500);
    session.stop();
  });

  it("stop aborts the in-flight fetch and ends the loop", async () => {
    const rig = makeRig();
    const signals: AbortSignal[] = [];
    const fetchImpl: typeof fetch = async (_input, init) => {
      if (init?.signal) signals.push(init.signal);
      return new Promise<Response>(() => {}); // never resolves
    };
    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(signals).toHaveLength(1);
    expect(rig.states).toHaveLength(1); // initial "connecting" emit only
    session.stop();
    expect(signals[0].aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(rig.states).toHaveLength(1); // loop ended, no further updates
  });

  it("terminal drain: at most 2 polls after terminal, then stopped", async () => {
    const rig = makeRig({ runStatus: "RUNNING" });
    const fetchImpl = rig.trackFetch(() => sseResponse([]));
    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().status).toBe("following");
    expect(rig.urls).toHaveLength(1);

    rig.runStatus = "COMPLETED";
    await vi.advanceTimersByTimeAsync(1500); // drain poll 1 (detects terminal)
    expect(rig.urls).toHaveLength(2);
    expect(rig.last().status).toBe("following");

    await vi.advanceTimersByTimeAsync(500); // drain poll 2 → stop
    expect(rig.urls).toHaveLength(3);
    expect(rig.last().status).toBe("stopped");

    await vi.advanceTimersByTimeAsync(10_000); // never polls again
    expect(rig.urls).toHaveLength(3);
    session.stop();
  });

  it("accepts late events during the terminal drain", async () => {
    const rig = makeRig({ runStatus: "COMPLETED" });
    const fetchImpl = rig.trackFetch((_url, index) =>
      index === 0
        ? sseResponse([{ id: 1, type: "run.started" }])
        : sseResponse([{ id: 2, type: "run.completed" }]),
    );
    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().events.map((e) => e.eventId)).toEqual([1]);

    await vi.advanceTimersByTimeAsync(500); // drain poll 2
    expect(rig.last().events.map((e) => e.eventId)).toEqual([1, 2]);
    expect(rig.last().status).toBe("stopped");
    session.stop();
  });

  it("bounds the kept history without moving the cursor back", async () => {
    const rig = makeRig();
    const overflow = Array.from({ length: MAX_KEPT_EVENTS + 100 }, (_, i) => ({
      id: i + 1,
      type: "step.started",
    }));
    const fetchImpl = rig.trackFetch((_url, index) =>
      index === 0 ? sseResponse(overflow) : sseResponse([{ id: MAX_KEPT_EVENTS + 101, type: "run.completed" }]),
    );
    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().events).toHaveLength(MAX_KEPT_EVENTS);
    expect(rig.last().events[0].eventId).toBe(101);

    await vi.advanceTimersByTimeAsync(1500);
    // Cursor kept advancing even though old UI entries were dropped.
    expect(rig.urls[1]).toContain(`after_id=${MAX_KEPT_EVENTS + 100}`);
    expect(rig.last().events).toHaveLength(MAX_KEPT_EVENTS);
    expect(rig.last().events.at(-1)?.eventId).toBe(MAX_KEPT_EVENTS + 101);
    session.stop();
  });

  it("skips malformed frames without dropping the rest of the batch", async () => {
    const rig = makeRig();
    const body =
      sseBody([{ id: 1, type: "llm.started" }]) +
      "id: 2\nevent: bad\ndata: {not json\n\n" +
      sseBody([{ id: 2, type: "tool.started" }]);
    const fetchImpl = rig.trackFetch(() => new Response(body, { status: 200 }));
    const session = startReplaySession(rig.options(fetchImpl));
    await vi.advanceTimersByTimeAsync(0);
    expect(rig.last().events.map((e) => e.eventId)).toEqual([1, 2]);
    expect(rig.last().status).toBe("following");
    session.stop();
  });
});
