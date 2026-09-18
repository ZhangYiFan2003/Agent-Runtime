import { useQuery } from "@tanstack/react-query";
import { fetchHealth } from "../api/health";
import { getConnection } from "../lib/connection";
import { queryKeys } from "./keys";

/** Live health of the configured Runtime (sidebar status + Settings page). */
export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: () => fetchHealth(getConnection()),
    // Limited retry only — an unreachable runtime must not spin forever.
    retry: 1,
    staleTime: 10_000,
  });
}
