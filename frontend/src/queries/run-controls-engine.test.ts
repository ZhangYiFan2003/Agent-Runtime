import { describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import type { InterruptSummary } from "../api/adapters/run";
import { createRunControlsEngine } from "./run-controls-engine";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function makeQueryClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

interface RecordedRequest {
  url: string;
  idempotencyKey: string | null;
  body: unknown;
}

function recorder(behavior: (call: number) => Promise<Response> | Response): {
  fetchImpl: typeof fetch;
  requests: RecordedRequest[];
} {
  const requests: RecordedRequest[] = [];
  let call = 0;
  const fetchImpl: typeof fetch = async (input, init) => {
    call += 1;
    const headers = new Headers(init?.headers);
    requests.push({
      url: String(input),
      idempotencyKey: headers.get("Idempotency-Key"),
      body: init?.body === undefined || init?.body === null ? null : JSON.parse(String(init.body)),
    });
    return behavior(call);
  };
  return { fetchImpl, requests };
}

const noSleep = async () => {};

const childInterrupt: InterruptSummary = {
  runId: "run_child",
  invocationId: "run_child:call_0",
  interruptType: "tool_approval",
  toolName: "shell",
  reason: "policy requires approval",
  createdAt: "2026-09-19T10:00:03+00:00",
};

describe("createRunControlsEngine — idempotency keys", () => {
  it("one click mints one key; the next click mints a new key", async () => {
    const { fetchImpl, requests } = recorder(() => jsonResponse(200, {}));
    const mintKey = vi.fn(() => `minted-${mintKey.mock.calls.length}`);
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient: makeQueryClient(),
      deps: { fetchImpl, sleep: noSleep, mintKey },
    });
    await engine.resume();
    await engine.resume();
    expect(requests.map((r) => r.idempotencyKey)).toEqual(["minted-1", "minted-2"]);
  });

  it("reuses the exact same key across transport-failure retries of one click", async () => {
    const { fetchImpl, requests } = recorder((call) => {
      if (call === 1) throw new ApiError({ status: 0, message: "network down" });
      return jsonResponse(202, { status: "queued" });
    });
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient: makeQueryClient(),
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "fixed-key" },
    });
    const ok = await engine.resume();
    expect(ok).toBe(true);
    expect(requests.map((r) => r.idempotencyKey)).toEqual(["fixed-key", "fixed-key"]);
    expect(requests[0].url).toContain("/v1/runs/run_a/resume");
  });

  it("accepts a 202 resume response as success", async () => {
    const { fetchImpl } = recorder(() => jsonResponse(202, { status: "queued" }));
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient: makeQueryClient(),
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    const ok = await engine.resume();
    expect(ok).toBe(true);
    expect(engine.snapshot().feedback).toEqual({ tone: "success", message: "Run resumed" });
  });

  it("a rapid double click issues exactly one request", async () => {
    const { fetchImpl, requests } = recorder(() => jsonResponse(200, {}));
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient: makeQueryClient(),
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    const [first, second] = [engine.cancel(), engine.cancel()];
    expect(await first).toBe(true);
    expect(await second).toBe(false);
    expect(requests).toHaveLength(1);
  });
});

