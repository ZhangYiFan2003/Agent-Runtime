import type { RunStatusGroup, RunView } from "../api/adapters/run";

/**
 * Pure view logic for the Runs page: client-side filtering, sorting,
 * duration math and lineage labels. Kept out of components so behavior is
 * directly unit-testable. No backend status machine is re-implemented here —
 * classification comes from the adapter layer.
 */

export interface RunsFilter {
  search: string;
  /** "all" or an exact status string present in the data. */
  status: string;
  /** "all" or an exact execution_strategy string present in the data. */
  strategy: string;
}

export const ALL_FILTER = "all";

export type RunsSortKey = "newest" | "oldest" | "status" | "duration";

export const SORT_OPTIONS: Array<{ key: RunsSortKey; label: string }> = [
  { key: "newest", label: "Newest" },
  { key: "oldest", label: "Oldest" },
  { key: "status", label: "Status" },
  { key: "duration", label: "Duration" },
];

/** Console priority: in-flight first, terminal noise last. */
const STATUS_GROUP_ORDER: Record<RunStatusGroup, number> = {
  active: 0,
  waiting: 1,
  failure: 2,
  idle: 3,
  success: 4,
  unknown: 5,
};

/**
 * Wall-clock duration of a run: startedAt → completedAt (terminal) or
 * updatedAt (still in flight). Null when timestamps are missing or absurd —
 * callers render "—" and the Duration sort treats nulls as lowest.
 */
export function runDurationMs(run: RunView): number | null {
  const start = Date.parse(run.startedAt || run.createdAt);
  const endRaw = run.completedAt ?? run.updatedAt;
  const end = Date.parse(endRaw);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  const ms = end - start;
  return ms >= 0 ? ms : null;
}

export function deriveFilterOptions(runs: RunView[]): {
  statuses: string[];
  strategies: string[];
} {
  const statuses = new Set<string>();
  const strategies = new Set<string>();
  for (const run of runs) {
    statuses.add(run.status);
    if (run.executionStrategy) strategies.add(run.executionStrategy);
  }
  return { statuses: [...statuses].sort(), strategies: [...strategies].sort() };
}

export function filterRuns(runs: RunView[], filter: RunsFilter): RunView[] {
  const query = filter.search.trim().toLowerCase();
  return runs.filter((run) => {
    if (filter.status !== ALL_FILTER && run.status !== filter.status) return false;
    if (filter.strategy !== ALL_FILTER && run.executionStrategy !== filter.strategy) return false;
    if (query === "") return true;
    return (
      run.runId.toLowerCase().includes(query) ||
      run.threadId.toLowerCase().includes(query) ||
      (run.turnId ?? "").toLowerCase().includes(query) ||
      run.executionStrategy.toLowerCase().includes(query) ||
      run.runKind.toLowerCase().includes(query)
    );
  });
}

export function sortRuns(runs: RunView[], sort: RunsSortKey): RunView[] {
  const sorted = [...runs];
  switch (sort) {
    case "newest":
      sorted.sort((a, b) => timestampDesc(a.createdAt, b.createdAt));
      break;
    case "oldest":
      sorted.sort((a, b) => timestampDesc(b.createdAt, a.createdAt));
      break;
    case "status": {
      sorted.sort(
        (a, b) =>
          STATUS_GROUP_ORDER[a.statusGroup] - STATUS_GROUP_ORDER[b.statusGroup] ||
          timestampDesc(a.createdAt, b.createdAt),
      );
      break;
    }
    case "duration":
      sorted.sort((a, b) => (runDurationMs(b) ?? -1) - (runDurationMs(a) ?? -1));
      break;
  }
  return sorted;
}

function timestampDesc(a: string, b: string): number {
  return Date.parse(b) - Date.parse(a);
}

/** "" · "2 children" · "2 children · 1 active" */
export function childrenLabel(run: RunView): string {
  if (run.childrenCount <= 0) return "";
  const parts = [`${run.childrenCount} ${run.childrenCount === 1 ? "child" : "children"}`];
  if (run.activeChildrenCount > 0) parts.push(`${run.activeChildrenCount} active`);
  return parts.join(" · ");
}

/** True when the run is a child of another run (has a parent_run_id). */
export function isChildRun(run: RunView): boolean {
  return run.parentRunId !== null && run.parentRunId !== "";
}

export interface RunsEmptyState {
  title: string;
  hint: string;
}

export function runsEmptyState(totalCount: number, filteredCount: number): RunsEmptyState | null {
  if (totalCount === 0) {
    return {
      title: "No runs yet",
      hint: "Start an Agent run from the Workbench or the Runtime API.",
    };
  }
  if (filteredCount === 0) {
    return { title: "No runs match", hint: "Adjust or clear the current filters." };
  }
  return null;
}
