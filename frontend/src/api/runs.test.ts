import { describe, expect, it } from "vitest";
import { fetchRuns } from "./runs";
import { ApiError } from "./client";
import { makeRunViewDto } from "../test-fixtures/run";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const BASE = { baseUrl: "", apiKey: "key" };

describe("fetchRuns", () => {
  it("parses and adapts the /v1/runs response", async () => {
    const fetchImpl: typeof fetch = async (input) => {
      expect(String(input)).toContain("/v1/runs");
      return jsonResponse(200, {
        runs: [
          makeRunViewDto({ run_id: "run_a", status: "RUNNING" }),
          makeRunViewDto({
            run_id: "run_b",
            status: "COMPLETED",
            execution_strategy: "plan_execute",
            parent_run_id: "run_a",
            children_count: 2,
            active_children_count: 1,
            active_child_run_ids: ["run_b1"],
          }),
        ],
      });
    };
    const runs = await fetchRuns(BASE, fetchImpl);
    expect(runs).toHaveLength(2);
    expect(runs[0].runId).toBe("run_a");
    expect(runs[0].statusGroup).toBe("active");
    expect(runs[1].parentRunId).toBe("run_a");
    expect(runs[1].childrenCount).toBe(2);
    expect(runs[1].activeChildRunIds).toEqual(["run_b1"]);
  });

  it("tolerates unknown fields and unknown enums in the list", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(200, {
        runs: [
          makeRunViewDto({
            run_id: "run_future",
            status: "SUSPENDED_BY_OPERATOR",
            execution_strategy: "quantum_swarm",
            future_list_field: [1, 2, 3],
          }),
        ],
      });
    const runs = await fetchRuns(BASE, fetchImpl);
    expect(runs).toHaveLength(1);
    expect(runs[0].status).toBe("SUSPENDED_BY_OPERATOR");
    expect(runs[0].statusGroup).toBe("unknown");
  });

  it("throws ApiError on HTTP failure (plain error shape)", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(401, { error: "unauthorized" });
    const error: unknown = await fetchRuns(BASE, fetchImpl).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    if (error instanceof ApiError) {
      expect(error.status).toBe(401);
      expect(error.message).toBe("unauthorized");
    }
  });
});
