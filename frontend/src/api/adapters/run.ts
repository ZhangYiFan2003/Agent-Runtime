import type { RunViewDto, InterruptSummaryDto, ChildRunDto } from "../dto/run";

/**
 * Adapter: RunView DTO (snake_case, open enums) → camelCase view model.
 *
 * Status classification maps the backend's open status set onto a small
 * closed group used for rendering; an unknown status degrades to
 * "unknown" instead of crashing the UI.
 */
export type RunStatusGroup = "active" | "waiting" | "success" | "failure" | "idle" | "unknown";

const STATUS_GROUPS: Record<string, RunStatusGroup> = {
  RUNNING: "active",
  WAITING_APPROVAL: "waiting",
  WAITING_CHILD: "waiting",
  INTERRUPTED: "idle",
  COMPLETED: "success",
  FAILED: "failure",
  CANCELLED: "idle",
};

export const KNOWN_RUN_STATUSES = Object.keys(STATUS_GROUPS);

export function classifyRunStatus(status: string): RunStatusGroup {
  return STATUS_GROUPS[status] ?? "unknown";
}

export const KNOWN_STRATEGIES = ["react", "plan_execute", "multi_agent"] as const;

export interface InterruptSummary {
  runId: string;
  invocationId: string;
  interruptType: string;
  toolName: string | null;
  reason: string;
  createdAt: string;
}

export interface RunError {
  type: string;
  message: string;
  step: string | null;
}

export interface RunView {
  runId: string;
  threadId: string;
  turnId: string | null;
  runKind: string;
  executionStrategy: string;
  status: string;
  statusGroup: RunStatusGroup;
  waitingReason: string | null;
  recoveryAction: string | null;
  parentRunId: string | null;
  parentStepId: string | null;
  createdAt: string;
  updatedAt: string;
  startedAt: string;
  completedAt: string | null;
  output: string | null;
  error: RunError | null;
  interrupt: InterruptSummary | null;
  childrenCount: number;
  activeChildrenCount: number;
  waitingChildrenCount: number;
  terminalChildrenCount: number;
  activeChildRunIds: string[];
  pendingInterrupts: InterruptSummary[];
  allowedOperations: string[];
}

function adaptInterrupt(dto: InterruptSummaryDto): InterruptSummary {
  return {
    runId: dto.run_id,
    invocationId: dto.invocation_id,
    interruptType: dto.interrupt_type,
    toolName: dto.tool_name,
    reason: dto.reason,
    createdAt: dto.created_at,
  };
}

export function adaptRunView(dto: RunViewDto): RunView {
  return {
    runId: dto.run_id,
    threadId: dto.thread_id,
    turnId: dto.turn_id,
    runKind: dto.run_kind,
    executionStrategy: dto.execution_strategy,
    status: dto.status,
    statusGroup: classifyRunStatus(dto.status),
    waitingReason: dto.waiting_reason,
    recoveryAction: dto.recovery_action,
    parentRunId: dto.parent_run_id,
    parentStepId: dto.parent_step_id,
    createdAt: dto.created_at,
    updatedAt: dto.updated_at,
    startedAt: dto.started_at,
    completedAt: dto.completed_at,
    output: dto.output,
    error: dto.error
      ? {
          type: dto.error.type,
          message: dto.error.message,
          step: dto.error.step ?? null,
        }
      : null,
    interrupt: dto.interrupt ? adaptInterrupt(dto.interrupt) : null,
    childrenCount: dto.children_count,
    activeChildrenCount: dto.active_children_count,
    waitingChildrenCount: dto.waiting_children_count,
    terminalChildrenCount: dto.terminal_children_count,
    activeChildRunIds: dto.active_child_run_ids,
    pendingInterrupts: dto.pending_interrupts.map(adaptInterrupt),
    allowedOperations: dto.allowed_operations,
  };
}

export interface ChildRun {
  childRunId: string;
  runKind: string;
  status: string;
  statusGroup: RunStatusGroup;
  parentRunId: string | null;
  parentStepId: string | null;
  assignmentId: string | null;
  workerRole: string | null;
  attempt: number | null;
  createdAt: string;
  updatedAt: string;
}

export function adaptChildRun(dto: ChildRunDto): ChildRun {
  return {
    childRunId: dto.child_run_id,
    runKind: dto.run_kind,
    status: dto.status,
    statusGroup: classifyRunStatus(dto.status),
    parentRunId: dto.parent_run_id,
    parentStepId: dto.parent_step_id,
    assignmentId: dto.assignment_id,
    workerRole: dto.worker_role,
    attempt: dto.attempt,
    createdAt: dto.created_at,
    updatedAt: dto.updated_at,
  };
}
