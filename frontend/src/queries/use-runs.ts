import { useInfiniteQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { fetchRuns, type RunPage } from "../api/runs";
import type { RunSummary } from "../api/adapters/run-summary";
import { useConnection, type ConnectionSnapshot } from "../lib/connection";
import { ALL_FILTER, type RunsFilter, type RunsSortKey } from "../lib/runs-view";
import { queryKeys } from "./keys";

export function runsQueryOptions(connection: ConnectionSnapshot, filter: RunsFilter, sort: RunsSortKey) {
  const params = { search: filter.search.trim(), status: filter.status, strategy: filter.strategy, sort };
  return {
    queryKey: [...queryKeys.forConnection(connection).runs, params] as const,
    queryFn: ({ signal, pageParam }: { signal: AbortSignal; pageParam: string | null }) =>
      fetchRuns(connection.config, { ...params, cursor: pageParam, signal }),
    initialPageParam: null as string | null,
    getNextPageParam: (page: RunPage) => page.nextCursor,
    retry: 1,
    staleTime: 5_000,
    refetchOnMount: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  };
}

/** Overlapping pages may contain an updated run; one visible row per stable identity. */
export function flattenRunPages(pages: RunPage[]): RunSummary[] {
  const runs = new Map<string, RunSummary>();
  for (const page of pages) for (const run of page.runs) {
    if (!runs.has(run.runId)) runs.set(run.runId, run);
  }
  return [...runs.values()];
}

/** Explicit pagination only. Refresh returns to the first page, never replays old pages. */
export function useRuns(filter: RunsFilter = { search: "", status: ALL_FILTER, strategy: ALL_FILTER }, sort: RunsSortKey = "newest") {
  const connection = useConnection();
  const queryClient = useQueryClient();
  const options = runsQueryOptions(connection, filter, sort);
  const query = useInfiniteQuery(options);
  const refresh = async () => {
    await queryClient.cancelQueries({ queryKey: options.queryKey, exact: true });
    queryClient.setQueryData<InfiniteData<RunPage, string | null>>(options.queryKey, (data) =>
      data ? { pages: data.pages.slice(0, 1), pageParams: [null] } : data);
    return query.refetch();
  };
  return { ...query, refresh };
}
