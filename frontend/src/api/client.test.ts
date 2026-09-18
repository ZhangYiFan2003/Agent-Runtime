import { describe, expect, it } from "vitest";
import { ApiError, apiGet, normalizeErrorBody } from "./client";

describe("normalizeErrorBody", () => {
  it("normalizes the structured error shape", () => {
    const shape = normalizeErrorBody(409, {
      error: {
        code: "invalid_run_transition",
        message: "run run_1 cannot be resumed from COMPLETED",
        run_id: "run_1",
        status: "COMPLETED",
        operation: "resume",
        details: { extra: true },
      },
    });
    expect(shape).toEqual({
      status: 409,
      code: "invalid_run_transition",
      message: "run run_1 cannot be resumed from COMPLETED",
      details: { extra: true },
    });
  });

  it("normalizes the plain-string error shape", () => {
    const shape = normalizeErrorBody(401, { error: "unauthorized" });
    expect(shape).toEqual({ status: 401, message: "unauthorized" });
  });

  it("falls back to a status message for empty bodies", () => {
    expect(normalizeErrorBody(500, null).message).toBe("Request failed with status 500");
    expect(normalizeErrorBody(404, {}).message).toBe("Request failed with status 404");
  });

  it("uses code as message when structured message is missing", () => {
    const shape = normalizeErrorBody(422, { error: { code: "invalid_request" } });
    expect(shape.code).toBe("invalid_request");
    expect(shape.message).toBe("invalid_request");
  });
});

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("apiGet", () => {
  const base = { baseUrl: "http://127.0.0.1:8080", apiKey: "test-key" };

  it("sends bearer auth and returns parsed JSON", async () => {
    let seenAuth: string | null = null;
    const fetchImpl: typeof fetch = async (_input, init) => {
      seenAuth = (init?.headers as Record<string, string>).Authorization;
      return jsonResponse(200, { status: "ok" });
    };
    const body = await apiGet("/health", { ...base, fetchImpl });
    expect(body).toEqual({ status: "ok" });
    expect(seenAuth).toBe("Bearer test-key");
  });

  it("throws ApiError with normalized message on HTTP failure", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(404, { error: "run_not_found" });
    const error: unknown = await apiGet("/v1/runs/run_x", { ...base, fetchImpl }).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    if (error instanceof ApiError) {
      expect(error.status).toBe(404);
      expect(error.message).toBe("run_not_found");
    }
  });

  it("wraps network failures as ApiError with status 0", async () => {
    const fetchImpl: typeof fetch = async () => {
      throw new TypeError("fetch failed");
    };
    const error: unknown = await apiGet("/health", { ...base, fetchImpl }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    if (error instanceof ApiError) {
      expect(error.status).toBe(0);
    }
  });
});