describe("createRunControlsEngine — invalidation and errors", () => {
  it("cancel success hits /cancel and invalidates exactly the run family", async () => {
    const { fetchImpl } = recorder(() => jsonResponse(200, { status: "CANCELLED" }));
    const queryClient = makeQueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient,
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    await engine.cancel();
    const keys = invalidate.mock.calls.map((call) => call[0]?.queryKey);
    expect(keys).toContainEqual(["runs", "run_a"]);
    expect(keys).toContainEqual(["runs", "run_a", "children"]);
    expect(keys).toContainEqual(["runs", "run_a", "trace"]);
    expect(keys).toContainEqual(["runs", "run_a", "metrics"]);
    // never a global invalidation
    expect(keys).not.toContainEqual(["runs"]);
    expect(engine.snapshot().feedback).toEqual({ tone: "success", message: "Cancel requested" });
  });

  it("checkpoint_conflict surfaces the conflict message and silently refetches", async () => {
    const { fetchImpl } = recorder(() =>
      jsonResponse(409, { error: { code: "checkpoint_conflict", message: "version moved on" } }),
    );
    const queryClient = makeQueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient,
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    const ok = await engine.cancel();
    expect(ok).toBe(false);
    expect(engine.snapshot().feedback).toEqual({
      tone: "error",
      message: "Run was updated concurrently. Refreshed to the latest state.",
    });
    expect(invalidate).toHaveBeenCalled();
  });

  it("operation_in_progress surfaces the conflict message and silently refetches", async () => {
    const { fetchImpl } = recorder(() =>
      jsonResponse(409, { error: { code: "operation_in_progress", message: "busy" } }),
    );
    const queryClient = makeQueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient,
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    await engine.interrupt();
    expect(engine.snapshot().feedback?.message).toBe(
      "Another control operation is already in progress.",
    );
    expect(invalidate).toHaveBeenCalled();
  });

  it("idempotency_key_conflict is surfaced and does NOT trigger a refetch", async () => {
    const { fetchImpl } = recorder(() =>
      jsonResponse(409, { error: { code: "idempotency_key_conflict", message: "key reuse" } }),
    );
    const queryClient = makeQueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient,
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    await engine.cancel();
    expect(engine.snapshot().feedback?.message).toContain("Idempotency key conflict");
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("no auto-retry after a received HTTP 500", async () => {
    const { fetchImpl, requests } = recorder(() =>
      jsonResponse(500, { error: { code: "internal", message: "boom" } }),
    );
    const engine = createRunControlsEngine({
      runId: "run_a",
      queryClient: makeQueryClient(),
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    await engine.cancel();
    expect(requests).toHaveLength(1);
    expect(engine.snapshot().feedback).toEqual({ tone: "error", message: "boom" });
  });
});

describe("createRunControlsEngine — approvals target the child run", () => {
  it("approve posts to the child run id with the exact invocation id", async () => {
    const { fetchImpl, requests } = recorder(() => jsonResponse(200, {}));
    const queryClient = makeQueryClient();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const engine = createRunControlsEngine({
      runId: "run_parent",
      queryClient,
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    const ok = await engine.resolveApproval(childInterrupt, "approve");
    expect(ok).toBe(true);
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toContain("/v1/runs/run_child/resume");
    expect(requests[0].url).not.toContain("/v1/runs/run_parent/");
    expect(requests[0].body).toEqual({
      decision: "approve",
      invocation_id: "run_child:call_0",
    });
    // Both the child family and the parent page aggregates refresh.
    const keys = invalidate.mock.calls.map((call) => call[0]?.queryKey);
    expect(keys).toContainEqual(["runs", "run_child"]);
    expect(keys).toContainEqual(["runs", "run_parent"]);
    expect(engine.snapshot().feedback).toEqual({ tone: "success", message: "Approval submitted" });
  });

  it("reject posts to the child run id with the exact invocation id", async () => {
    const { fetchImpl, requests } = recorder(() => jsonResponse(200, {}));
    const engine = createRunControlsEngine({
      runId: "run_parent",
      queryClient: makeQueryClient(),
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    await engine.resolveApproval(childInterrupt, "reject");
    expect(requests[0].url).toContain("/v1/runs/run_child/resume");
    expect(requests[0].body).toEqual({
      decision: "reject",
      invocation_id: "run_child:call_0",
    });
    expect(engine.snapshot().feedback).toEqual({ tone: "success", message: "Rejection submitted" });
  });
});

describe("createRunControlsEngine — pending scoping", () => {
  it("pending state is scoped per runId", async () => {
    let release!: (value: Response) => void;
    const gate = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const { fetchImpl } = recorder(() => gate);
    const queryClient = makeQueryClient();
    const engineA = createRunControlsEngine({
      runId: "run_a",
      queryClient,
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    const engineB = createRunControlsEngine({
      runId: "run_b",
      queryClient,
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    const pending = engineA.cancel();
    expect(engineA.snapshot().anyPending).toBe(true);
    expect(engineA.snapshot().pending.cancel).toBe(true);
    expect(engineB.snapshot().anyPending).toBe(false);
    release(jsonResponse(200, {}));
    await pending;
    expect(engineA.snapshot().anyPending).toBe(false);
  });

  it("tracks approval cards independently per invocation key", async () => {
    let release!: (value: Response) => void;
    const gate = new Promise<Response>((resolve) => {
      release = resolve;
    });
    const { fetchImpl } = recorder(() => gate);
    const engine = createRunControlsEngine({
      runId: "run_parent",
      queryClient: makeQueryClient(),
      deps: { fetchImpl, sleep: noSleep, mintKey: () => "k" },
    });
    const other: InterruptSummary = { ...childInterrupt, invocationId: "run_child:call_1" };
    const pending = engine.resolveApproval(childInterrupt, "approve");
    const key = `${childInterrupt.runId}:${childInterrupt.invocationId}`;
    expect(engine.snapshot().pending.approvals.get(key)).toBe(true);
    expect(engine.snapshot().pending.approvals.has(`${other.runId}:${other.invocationId}`)).toBe(
      false,
    );
    release(jsonResponse(200, {}));
    await pending;
    expect(engine.snapshot().pending.approvals.size).toBe(0);
  });
});
