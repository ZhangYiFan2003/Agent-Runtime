import { describe, expect, it } from "vitest";
import {
  SUBMISSION_LOST_MESSAGE,
  TURN_ALREADY_RUNNING_MESSAGE,
  createWorkbenchSession,
} from "./workbench-session";
import type { ConnectionConfig } from "../lib/connection";
import type { StorageLike } from "../lib/thread-registry";

const CONFIG = { baseUrl: "", apiKey: "key" };

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function temporaryStorage(): StorageLike & { entries: Map<string, string> } {
  const entries = new Map<string, string>();
  return {
    entries,
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => { entries.set(key, value); },
    removeItem: (key) => { entries.delete(key); },
  };
}

const ACCEPTED = {
  thread_id: "thread_1", run_id: "run_1", turn_id: "turn_1", status: "RUNNING",
};

function rejected(status: number, code: string): Response {
  return jsonResponse(status, {
    error: { code, message: "Submission rejected", details: { active_run_id: "run_active" } },
  });
}

describe("workbench session", () => {
  it("submits a turn and keeps the prompt echo pending until events acknowledge it", async () => {
    const session = createWorkbenchSession({
      getConfig: () => CONFIG,
      fetchImpl: async () =>
        jsonResponse(202, {
          thread_id: "thread_1",
          turn_id: "turn_7",
          run_id: "run_7",
          status: "WAITING_CHILD",
        }),
    });

    const submission = session.submit("thread_1", "ship it");
    expect(session.snapshot().phase).toBe("submitting");
    expect(session.snapshot().prompt).toBe("ship it");

    await submission;
    const state = session.snapshot();
    expect(state.phase).toBe("idle");
    expect(state.confirmedRunId).toBe("run_7");
    expect(state.confirmedTurnId).toBe("turn_7");
    // Prompt echo persists until the real user.message event arrives.
    expect(state.prompt).toBe("ship it");

    session.acknowledgePrompt();
    expect(session.snapshot().prompt).toBeNull();
  });

  it("maps a 409 to a friendly already-running failure and clears the echo", async () => {
    const session = createWorkbenchSession({
      getConfig: () => CONFIG,
      fetchImpl: async () => rejected(409, "THREAD_BUSY"),
    });
    await session.submit("thread_1", "again");
    const state = session.snapshot();
    expect(state.phase).toBe("failed");
    expect(state.errorStatus).toBe(409);
    expect(state.errorMessage).toBe(TURN_ALREADY_RUNNING_MESSAGE);
    expect(state.prompt).toBeNull();
    expect(state.rejectedDraft).toBe("again");
    expect(state.confirmedRunId).toBe("run_active");
    expect(state.confirmedStatus).toBeNull();

    session.dismissNotice();
    expect(session.snapshot().phase).toBe("idle");
  });

  it("surfaces a transport failure once as the lost phase (never resends)", async () => {
    let calls = 0;
    const session = createWorkbenchSession({
      getConfig: () => CONFIG,
      fetchImpl: async () => {
        calls += 1;
        throw new TypeError("connection reset");
      },
    });
    await session.submit("thread_1", "hello");
    const state = session.snapshot();
    expect(calls).toBe(1);
    expect(state.phase).toBe("lost");
    expect(SUBMISSION_LOST_MESSAGE).toContain("may still be executing");
    // The prompt echo stays — the run may still persist user.message.
    expect(state.prompt).toBe("hello");
    session.dismissNotice();
    expect(session.snapshot().phase).toBe("lost");
  });

  it("abortAndReset (thread switch) aborts the POST and swallows its outcome", async () => {
    const rig: { signal: AbortSignal | null; reject: ((error: unknown) => void) | null } = {
      signal: null,
      reject: null,
    };
    const session = createWorkbenchSession({
      getConfig: () => CONFIG,
      fetchImpl: (_input, init) =>
        new Promise<Response>((_resolve, reject) => {
          rig.signal = (init?.signal ?? null) as AbortSignal | null;
          rig.reject = reject;
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    });

    const submission = session.submit("thread_1", "long turn");
    expect(session.snapshot().phase).toBe("submitting");

    session.abortAndReset();
    expect(rig.signal?.aborted).toBe(true);
    expect(session.snapshot().phase).toBe("idle");
    expect(session.snapshot().threadId).toBeNull();

    await submission; // resolves without surfacing the aborted POST
    const state = session.snapshot();
    expect(state.phase).toBe("idle");
    expect(state.errorMessage).toBeNull();

    // A late resolution from the stale request must not leak into new state.
    rig.reject?.(new Error("late"));
    expect(session.snapshot().phase).toBe("idle");

    session.restore("thread_1");
    expect(session.snapshot()).toMatchObject({ phase: "lost", prompt: "long turn" });
  });

  it("ignores concurrent send attempts and confirms with the original key and message", async () => {
    let resolveRequest: ((response: Response) => void) | undefined;
    const requests: { key: string | null; body: string }[] = [];
    let keysMinted = 0;
    const session = createWorkbenchSession({
      getConfig: () => CONFIG,
      mintKey: () => `submission_${++keysMinted}`,
      fetchImpl: async (_input, init) => {
        requests.push({ key: new Headers(init?.headers).get("Idempotency-Key"), body: String(init?.body) });
        if (requests.length === 1) return new Promise<Response>((resolve) => { resolveRequest = resolve; });
        return jsonResponse(202, ACCEPTED);
      },
    });
    const original = session.submit("thread_1", "original");
    await session.submit("thread_1", "concurrent replacement");
    expect(requests).toHaveLength(1);
    resolveRequest?.(jsonResponse(200, {}));
    await original;
    expect(session.snapshot().phase).toBe("lost");
    await session.submit("thread_1", "edited after uncertainty");
    expect(requests).toHaveLength(1);
    await session.retryUnconfirmed();
    expect(keysMinted).toBe(1);
    expect(requests).toEqual([
      { key: "submission_1", body: JSON.stringify({ message: "original" }) },
      { key: "submission_1", body: JSON.stringify({ message: "original" }) },
    ]);
    expect(session.snapshot()).toMatchObject({ phase: "idle", confirmedRunId: "run_1" });
  });

  it.each([
    [400, "INVALID_REQUEST"], [401, "UNAUTHORIZED"], [409, "IDEMPOTENCY_KEY_CONFLICT"],
    [409, "THREAD_BUSY"], [429, "RATE_LIMITED"], [503, "QUEUE_CAPACITY_EXCEEDED"],
    [503, "PRINCIPAL_QUEUE_CAPACITY_EXCEEDED"], [503, "SUBMISSION_INITIALIZATION_FAILED"],
  ])("restores editing after definite rejection %i %s, then uses a new key", async (status, code) => {
    const storage = temporaryStorage();
    const requests: { key: string | null; body: string }[] = [];
    let keysMinted = 0;
    const session = createWorkbenchSession({
      getConfig: () => CONFIG, storage,
      mintKey: () => `submission_${++keysMinted}`,
      fetchImpl: async (_input, init) => {
        requests.push({ key: new Headers(init?.headers).get("Idempotency-Key"), body: String(init?.body) });
        return requests.length === 1 ? rejected(status, code) : jsonResponse(202, ACCEPTED);
      },
    });
    await session.submit("thread_1", "first draft");
    expect(session.snapshot()).toMatchObject({ phase: "failed", rejectedDraft: "first draft", prompt: null });
    expect(storage.entries.size).toBe(0);
    await session.submit("thread_1", "edited draft");
    expect(requests[1]).toEqual({ key: "submission_2", body: JSON.stringify({ message: "edited draft" }) });
    expect(session.snapshot().phase).toBe("idle");
  });

  it.each([
    [500, { error: "execution failed after acceptance" }],
    [503, { error: "upstream unavailable" }],
    [503, { error: { code: "UNKNOWN_FAILURE", message: "unavailable" } }],
    [200, {}], [202, { ...ACCEPTED, status: "UNKNOWN" }],
  ])("retains confirmation identity for uncertain HTTP %i body %j", async (status, body) => {
    const storage = temporaryStorage();
    let calls = 0;
    const session = createWorkbenchSession({
      getConfig: () => CONFIG, storage, mintKey: () => "original-key",
      fetchImpl: async () => { calls += 1; return jsonResponse(status, body); },
    });
    await session.submit("thread_1", "original");
    expect(calls).toBe(1);
    expect(session.snapshot()).toMatchObject({ phase: "lost", prompt: "original", rejectedDraft: null });
    expect([...storage.entries.values()].map((value) => JSON.parse(value))).toEqual([
      { threadId: "thread_1", message: "original", key: "original-key" },
    ]);
  });

  it("restores after refresh only for the same Runtime and Thread, without persisting credentials", async () => {
    const storage = temporaryStorage();
    let config: ConnectionConfig = { baseUrl: "http://runtime-a.example/v1/../", apiKey: "test-credential-not-persisted" };
    const first = createWorkbenchSession({
      getConfig: () => config, storage, mintKey: () => "recover-key",
      fetchImpl: async () => { throw new TypeError("connection lost"); },
    });
    await first.submit("thread_1", "original message");
    const serialized = JSON.stringify([...storage.entries]);
    expect(serialized).not.toContain(config.apiKey);
    expect(serialized).not.toContain("apiKey");

    const calls: { url: string; key: string | null; body: string }[] = [];
    const refreshed = createWorkbenchSession({
      getConfig: () => config, storage,
      fetchImpl: async (input, init) => {
        calls.push({ url: String(input), key: new Headers(init?.headers).get("Idempotency-Key"), body: String(init?.body) });
        return jsonResponse(202, ACCEPTED);
      },
    });
    config = { baseUrl: "http://runtime-b.example", apiKey: "another-test-credential" };
    refreshed.restore("thread_1");
    expect(refreshed.snapshot().phase).toBe("idle");
    config = { ...config, baseUrl: "http://runtime-a.example/" };
    refreshed.restore("thread_2");
    expect(refreshed.snapshot().phase).toBe("idle");
    refreshed.restore("thread_1");
    expect(refreshed.snapshot()).toMatchObject({ phase: "lost", prompt: "original message" });
    expect(calls).toHaveLength(0);
    await refreshed.retryUnconfirmed();
    expect(calls).toEqual([{
      url: "http://runtime-a.example/v1/threads/thread_1/turns", key: "recover-key",
      body: JSON.stringify({ message: "original message" }),
    }]);
    expect(storage.entries.size).toBe(0);
  });

  it("requires a separate confirmation after changing the displayed submission's Runtime", async () => {
    const storage = temporaryStorage();
    let config = { baseUrl: "http://runtime-a.example", apiKey: "key" };
    let calls = 0;
    const session = createWorkbenchSession({
      getConfig: () => config, storage,
      fetchImpl: async () => { calls += 1; throw new TypeError("offline"); },
    });
    await session.submit("thread_1", "A's message");
    config = { ...config, baseUrl: "http://runtime-b.example" };
    await session.submit("thread_1", "B's message");
    config = { ...config, baseUrl: "http://runtime-a.example" };
    await session.retryUnconfirmed();
    expect(calls).toBe(2);
    expect(session.snapshot()).toMatchObject({ phase: "lost", prompt: "A's message" });
  });

  it("clears only the explicitly forgotten submission and creates a new key for the next send", async () => {
    const storage = temporaryStorage();
    let key = 0;
    const sentKeys: (string | null)[] = [];
    const session = createWorkbenchSession({
      getConfig: () => CONFIG, storage, mintKey: () => `key_${++key}`,
      fetchImpl: async (_input, init) => {
        sentKeys.push(new Headers(init?.headers).get("Idempotency-Key"));
        throw new TypeError("offline");
      },
    });
    await session.submit("thread_1", "uncertain");
    session.restore("thread_2");
    await session.submit("thread_2", "another uncertain request");
    expect(storage.entries.size).toBe(2);
    session.restore("thread_1");
    session.abandonUnconfirmed();
    expect(session.snapshot().phase).toBe("idle");
    expect(storage.entries.size).toBe(1);
    await session.submit("thread_1", "new request");
    expect(sentKeys).toEqual(["key_1", "key_2", "key_3"]);
  });
});
