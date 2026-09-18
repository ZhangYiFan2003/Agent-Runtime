import { apiGet } from "./client";
import { runListSchema } from "./dto/run";
import { adaptRunView, type RunView } from "./adapters/run";
import type { ConnectionConfig } from "../lib/connection";

/** Fetches and parses `GET /v1/runs` → adapted RunView list. Throws ApiError. */
export async function fetchRuns(
  config: ConnectionConfig,
  fetchImpl?: typeof fetch,
): Promise<RunView[]> {
  const raw: unknown = await apiGet("/v1/runs", {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    fetchImpl,
  });
  const dto = runListSchema.parse(raw);
  return dto.runs.map(adaptRunView);
}
