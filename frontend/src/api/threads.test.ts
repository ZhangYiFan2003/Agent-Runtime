import { describe, expect, it } from "vitest";
import { ApiError } from "./client";
import { createThread, parseTurnResult, submitTurn } from "./threads";

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
      }),
      jsonResponse(200, {
        thread_id: "thread_1", run_id: "run_10", turn_id: "turn_10", status: "COMPLETED",
        text: "Done. The answer is 4.",
      }),
    ];
    const fetchImpl: typeof fetch = async (input, init) => {
      expect(String(input)).toBe("/v1/threads/thread_1/turns");
      expect(JSON.parse(String(init?.body))).toEqual({ message: "deploy it" });
      expect(new Headers(init?.headers).get("Idempotency-Key")).toBe("submission-test");
      const next = responses.shift();
      if (next === undefined) throw new Error("unexpected extra call");
      return next;
    };

    const waiting = await submitTurn(BASE, "thread_1", "deploy it", { fetchImpl, idempotencyKey: "submission-test" });
    expect(waiting).toEqual({
      threadId: "thread_1",
      turnId: "turn_9",
      runId: "run_9",
      status: "WAITING_APPROVAL",
      text: null,
      queued: false,
    });

    const completed = await submitTurn(BASE, "thread_1", "deploy it", { fetchImpl, idempotencyKey: "submission-test" });
    expect(completed.threadId).toBe("thread_1");
    expect(completed.text).toBe("Done. The answer is 4.");
    expect(completed.runId).toBe("run_10");
    expect(completed.turnId).toBe("turn_10");
    expect(completed.status).toBe("COMPLETED");
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
      idempotencyKey: "submission-test",
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
      idempotencyKey: "submission-test",
    }).catch((e: unknown) => e);
    expect(httpCalls).toBe(1);
    expect(httpError).toBeInstanceOf(ApiError);
    if (httpError instanceof ApiError) {
      expect(httpError.status).toBe(409);
      expect(httpError.message).toBe("thread turn already running");
    }
  });

  const valid = { thread_id: "thread_1", run_id: "run_1", turn_id: "turn_1", status: "RUNNING" };
  it.each([
    {}, null, "ok", { ...valid, run_id: undefined }, { ...valid, run_id: "" },
    { ...valid, turn_id: 1 }, { ...valid, thread_id: "another_thread" },
    { ...valid, status: "queued" }, { ...valid, queued: "true" }, { ...valid, text: null },
  ])("rejects invalid successful submission response %j", (body) => {
    expect(() => parseTurnResult("thread_1", body)).toThrowError("Invalid submission response");
  });

  it("accepts a queued response with identities and ignores additional fields", () => {
    expect(parseTurnResult("thread_1", { ...valid, queued: true, extra: "future" })).toEqual({
      threadId: "thread_1", runId: "run_1", turnId: "turn_1", status: "RUNNING",
      queued: true, text: null,
    });
  });
});
