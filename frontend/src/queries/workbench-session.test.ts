import { describe, expect, it } from "vitest";
import {
  SUBMISSION_LOST_MESSAGE,
  TURN_ALREADY_RUNNING_MESSAGE,
  createWorkbenchSession,
} from "./workbench-session";

const CONFIG = { baseUrl: "", apiKey: "key" };

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
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
      fetchImpl: async () => jsonResponse(409, { error: "thread turn already running" }),
    });
    await session.submit("thread_1", "again");
    const state = session.snapshot();
    expect(state.phase).toBe("failed");
    expect(state.errorStatus).toBe(409);
    expect(state.errorMessage).toBe(TURN_ALREADY_RUNNING_MESSAGE);
    expect(state.prompt).toBeNull();

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
    expect(session.snapshot().phase).toBe("idle");
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
  });
});
