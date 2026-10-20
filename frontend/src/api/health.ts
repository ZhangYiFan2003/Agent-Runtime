import { apiGet, ApiError, type ApiClientOptions } from "./client";
import { authCheckSchema, healthSchema, type HealthDto } from "./dto/health";
import { adaptHealth, type HealthView } from "./adapters/health";
import type { ConnectionConfig } from "../lib/connection";

/** Fetches and parses `GET /health`. Throws `ApiError`. */
export async function fetchHealth(
  config: ConnectionConfig,
  fetchImpl?: typeof fetch,
  signal?: AbortSignal,
): Promise<HealthView> {
  const raw: unknown = await apiGet("/health", { ...toClientOptions(config), fetchImpl, signal });
  const dto: HealthDto = healthSchema.parse(raw);
  return adaptHealth(dto);
}

function toClientOptions(config: ConnectionConfig): ApiClientOptions {
  return { baseUrl: config.baseUrl, apiKey: config.apiKey };
}

interface ConnectionTestError {
  status: number;
  code?: string;
  message: string;
}

export type ConnectionTestResult =
  | { ok: true; health: HealthView; healthError?: never }
  | { ok: true; health: null; healthError: ConnectionTestError }
  | { ok: false; error: ConnectionTestError };

function connectionError(error: unknown): ConnectionTestError {
  if (error instanceof ApiError) {
    return { status: error.status, code: error.code, message: error.message };
  }
  return {
    status: 0,
    message: error instanceof Error ? error.message : "Unexpected error",
  };
}

/**
 * Verify credentials before fetching independent public dependency information.
 * A health failure cannot invalidate credentials accepted by the protected probe.
 */
export async function testConnection(
  config: ConnectionConfig,
  fetchImpl?: typeof fetch,
): Promise<ConnectionTestResult> {
  try {
    const raw: unknown = await apiGet("/v1/auth/check", {
      ...toClientOptions(config),
      fetchImpl,
    });
    if (!authCheckSchema.safeParse(raw).success) {
      return {
        ok: false,
        error: {
          status: 0,
          code: "INVALID_AUTH_RESPONSE",
          message: "Invalid credential verification response: expected authenticated: true.",
        },
      };
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return {
        ok: false,
        error: {
          status: 404,
          code: "AUTH_CHECK_UNSUPPORTED",
          message: "This Runtime does not support credential verification (/v1/auth/check).",
        },
      };
    }
    return { ok: false, error: connectionError(error) };
  }

  try {
    return { ok: true, health: await fetchHealth(config, fetchImpl) };
  } catch (error) {
    return { ok: true, health: null, healthError: connectionError(error) };
  }
}
