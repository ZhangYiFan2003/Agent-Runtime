import { describe, expect, it } from "vitest";
import { runViewSchema } from "./run";
import { makeRunViewDto } from "../../test-fixtures/run";

describe("runViewSchema", () => {
  it("parses a valid RunView", () => {
    const dto = runViewSchema.parse(makeRunViewDto());
    expect(dto.run_id).toBe("run_abc123");
    expect(dto.status).toBe("RUNNING");
    expect(dto.allowed_operations).toEqual(["resume", "cancel"]);
  });

  it("stays forward-compatible with unknown new fields", () => {
    const dto = runViewSchema.parse(
      makeRunViewDto({ future_field: { nested: true }, another_one: 42 }),
    );
    expect(dto.future_field).toEqual({ nested: true });
    expect(dto.another_one).toBe(42);
  });

  it("does not fail on unknown status / strategy / run_kind values", () => {
    const dto = runViewSchema.parse(
      makeRunViewDto({
        status: "PAUSED_FOR_MAGIC",
        execution_strategy: "self_healing_swarm",
        run_kind: "subagent_v2",
      }),
    );
    expect(dto.status).toBe("PAUSED_FOR_MAGIC");
    expect(dto.execution_strategy).toBe("self_healing_swarm");
  });
});
