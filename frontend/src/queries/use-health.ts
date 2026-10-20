import { useQuery } from "@tanstack/react-query";
import { fetchHealth } from "../api/health";
import { useConnection } from "../lib/connection";
import { queryKeys } from "./keys";

/** Live health of the configured Runtime (sidebar status + Settings page). */
export function useHealth() {
  const connection = useConnection();
  return useQuery({
    queryKey: queryKeys.forConnection(connection).health,
    queryFn: ({ signal }) => fetchHealth(connection.config, undefined, signal),
    // Limited retry only — an unreachable runtime must not spin forever.
    retry: 1,
    staleTime: 10_000,
  });
}
