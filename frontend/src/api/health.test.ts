import { describe, expect, it, vi } from "vitest";
import { fetchHealth, testConnection } from "./health";

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
  it("verifies credentials before returning the adapted health view", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      jsonResponse(200, String(input).endsWith("/v1/auth/check")
        ? { authenticated: true, extra: "compatible" }
        : HEALTH_BODY),
    );
    const result = await testConnection({ baseUrl: "http://runtime", apiKey: "runtime-test" }, fetchImpl);
    expect(result.ok).toBe(true);
    expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual([
      "http://runtime/v1/auth/check",
      "http://runtime/health",
    ]);
    expect(fetchImpl.mock.calls[0]?.[1]?.headers).toMatchObject({
      Authorization: "Bearer runtime-test",
    });
    if (result.ok && result.health) {
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

  it.each([
    { apiKey: "wrong", status: 401 },
    { apiKey: "", status: 401 },
    { apiKey: "forbidden", status: 403 },
  ])("rejects auth $status for '$apiKey' even while public health returns 200", async ({ apiKey, status }) => {
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      String(input).endsWith("/health")
        ? jsonResponse(200, HEALTH_BODY)
        : jsonResponse(status, { error: "unauthorized" }),
    );
    const config = { baseUrl: "", apiKey };
    expect(await fetchHealth(config, fetchImpl)).toMatchObject({ status: "ok" });
    fetchImpl.mockClear();
    const result = await testConnection(config, fetchImpl);
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.status).toBe(status);
      expect(result.error.message).toBe("unauthorized");
    }
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("/v1/auth/check");
    if (!apiKey) {
      expect(fetchImpl.mock.calls[0]?.[1]?.headers).not.toHaveProperty("Authorization");
    }
  });

  it("reports an unsupported auth endpoint without falling back to public health", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async (input) =>
      String(input).endsWith("/health")
        ? jsonResponse(200, HEALTH_BODY)
        : jsonResponse(404, { error: "not found" }),
    );
    const result = await testConnection({ baseUrl: "", apiKey: "runtime-test" }, fetchImpl);
    expect(result).toMatchObject({
      ok: false,
      error: { status: 404, code: "AUTH_CHECK_UNSUPPORTED" },
    });
    if (!result.ok) expect(result.error.message).toContain("does not support");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it.each([{}, { authenticated: false }, { authenticated: "true" }, { authenticated: 1 }, null, [], "ok"])(
    "rejects malformed successful auth response %j",
    async (body) => {
      const fetchImpl = vi.fn<typeof fetch>(async () => jsonResponse(200, body));
      const result = await testConnection({ baseUrl: "", apiKey: "runtime-test" }, fetchImpl);
      expect(result).toMatchObject({
        ok: false,
        error: { code: "INVALID_AUTH_RESPONSE" },
      });
      expect(fetchImpl).toHaveBeenCalledTimes(1);
    },
  );

  it("keeps verified credentials separate from a reported database error", async () => {
    const fetchImpl: typeof fetch = async (input) =>
      jsonResponse(200, String(input).endsWith("/v1/auth/check")
        ? { authenticated: true }
        : { ...HEALTH_BODY, workers: 0, database: "error" });
    const result = await testConnection({ baseUrl: "", apiKey: "runtime-test" }, fetchImpl);
    expect(result).toMatchObject({ ok: true, health: { database: "error", workers: 0 } });
  });

  it("retains authentication success when public health is unavailable", async () => {
    const fetchImpl: typeof fetch = async (input) =>
      String(input).endsWith("/v1/auth/check")
        ? jsonResponse(200, { authenticated: true })
        : jsonResponse(503, { error: "database unavailable" });
    const result = await testConnection({ baseUrl: "", apiKey: "runtime-test" }, fetchImpl);
    expect(result).toEqual({
      ok: true,
      health: null,
      healthError: { status: 503, code: undefined, message: "database unavailable" },
    });
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
