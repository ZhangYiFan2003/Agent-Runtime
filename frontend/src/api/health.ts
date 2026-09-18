import { apiGet, ApiError, type ApiClientOptions } from "./client";
import { healthSchema, type HealthDto } from "./dto/health";
import { adaptHealth, type HealthView } from "./adapters/health";
import type { ConnectionConfig } from "../lib/connection";

/** Fetches and parses `GET /health`. Throws `ApiError`. */
export async function fetchHealth(config: ConnectionConfig): Promise<HealthView> {
  const raw: unknown = await apiGet("/health", toClientOptions(config));
  const dto: HealthDto = healthSchema.parse(raw);
  return adaptHealth(dto);
}

function toClientOptions(config: ConnectionConfig): ApiClientOptions {
  return { baseUrl: config.baseUrl, apiKey: config.apiKey };
}

export type ConnectionTestResult =
  | { ok: true; health: HealthView }
  | { ok: false; error: { status: number; code?: string; message: string } };

/**
 * Settings-page "Test Connection": hits `/health` with the given config and
 * returns a display-ready result. Never throws for HTTP/network failures;
 * the normalized error is part of the result.
 */
export async function testConnection(
  config: ConnectionConfig,
  fetchImpl?: typeof fetch,
): Promise<ConnectionTestResult> {
  try {
    const raw: unknown = await apiGet("/health", { ...toClientOptions(config), fetchImpl });
    const health = adaptHealth(healthSchema.parse(raw));
    return { ok: true, health };
  } catch (error) {
    if (error instanceof ApiError) {
      return {
        ok: false,
        error: { status: error.status, code: error.code, message: error.message },
      };
    }
    return {
      ok: false,
      error: {
        status: 0,
        message: error instanceof Error ? error.message : "Unexpected error",
      },
    };
  }
}
