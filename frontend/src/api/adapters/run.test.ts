import { describe, expect, it } from "vitest";
import { adaptRunView, classifyRunStatus } from "./run";
import { runViewSchema } from "../dto/run";
import { makeRunViewDto } from "../../test-fixtures/run";

describe("adaptRunView", () => {
  it("maps snake_case DTO to camelCase view model", () => {
    const view = adaptRunView(
      runViewSchema.parse(
        makeRunViewDto({
          parent_run_id: "run_parent",
          pending_interrupts: [
            {
              run_id: "run_child",
              invocation_id: "inv_1",
              interrupt_type: "approval",
              tool_name: "shell",
              reason: "confirm command",
              created_at: "2026-09-17T10:00:02+00:00",
            },
          ],
        }),
      ),
    );
    expect(view.runId).toBe("run_abc123");
    expect(view.threadId).toBe("thread_1");
    expect(view.executionStrategy).toBe("react");
    expect(view.parentRunId).toBe("run_parent");
    expect(view.statusGroup).toBe("active");
    expect(view.childrenCount).toBe(0);
    expect(view.pendingInterrupts).toHaveLength(1);
    expect(view.pendingInterrupts[0].invocationId).toBe("inv_1");
    expect(view.pendingInterrupts[0].toolName).toBe("shell");
  });

  it("maps run errors without dropping the message", () => {
    const view = adaptRunView(
      runViewSchema.parse(
        makeRunViewDto({
          status: "FAILED",
          error: { type: "ToolError", message: "tool exploded", step: "step_3", metadata: {} },
        }),
      ),
    );
    expect(view.statusGroup).toBe("failure");
    expect(view.error).toEqual({ type: "ToolError", message: "tool exploded", step: "step_3" });
  });
});

describe("classifyRunStatus", () => {
  it("classifies all known statuses", () => {
    expect(classifyRunStatus("RUNNING")).toBe("active");
    expect(classifyRunStatus("WAITING_APPROVAL")).toBe("waiting");
    expect(classifyRunStatus("WAITING_CHILD")).toBe("waiting");
    expect(classifyRunStatus("INTERRUPTED")).toBe("idle");
    expect(classifyRunStatus("COMPLETED")).toBe("success");
    expect(classifyRunStatus("FAILED")).toBe("failure");
    expect(classifyRunStatus("CANCELLED")).toBe("idle");
  });

  it("degrades unknown statuses to 'unknown' instead of crashing", () => {
    expect(classifyRunStatus("PAUSED_FOR_MAGIC")).toBe("unknown");
    expect(classifyRunStatus("")).toBe("unknown");
  });
});
