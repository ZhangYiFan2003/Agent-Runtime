import { describe, expect, it } from "vitest";
import { fetchRun, fetchRunChildren, fetchRunMetrics, fetchRunTrace } from "./run";
import { ApiError } from "./client";
import { makeChildRunDto, makeRunViewDto } from "../test-fixtures/run";
import { makeSpanDto, makeTraceDto } from "../test-fixtures/trace";
import { makeRunMetricsDto } from "../test-fixtures/metrics";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const BASE = { baseUrl: "", apiKey: "key" };

describe("fetchRun", () => {
  it("parses and adapts GET /v1/runs/{id}", async () => {
    const fetchImpl: typeof fetch = async (input) => {
      expect(String(input)).toContain("/v1/runs/run_a");
      return jsonResponse(200, makeRunViewDto({ run_id: "run_a", status: "WAITING_APPROVAL" }));
    };
    const run = await fetchRun(BASE, "run_a", fetchImpl);
    expect(run.runId).toBe("run_a");
    expect(run.statusGroup).toBe("waiting");
  });

  it("throws ApiError 404 for an unknown run", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(404, { error: { code: "run_not_found", message: "run not found" } });
    const error: unknown = await fetchRun(BASE, "run_nope", fetchImpl).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    if (error instanceof ApiError) {
      expect(error.status).toBe(404);
      expect(error.code).toBe("run_not_found");
    }
  });
});

describe("fetchRunChildren", () => {
  it("parses and adapts the children projection including interrupts", async () => {
    const fetchImpl: typeof fetch = async (input) => {
      expect(String(input)).toContain("/v1/runs/run_a/children");
      return jsonResponse(200, {
        parent_run_id: "run_a",
        children: [
          makeChildRunDto({ child_run_id: "run_c1", worker_role: "researcher" }),
          makeChildRunDto({
            child_run_id: "run_c2",
            status: "WAITING_APPROVAL",
            attempt: 2,
            interrupt: {
              run_id: "run_c2",
              invocation_id: "run_c2:call_1",
              interrupt_type: "tool_approval",
              tool_name: "shell",
              reason: "policy requires approval",
              created_at: "2026-09-17T10:00:03+00:00",
            },
          }),
        ],
      });
    };
    const result = await fetchRunChildren(BASE, "run_a", fetchImpl);
    expect(result.parentRunId).toBe("run_a");
    expect(result.children).toHaveLength(2);
    expect(result.children[0].workerRole).toBe("researcher");
    expect(result.children[1].attempt).toBe(2);
    expect(result.children[1].interrupt?.toolName).toBe("shell");
    expect(result.children[1].statusGroup).toBe("waiting");
  });
});

describe("fetchRunTrace", () => {
  it("parses and adapts trace + spans", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(200, {
        trace: makeTraceDto(),
        spans: [
          makeSpanDto({ span_id: "span_root", name: "agent.run" }),
          makeSpanDto({
            span_id: "span_llm",
            span_type: "llm",
            name: "llm.chat",
            parent_span_id: "span_root",
            attributes: { provider: "deepseek", model: "deepseek-chat", ttft_ms: 320 },
          }),
        ],
      });
    const result = await fetchRunTrace(BASE, "run_a", fetchImpl);
    expect(result).not.toBeNull();
    expect(result?.trace.traceId).toBe("trace_1");
    expect(result?.spans).toHaveLength(2);
    expect(result?.spans[1].parentSpanId).toBe("span_root");
    expect(result?.spans[1].spanKind).toBe("llm");
    expect(result?.spans[1].attributes.provider).toBe("deepseek");
  });

  it("classifies unknown span types as generic, never failing", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(200, {
        trace: makeTraceDto(),
        spans: [makeSpanDto({ span_type: "quantum_entanglement" })],
      });
    const result = await fetchRunTrace(BASE, "run_a", fetchImpl);
    expect(result?.spans[0].spanType).toBe("quantum_entanglement");
    expect(result?.spans[0].spanKind).toBe("other");
  });

  it("returns null on 404 (no trace yet) instead of throwing", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(404, { error: { code: "trace_not_found", message: "no trace" } });
    await expect(fetchRunTrace(BASE, "run_a", fetchImpl)).resolves.toBeNull();
  });

  it("still throws on non-404 failures", async () => {
    const fetchImpl: typeof fetch = async () => jsonResponse(500, { error: "boom" });
    const error: unknown = await fetchRunTrace(BASE, "run_a", fetchImpl).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    if (error instanceof ApiError) expect(error.status).toBe(500);
  });
});

describe("fetchRunMetrics", () => {
  it("parses and adapts RunMetrics", async () => {
    const fetchImpl: typeof fetch = async (input) => {
      expect(String(input)).toContain("/v1/runs/run_a/metrics");
      return jsonResponse(200, makeRunMetricsDto({ total_tokens: 8180, tool_success_rate: 0.924 }));
    };
    const metrics = await fetchRunMetrics(BASE, "run_a", fetchImpl);
    expect(metrics).not.toBeNull();
    expect(metrics?.totalTokens).toBe(8180);
    expect(metrics?.toolSuccessRate).toBe(0.924);
    expect(metrics?.budgetUtilization).toEqual({});
  });

  it("tolerates unknown extra fields from a newer backend", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(200, makeRunMetricsDto({ future_field: { nested: true } }));
    const metrics = await fetchRunMetrics(BASE, "run_a", fetchImpl);
    expect(metrics?.runId).toBe("run_abc123");
  });

  it("returns null on 404 (no trace yet) instead of throwing", async () => {
    const fetchImpl: typeof fetch = async () =>
      jsonResponse(404, { error: { code: "trace_not_found", message: "no trace" } });
    await expect(fetchRunMetrics(BASE, "run_a", fetchImpl)).resolves.toBeNull();
  });
});
