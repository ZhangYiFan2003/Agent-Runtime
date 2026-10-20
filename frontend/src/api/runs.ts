import { apiGet } from "./client";
import { runSummaryPageSchema } from "./dto/run-summary";
import { adaptRunSummary, type RunSummary } from "./adapters/run-summary";
import type { ConnectionConfig } from "../lib/connection";
import type { RunsSortKey } from "../lib/runs-view";

export interface RunPage { runs: RunSummary[]; nextCursor: string | null; hasMore: boolean }
export interface RunPageOptions {
  cursor?: string | null;
  limit?: number;
  search?: string;
  status?: string;
  strategy?: string;
  sort?: RunsSortKey;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

/** One bounded page. Global filtering and sorting belong to the database. */
export async function fetchRuns(
  config: ConnectionConfig,
  options: RunPageOptions = {},
): Promise<RunPage> {
  const params = new URLSearchParams({ limit: String(options.limit ?? 50), sort: options.sort ?? "newest" });
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.search?.trim()) params.set("search", options.search.trim());
  if (options.status && options.status !== "all") params.set("status", options.status);
  if (options.strategy && options.strategy !== "all") params.set("strategy", options.strategy);
  const raw: unknown = await apiGet(`/v1/runs?${params}`, {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    fetchImpl: options.fetchImpl,
    signal: options.signal,
  });
  const dto = runSummaryPageSchema.parse(raw);
  return { runs: dto.runs.map(adaptRunSummary), nextCursor: dto.next_cursor, hasMore: dto.has_more };
}
