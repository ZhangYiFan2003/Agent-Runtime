import { runViewSchema } from "../api/dto/run";
import { adaptRunView, type RunView } from "../api/adapters/run";

/**
 * Minimal valid RunView fixture — mirrors `run_view()` in
 * src/axiom/runtime/control_plane.py. Tests add / mutate fields on top.
 */
export function makeRunViewDto(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run_abc123",
    thread_id: "thread_1",
    turn_id: "turn_1",
    run_kind: "agent",
    execution_strategy: "react",
    status: "RUNNING",
    waiting_reason: null,
    recovery_action: null,
    parent_run_id: null,
    parent_step_id: null,
    created_at: "2026-09-17T10:00:00+00:00",
    updated_at: "2026-09-17T10:00:05+00:00",
    started_at: "2026-09-17T10:00:00+00:00",
    completed_at: null,
    output: null,
    error: null,
    interrupt: null,
    children_count: 0,
    active_children_count: 0,
    waiting_children_count: 0,
    terminal_children_count: 0,
    active_child_run_ids: [],
    pending_interrupts: [],
    assignment: null,
    allowed_operations: ["resume", "cancel"],
    ...overrides,
  };
}

/** Adapted view-model fixture for view-layer tests (filter/sort/format). */
export function makeRunView(overrides: Record<string, unknown> = {}): RunView {
  return adaptRunView(runViewSchema.parse(makeRunViewDto(overrides)));
}
