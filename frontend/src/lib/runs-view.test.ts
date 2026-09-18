import { describe, expect, it } from "vitest";
import {
  ALL_FILTER,
  childrenLabel,
  deriveFilterOptions,
  filterRuns,
  isChildRun,
  runDurationMs,
  runsEmptyState,
  sortRuns,
} from "./runs-view";
import { makeRunView } from "../test-fixtures/run";

const RUNS = [
  makeRunView({ run_id: "run_old", status: "COMPLETED", created_at: "2026-09-17T09:00:00+00:00", updated_at: "2026-09-17T09:05:00+00:00", started_at: "2026-09-17T09:00:00+00:00", completed_at: "2026-09-17T09:05:00+00:00" }),
  makeRunView({ run_id: "run_new", status: "RUNNING", created_at: "2026-09-18T01:00:00+00:00", updated_at: "2026-09-18T01:00:12+00:00", started_at: "2026-09-18T01:00:00+00:00", thread_id: "thread_xyz" }),
  makeRunView({ run_id: "run_child1", status: "WAITING_APPROVAL", execution_strategy: "multi_agent", run_kind: "worker", parent_run_id: "run_new", created_at: "2026-09-18T01:01:00+00:00", started_at: "2026-09-18T01:01:00+00:00", updated_at: "2026-09-18T01:01:00+00:00" }),
  makeRunView({ run_id: "run_failed", status: "FAILED", execution_strategy: "plan_execute", children_count: 3, active_children_count: 1, active_child_run_ids: ["run_c1"], created_at: "2026-09-18T00:30:00+00:00", updated_at: "2026-09-18T00:31:00+00:00", started_at: "2026-09-18T00:30:00+00:00", completed_at: "2026-09-18T00:31:00+00:00" }),
];

const NO_FILTER = { search: "", status: ALL_FILTER, strategy: ALL_FILTER };

describe("filterRuns", () => {
  it("returns everything with an empty filter", () => {
    expect(filterRuns(RUNS, NO_FILTER)).toHaveLength(4);
  });

  it("matches search against run id / thread id / turn id", () => {
    expect(filterRuns(RUNS, { ...NO_FILTER, search: "run_new" })).toHaveLength(1);
    expect(filterRuns(RUNS, { ...NO_FILTER, search: "thread_xyz" })[0].runId).toBe("run_new");
    expect(filterRuns(RUNS, { ...NO_FILTER, search: "turn_1" })).toHaveLength(4);
    expect(filterRuns(RUNS, { ...NO_FILTER, search: "nope" })).toHaveLength(0);
  });

  it("matches search against strategy and run kind", () => {
    expect(filterRuns(RUNS, { ...NO_FILTER, search: "multi_agent" })).toHaveLength(1);
    expect(filterRuns(RUNS, { ...NO_FILTER, search: "worker" })).toHaveLength(1);
  });

  it("filters by exact status and strategy", () => {
    expect(filterRuns(RUNS, { ...NO_FILTER, status: "RUNNING" })).toHaveLength(1);
    expect(filterRuns(RUNS, { ...NO_FILTER, status: "COMPLETED" })).toHaveLength(1);
    expect(filterRuns(RUNS, { ...NO_FILTER, strategy: "plan_execute" })).toHaveLength(1);
  });

  it("combines status filter with search", () => {
    expect(
      filterRuns(RUNS, { ...NO_FILTER, status: "FAILED", search: "run" }),
    ).toHaveLength(1);
    expect(
      filterRuns(RUNS, { ...NO_FILTER, status: "FAILED", search: "run_new" }),
    ).toHaveLength(0);
  });
});

describe("sortRuns", () => {
  it("sorts newest first by default", () => {
    const sorted = sortRuns(RUNS, "newest");
    expect(sorted.map((r) => r.runId)).toEqual([
      "run_child1",
      "run_new",
      "run_failed",
      "run_old",
    ]);
  });

  it("sorts oldest first", () => {
    expect(sortRuns(RUNS, "oldest")[0].runId).toBe("run_old");
  });

  it("sorts by status group priority, newest within a group", () => {
    const sorted = sortRuns(RUNS, "status");
    expect(sorted[0].statusGroup).toBe("active");
    expect(sorted.map((r) => r.runId)).toEqual([
      "run_new",
      "run_child1",
      "run_failed",
      "run_old",
    ]);
  });

  it("sorts by duration descending, nulls last", () => {
    const sorted = sortRuns(RUNS, "duration");
    // run_old: 5m, run_failed: 1m, run_new: 12s, run_child1: 0ms (updated==started)
    expect(sorted[0].runId).toBe("run_old");
    expect(sorted[1].runId).toBe("run_failed");
    expect(sorted[2].runId).toBe("run_new");
    expect(sorted[3].runId).toBe("run_child1");
  });
});

describe("runDurationMs", () => {
  it("uses completed_at for terminal runs", () => {
    expect(runDurationMs(RUNS[0])).toBe(5 * 60 * 1000);
  });

  it("uses updated_at for in-flight runs", () => {
    expect(runDurationMs(RUNS[1])).toBe(12_000);
  });

  it("returns null for invalid timestamps", () => {
    const bad = makeRunView({ started_at: "garbage", updated_at: "also-garbage" });
    expect(runDurationMs(bad)).toBeNull();
    const negative = makeRunView({
      started_at: "2026-09-18T01:00:00+00:00",
      updated_at: "2026-09-17T01:00:00+00:00",
    });
    expect(runDurationMs(negative)).toBeNull();
  });
});

describe("deriveFilterOptions", () => {
  it("derives sorted unique options from actual data, including unknown values", () => {
    const withUnknown = [
      ...RUNS,
      makeRunView({ run_id: "run_x", status: "SUSPENDED", execution_strategy: "swarm_v2" }),
    ];
    const { statuses, strategies } = deriveFilterOptions(withUnknown);
    expect(statuses).toContain("SUSPENDED");
    expect(statuses).toContain("RUNNING");
    expect(strategies).toEqual(["multi_agent", "plan_execute", "react", "swarm_v2"].sort());
  });
});

describe("lineage helpers", () => {
  it("detects child runs", () => {
    expect(isChildRun(RUNS[2])).toBe(true);
    expect(isChildRun(RUNS[0])).toBe(false);
  });

  it("formats children labels compactly", () => {
    expect(childrenLabel(RUNS[0])).toBe("");
    expect(childrenLabel(RUNS[3])).toBe("3 children · 1 active");
    const one = makeRunView({ children_count: 1 });
    expect(childrenLabel(one)).toBe("1 child");
  });
});

describe("runsEmptyState", () => {
  it("distinguishes empty runtime from empty filter results", () => {
    expect(runsEmptyState(0, 0)).toEqual({
      title: "No runs yet",
      hint: "Start an Agent run from the Workbench or the Runtime API.",
    });
    expect(runsEmptyState(4, 0)).toEqual({
      title: "No runs match",
      hint: "Adjust or clear the current filters.",
    });
    expect(runsEmptyState(4, 2)).toBeNull();
  });
});
