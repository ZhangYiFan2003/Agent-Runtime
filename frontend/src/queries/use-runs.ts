import { useQuery } from "@tanstack/react-query";
import { fetchRuns } from "../api/runs";
import { getConnection } from "../lib/connection";
import { queryKeys } from "./keys";

/** All runs across threads (`GET /v1/runs`), newest first is applied in the view layer. */
export function useRuns() {
  return useQuery({
    queryKey: queryKeys.runs,
    queryFn: () => fetchRuns(getConnection()),
    retry: 1,
    staleTime: 5_000,
  });
}
