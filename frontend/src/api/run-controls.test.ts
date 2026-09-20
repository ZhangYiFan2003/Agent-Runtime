import { describe, expect, it } from "vitest";
import { cancelRun, interruptRun, requeueRun, resumeRun } from "./run-controls";
import { ApiError } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const CONFIG = { baseUrl: "", apiKey: "key" };
const OPTS = { idempotencyKey: "key-1" };

interface RecordedRequest {
  url: string;
  method: string;
  idempotencyKey: string | null;
  body: unknown;
}

function recordingFetch(response: () => Response): {
  fetchImpl: typeof fetch;
  requests: RecordedRequest[];
} {
  const requests: RecordedRequest[] = [];
  const fetchImpl: typeof fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    requests.push({
      url: String(input),
      method: init?.method ?? "GET",
      idempotencyKey: headers.get("Idempotency-Key"),
      body: init?.body === undefined || init?.body === null ? null : JSON.parse(String(init.body)),
    });
    return response();
  };
  return { fetchImpl, requests };
}

describe("run control mutations", () => {
  it("resumeRun posts decision + invocation_id with the Idempotency-Key header", async () => {
    const { fetchImpl, requests } = recordingFetch(() =>
      jsonResponse(202, { status: "queued" }),
    );
    await resumeRun(
      CONFIG,
      "run_a",
      { decision: "approve", invocationId: "run_a:call_0" },
      { ...OPTS, fetchImpl },
    );
    expect(requests).toHaveLength(1);
    expect(requests[0].method).toBe("POST");
    expect(requests[0].url).toContain("/v1/runs/run_a/resume");
    expect(requests[0].idempotencyKey).toBe("key-1");
    expect(requests[0].body).toEqual({ decision: "approve", invocation_id: "run_a:call_0" });
  });

  it("resumeRun sends an empty body for a plain resume and accepts 202", async () => {
    const { fetchImpl, requests } = recordingFetch(() =>
      jsonResponse(202, { status: "queued" }),
    );
    const result = await resumeRun(CONFIG, "run_a", {}, { ...OPTS, fetchImpl });
    expect(result).toEqual({ status: "queued" });
    expect(requests[0].body).toEqual({});
  });

  it("cancelRun posts to /cancel with an empty body", async () => {
    const { fetchImpl, requests } = recordingFetch(() =>
      jsonResponse(200, { run_id: "run_a", status: "CANCELLED" }),
    );
    await cancelRun(CONFIG, "run_a", { ...OPTS, fetchImpl });
    expect(requests[0].url).toContain("/v1/runs/run_a/cancel");
    expect(requests[0].idempotencyKey).toBe("key-1");
    expect(requests[0].body).toEqual({});
  });

  it("interruptRun omits an empty reason and submits a non-empty one", async () => {
    const { fetchImpl, requests } = recordingFetch(() =>
      jsonResponse(200, { run_id: "run_a", status: "INTERRUPTED" }),
    );
    await interruptRun(CONFIG, "run_a", {}, { ...OPTS, fetchImpl });
    expect(requests[0].url).toContain("/v1/runs/run_a/interrupt");
    expect(requests[0].body).toEqual({});

    await interruptRun(CONFIG, "run_a", { reason: "budget freeze" }, { ...OPTS, fetchImpl });
    expect(requests[1].body).toEqual({ reason: "budget freeze" });
  });

  it("requeueRun posts to /requeue and accepts 202", async () => {
    const { fetchImpl, requests } = recordingFetch(() =>
      jsonResponse(202, { status: "queued" }),
    );
    await requeueRun(CONFIG, "run_a", { ...OPTS, fetchImpl });
    expect(requests[0].url).toContain("/v1/runs/run_a/requeue");
    expect(requests[0].body).toEqual({});
  });

  it("normalizes a structured 409 into ApiError with the conflict code", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(409, {
        error: {
          code: "invalid_run_transition",
          message: "run is already terminal",
          run_id: "run_a",
          status: "CANCELLED",
          operation: "cancel",
        },
      });
    const error: unknown = await cancelRun(CONFIG, "run_a", { ...OPTS, fetchImpl }).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    if (error instanceof ApiError) {
      expect(error.status).toBe(409);
      expect(error.code).toBe("invalid_run_transition");
      expect(error.message).toBe("run is already terminal");
    }
  });
});
