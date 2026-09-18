import { describe, expect, it } from "vitest";
import { testConnection } from "./health";

const HEALTH_BODY = {
  status: "ok",
  workers: 4,
  database: "ok",
  storage_backend: "sqlite",
  capacity: {
    queued_runs: 0,
    active_runs: 1,
    max_queued_runs: 64,
    max_active_runs: 4,
  },
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("testConnection", () => {
  it("returns the adapted health view on success", async () => {
    const fetchImpl: typeof fetch = async () => jsonResponse(200, HEALTH_BODY);
    const result = await testConnection({ baseUrl: "", apiKey: "" }, fetchImpl);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.health.status).toBe("ok");
      expect(result.health.storageBackend).toBe("sqlite");
      expect(result.health.workers).toBe(4);
      expect(result.health.capacity).toEqual({
        activeRuns: 1,
        queuedRuns: 0,
        maxActiveRuns: 4,
        maxQueuedRuns: 64,
      });
    }
  });

  it("surfaces HTTP failures with status and normalized message", async () => {
    const fetchImpl: typeof fetch = async () => jsonResponse(401, { error: "unauthorized" });
    const result = await testConnection({ baseUrl: "", apiKey: "wrong" }, fetchImpl);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(401);
      expect(result.error.message).toBe("unauthorized");
    }
  });

  it("surfaces structured control errors with code", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(503, {
        error: { code: "queue_capacity_exceeded", message: "backlog is at capacity" },
      });
    const result = await testConnection({ baseUrl: "", apiKey: "" }, fetchImpl);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(503);
      expect(result.error.code).toBe("queue_capacity_exceeded");
      expect(result.error.message).toBe("backlog is at capacity");
    }
  });

  it("surfaces network failures without throwing", async () => {
    const fetchImpl: typeof fetch = async () => {
      throw new TypeError("fetch failed");
    };
    const result = await testConnection({ baseUrl: "", apiKey: "" }, fetchImpl);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(0);
      expect(result.error.message).toContain("fetch failed");
    }
  });
});
