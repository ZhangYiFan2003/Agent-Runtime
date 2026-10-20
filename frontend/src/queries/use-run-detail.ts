import { useQuery } from "@tanstack/react-query";
import {
  fetchRun,
  fetchRunArtifacts,
  fetchClaimProvenance,
  fetchRunClaims,
  fetchRunChildren,
  fetchRunMetrics,
  fetchRunTrace,
} from "../api/run";
import { useConnection } from "../lib/connection";
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
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).run(runId),
    queryFn: ({ signal }) => fetchRun(connection.config, runId, undefined, signal),
    enabled: runId !== "",
    retry: 1,
    refetchInterval: (query) =>
      runDetailRefetchInterval(query.state.data?.status, RUN_REFETCH_INTERVAL_MS),
  });
}

export function useRunChildren(runId: string, runStatus: string | undefined) {
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).children(runId),
    queryFn: ({ signal }) => fetchRunChildren(connection.config, runId, undefined, signal),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}

export function useRunArtifacts(runId: string, runStatus: string | undefined) {
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).artifacts(runId),
    queryFn: ({ signal }) => fetchRunArtifacts(connection.config, runId, undefined, signal),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}

export function useRunClaims(runId: string, runStatus: string | undefined) {
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).claims(runId),
    queryFn: ({ signal }) => fetchRunClaims(connection.config, runId, undefined, signal),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}

export function useClaimProvenance(claimId: string | null) {
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).claimProvenance(claimId ?? ""),
    queryFn: ({ signal }) => fetchClaimProvenance(connection.config, claimId as string, undefined, signal),
    enabled: claimId !== null && claimId !== "",
    retry: 1,
  });
}

export function useRunTrace(runId: string, runStatus: string | undefined) {
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).trace(runId),
    queryFn: ({ signal }) => fetchRunTrace(connection.config, runId, undefined, signal),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}

export function useRunMetrics(runId: string, runStatus: string | undefined) {
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).metrics(runId),
    queryFn: ({ signal }) => fetchRunMetrics(connection.config, runId, undefined, signal),
    enabled: runId !== "" && runStatus !== undefined,
    retry: 1,
    refetchInterval: () => runDetailRefetchInterval(runStatus, DETAIL_REFETCH_INTERVAL_MS),
  });
}
