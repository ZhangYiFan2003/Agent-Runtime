import type { RunSummaryDto } from "../dto/run-summary";
import { classifyRunStatus, type RunStatusGroup } from "./run";

export interface RunSummary {
  runId: string;
  threadId: string;
  turnId: string | null;
  runKind: string;
  executionStrategy: string;
  status: string;
  statusGroup: RunStatusGroup;
  parentRunId: string | null;
  createdAt: string;
  updatedAt: string;
  startedAt: string;
  completedAt: string | null;
  childrenCount: number;
  activeChildrenCount: number;
}

export function adaptRunSummary(dto: RunSummaryDto): RunSummary {
  return {
    runId: dto.run_id, threadId: dto.thread_id, turnId: dto.turn_id,
    runKind: dto.run_kind, executionStrategy: dto.execution_strategy,
    status: dto.status, statusGroup: classifyRunStatus(dto.status),
    parentRunId: dto.parent_run_id, createdAt: dto.created_at, updatedAt: dto.updated_at,
    startedAt: dto.started_at, completedAt: dto.completed_at,
    childrenCount: dto.children_count, activeChildrenCount: dto.active_children_count,
  };
}
