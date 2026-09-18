import { useQuery } from "@tanstack/react-query";
import { fetchRun, fetchRunChildren, fetchRunMetrics, fetchRunTrace } from "../api/run";
import { getConnection } from "../lib/connection";
import {
  DETAIL_REFETCH_INTERVAL_MS,
  RUN_REFETCH_INTERVAL_MS,
  runDetailRefetchInterval,
} from "../lib/run-detail-view";
import { queryKeys } from "./keys";

/**
 * Run Detail queries. All poll while the run is non-terminal and stop on a
 * terminal status; `enabled` guards against an empty runId. Trace and
 * metrics return `null` when the run has no trace yet (404), so the UI can
 * render a neutral "not available yet" state instead of an error.
 */
export function useRun(runId: string) {
  return useQuery({
    queryKey: queryKeys.run(runId),
    queryFn: () => fetchRun(getConnection(), runId),
    enabled: runId !== "",
    retry: 1,
    refetchInterval: (query) =>
      runDetailRefetchInterval(query.state.data?.status, RUN_REFETCH_INTERVAL_MS),
  });
}

export function useRunChildren(runId: string, runStatus: string | undefined) {
  return useQuery({
    queryKey: queryKeys.children(runId),
    queryFn: () => fetchRunChildren(getConnection(), runId),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}

export function useRunTrace(runId: string, runStatus: string | undefined) {
  return useQuery({
    queryKey: queryKeys.trace(runId),
    queryFn: () => fetchRunTrace(getConnection(), runId),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}

export function useRunMetrics(runId: string, runStatus: string | undefined) {
  return useQuery({
    queryKey: queryKeys.metrics(runId),
    queryFn: () => fetchRunMetrics(getConnection(), runId),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}
