import { describe, expect, it } from "vitest";
import { ApiError } from "./client";
import { createThread, submitTurn } from "./threads";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const BASE = { baseUrl: "", apiKey: "key" };

describe("createThread", () => {
  it("parses the {id} response", async () => {
    const fetchImpl: typeof fetch = async (input, init) => {
      expect(String(input)).toContain("/v1/threads");
      expect(init?.method).toBe("POST");
      return jsonResponse(200, { id: "thread_abc123" });
    };
    const thread = await createThread(BASE, { fetchImpl });
    expect(thread.threadId).toBe("thread_abc123");
  });
});

describe("submitTurn", () => {
  it("parses the 202 waiting shape and the 200 completed text shape", async () => {
    const responses = [
      jsonResponse(202, {
        thread_id: "thread_1",
        turn_id: "turn_9",
        run_id: "run_9",
        status: "WAITING_APPROVAL",
        interrupt: { invocation_id: "run_9:call_0", tool_name: "shell" },
        text: null,
      }),
      jsonResponse(200, { thread_id: "thread_1", text: "Done. The answer is 4." }),
    ];
    const fetchImpl: typeof fetch = async (input, init) => {
      expect(String(input)).toBe("/v1/threads/thread_1/turns");
      expect(JSON.parse(String(init?.body))).toEqual({ message: "deploy it" });
      // Idempotency-Key must NOT be sent on turn submissions.
      expect(new Headers(init?.headers).has("Idempotency-Key")).toBe(false);
      const next = responses.shift();
      if (next === undefined) throw new Error("unexpected extra call");
      return next;
    };

    const waiting = await submitTurn(BASE, "thread_1", "deploy it", { fetchImpl });
    expect(waiting).toEqual({
      threadId: "thread_1",
      turnId: "turn_9",
      runId: "run_9",
      status: "WAITING_APPROVAL",
      text: null,
      queued: false,
    });

    // A completed root turn carries only {thread_id, text} — no run_id, and
    // callers must not fabricate a run from it.
    const completed = await submitTurn(BASE, "thread_1", "deploy it", { fetchImpl });
    expect(completed.threadId).toBe("thread_1");
    expect(completed.text).toBe("Done. The answer is 4.");
    expect(completed.runId).toBeNull();
    expect(completed.status).toBeNull();
    expect(completed.queued).toBe(false);
  });

  it("never auto-retries: transport failure and HTTP error surface exactly once", async () => {
    let calls = 0;
    const failing: typeof fetch = async () => {
      calls += 1;
      throw new TypeError("socket hangup");
    };
    const transportError: unknown = await submitTurn(BASE, "thread_1", "hi", {
      fetchImpl: failing,
    }).catch((e: unknown) => e);
    expect(calls).toBe(1);
    expect(transportError).toBeInstanceOf(ApiError);
    if (transportError instanceof ApiError) expect(transportError.status).toBe(0);

    // Explicit HTTP errors are not retried either; the plain error shape is
    // normalized into ApiError.
    let httpCalls = 0;
    const conflicted: typeof fetch = async () => {
      httpCalls += 1;
      return jsonResponse(409, { error: "thread turn already running" });
    };
    const httpError: unknown = await submitTurn(BASE, "thread_1", "hi", {
      fetchImpl: conflicted,
    }).catch((e: unknown) => e);
    expect(httpCalls).toBe(1);
    expect(httpError).toBeInstanceOf(ApiError);
    if (httpError instanceof ApiError) {
      expect(httpError.status).toBe(409);
      expect(httpError.message).toBe("thread turn already running");
    }
  });
});
