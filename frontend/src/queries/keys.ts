/** Central query keys. Phase 1 only uses `health`; run/trace/metrics keys
 *  are declared now so later phases share one registry. */
export const queryKeys = {
  health: ["health"] as const,
  runs: ["runs"] as const,
  run: (runId: string) => ["runs", runId] as const,
  children: (runId: string) => ["runs", runId, "children"] as const,
  trace: (runId: string) => ["runs", runId, "trace"] as const,
  metrics: (runId: string) => ["runs", runId, "metrics"] as const,
};
