import { useCallback, useMemo, useSyncExternalStore } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { ApprovalDecision } from "../api/run-controls";
import type { InterruptSummary } from "../api/adapters/run";
import {
  createRunControlsEngine,
  type RunControlsFeedback,
  type RunControlsPending,
  type RunControlsSnapshot,
} from "./run-controls-engine";

/**
 * Run control mutations for the Run Detail page (Phase 5). Thin React
 * wrapper over `createRunControlsEngine` — all semantics (idempotency-key
 * reuse, transport-only retry, 409 mapping, scoped invalidation, no
 * optimistic status) live in the engine and are unit-tested without React.
 */

export type { RunControlsFeedback, RunControlsPending };
export { invalidateRunFamily } from "./run-controls-engine";

export interface RunControls extends RunControlsSnapshot {
  dismissFeedback: () => void;
  resume: () => Promise<boolean>;
  cancel: () => Promise<boolean>;
  interrupt: (reason?: string) => Promise<boolean>;
  requeue: () => Promise<boolean>;
  resolveApproval: (interrupt: InterruptSummary, decision: ApprovalDecision) => Promise<boolean>;
}

export interface UseRunControlsOptions {
  /** This run's parent, when it is a child run — its aggregates also refresh. */
  parentRunId?: string | null;
}

export function useRunControls(runId: string, options: UseRunControlsOptions = {}): RunControls {
  const queryClient = useQueryClient();
  const parentRunId = options.parentRunId ?? null;
  // One engine per (runId, parentRunId) — pending state is scoped to the page.
  const engine = useMemo(
    () => createRunControlsEngine({ runId, parentRunId, queryClient }),
    [runId, parentRunId, queryClient],
  );
  const snapshot = useSyncExternalStore(
    useCallback((listener) => engine.subscribe(listener), [engine]),
    () => engine.snapshot(),
  );
  return {
    ...snapshot,
    dismissFeedback: engine.dismissFeedback,
    resume: engine.resume,
    cancel: engine.cancel,
    interrupt: engine.interrupt,
    requeue: engine.requeue,
    resolveApproval: engine.resolveApproval,
  };
}
