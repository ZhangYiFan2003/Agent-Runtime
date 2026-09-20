import type { QueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import {
  cancelRun,
  interruptRun,
  requeueRun,
  resumeRun,
  type ApprovalDecision,
} from "../api/run-controls";
import { executeControlMutation, mintIdempotencyKey } from "../api/idempotency";
import { controlErrorMessage } from "../lib/control-errors";
import type { InterruptSummary } from "../api/adapters/run";
import { getConnection } from "../lib/connection";
import { queryKeys } from "./keys";

/**
 * Framework-free mutation engine behind `useRunControls` — every Phase-5
 * semantic lives here so it can be unit-tested with a real QueryClient and a
 * mocked fetch, no React required. The hook is a thin useSyncExternalStore
 * wrapper over `snapshot()` / `subscribe()`.
 *
 * Semantics (FRONTEND_PLAN J):
 * - One user intention = one Idempotency-Key, minted per click and reused
 *   across network-level retries (transport failures only, max 2 attempts).
 * - No optimistic status mutation: local effects are pending state + an
 *   inline feedback line; truth comes back via query invalidation/refetch.
 * - 409 state-conflict codes surface a concise message AND silently refetch
 *   the run family; `idempotency_key_conflict` surfaces as a client bug.
 * - Pending state is per engine instance (= per run detail page); there is
 *   no app-global lock.
 */

export interface RunControlsFeedback {
  tone: "success" | "error";
  message: string;
}

export interface RunControlsPending {
  resume: boolean;
  cancel: boolean;
  interrupt: boolean;
  requeue: boolean;
  /** Per approval card, keyed by `${targetRunId}:${invocationId}`. */
  approvals: ReadonlyMap<string, boolean>;
}

export interface RunControlsSnapshot {
  pending: RunControlsPending;
  /** True while ANY control mutation for this run is in flight — disables every control button. */
  anyPending: boolean;
  feedback: RunControlsFeedback | null;
}

export interface RunControlsEngine {
  snapshot(): RunControlsSnapshot;
  subscribe(listener: () => void): () => void;
  dismissFeedback(): void;
  resume(): Promise<boolean>;
  cancel(): Promise<boolean>;
  interrupt(reason?: string): Promise<boolean>;
  requeue(): Promise<boolean>;
  resolveApproval(interrupt: InterruptSummary, decision: ApprovalDecision): Promise<boolean>;
}

export const CONTROL_SUCCESS_MESSAGES: Record<string, string> = {
  resume: "Run resumed",
  cancel: "Cancel requested",
  interrupt: "Interrupt requested",
  requeue: "Run requeued",
  approve: "Approval submitted",
  reject: "Rejection submitted",
};

/**
 * Invalidate exactly the run family — never a global `invalidateQueries()`.
 * `parentRunId` covers the case where a child run was targeted (child
 * approval from the parent page, or an action on a child run) so parent
 * aggregates (pendingInterrupts, child counts) refresh too.
 */
export async function invalidateRunFamily(
  queryClient: QueryClient,
  runId: string,
  options: { parentRunId?: string | null } = {},
): Promise<void> {
  const keys: Array<readonly unknown[]> = [
    queryKeys.run(runId),
    queryKeys.children(runId),
    queryKeys.trace(runId),
    queryKeys.metrics(runId),
  ];
  const parentRunId = options.parentRunId;
  if (typeof parentRunId === "string" && parentRunId !== "" && parentRunId !== runId) {
    keys.push(queryKeys.run(parentRunId), queryKeys.children(parentRunId));
  }
  await Promise.all(keys.map((key) => queryClient.invalidateQueries({ queryKey: key })));
}

export interface RunControlsEngineDeps {
  /** Injectable fetch for tests. */
  fetchImpl?: typeof fetch;
  /** Injectable retry-delay sleeper for tests. */
  sleep?: (ms: number) => Promise<void>;
  /** Injectable key minter for tests. */
  mintKey?: () => string;
}

export interface RunControlsEngineOptions {
  runId: string;
  /** This run's parent, when it is a child run — its aggregates also refresh. */
  parentRunId?: string | null;
  queryClient: QueryClient;
  deps?: RunControlsEngineDeps;
}

type MutationKind = "resume" | "cancel" | "interrupt" | "requeue" | `approval:${string}`;

function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error;
  return new ApiError({
    status: 0,
    message: error instanceof Error ? error.message : "Unexpected error",
  });
}

function emptyPending(): RunControlsPending {
  return { resume: false, cancel: false, interrupt: false, requeue: false, approvals: new Map() };
}

export function createRunControlsEngine(options: RunControlsEngineOptions): RunControlsEngine {
  const { runId, queryClient } = options;
  const parentRunId = options.parentRunId ?? null;
  const deps = options.deps ?? {};
  const fetchImpl = deps.fetchImpl;
  const sleep = deps.sleep;
  const mintKey = deps.mintKey ?? mintIdempotencyKey;

  let pending = emptyPending();
  let feedback: RunControlsFeedback | null = null;
  const listeners = new Set<() => void>();
  // Cached snapshot so useSyncExternalStore's getSnapshot is referentially
  // stable between state changes (a fresh object per call re-renders forever).
  let snapshotCache: RunControlsSnapshot | null = null;
  // Double-click proof that survives the gap between click and re-render.
  const inFlight = new Set<MutationKind>();

  function buildSnapshot(): RunControlsSnapshot {
    return {
      pending,
      anyPending:
        pending.resume ||
        pending.cancel ||
        pending.interrupt ||
        pending.requeue ||
        pending.approvals.size > 0,
      feedback,
    };
  }

  function emit() {
    snapshotCache = buildSnapshot();
    for (const listener of listeners) listener();
  }

  function setPending(kind: MutationKind, value: boolean) {
    if (kind.startsWith("approval:")) {
      const key = kind.slice("approval:".length);
      const approvals = new Map(pending.approvals);
      if (value) approvals.set(key, true);
      else approvals.delete(key);
      pending = { ...pending, approvals };
    } else {
      pending = { ...pending, [kind]: value } as RunControlsPending;
    }
  }

  function snapshot(): RunControlsSnapshot {
    if (snapshotCache === null) snapshotCache = buildSnapshot();
    return snapshotCache;
  }

  async function perform(
    kind: MutationKind,
    successMessage: string,
    target: (idempotencyKey: string) => Promise<unknown>,
    family?: { runId: string; parentRunId?: string | null },
  ): Promise<boolean> {
    if (runId === "" || inFlight.has(kind)) return false;
    const familyRunId = family?.runId ?? runId;
    const familyParent = family?.parentRunId ?? parentRunId;
    inFlight.add(kind);
    setPending(kind, true);
    feedback = null;
    emit();
    // One user intention = one key, minted here and reused across
    // transport-failure retries by executeControlMutation.
    const idempotencyKey = mintKey();
    try {
      await executeControlMutation(() => target(idempotencyKey), { sleep });
      await invalidateRunFamily(queryClient, familyRunId, { parentRunId: familyParent });
      feedback = { tone: "success", message: successMessage };
      return true;
    } catch (error) {
      const mapped = controlErrorMessage(toApiError(error));
      if (mapped.kind === "conflict") {
        // State changed under us: note the conflict and silently refetch.
        void invalidateRunFamily(queryClient, familyRunId, { parentRunId: familyParent });
      }
      feedback = { tone: "error", message: mapped.message };
      return false;
    } finally {
      inFlight.delete(kind);
      setPending(kind, false);
      emit();
    }
  }

  const controls: RunControlsEngine = {
    snapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dismissFeedback() {
      if (feedback === null) return;
      feedback = null;
      emit();
    },
    resume: () =>
      perform("resume", CONTROL_SUCCESS_MESSAGES.resume, (key) =>
        resumeRun(getConnection(), runId, {}, { idempotencyKey: key, fetchImpl }),
      ),
    cancel: () =>
      perform("cancel", CONTROL_SUCCESS_MESSAGES.cancel, (key) =>
        cancelRun(getConnection(), runId, { idempotencyKey: key, fetchImpl }),
      ),
    interrupt: (reason?: string) =>
      perform("interrupt", CONTROL_SUCCESS_MESSAGES.interrupt, (key) =>
        interruptRun(getConnection(), runId, { reason }, { idempotencyKey: key, fetchImpl }),
      ),
    requeue: () =>
      perform("requeue", CONTROL_SUCCESS_MESSAGES.requeue, (key) =>
        requeueRun(getConnection(), runId, { idempotencyKey: key, fetchImpl }),
      ),
    resolveApproval(interrupt: InterruptSummary, decision: ApprovalDecision) {
      const key = `${interrupt.runId}:${interrupt.invocationId}` as const;
      // Approvals POST to the interrupt's target run — a child approval
      // targets the child run, never the parent by proxy. The page run's
      // aggregates refresh through the family invalidation (it is the
      // parent when the interrupt targets a child).
      const family = {
        runId: interrupt.runId,
        parentRunId: interrupt.runId === runId ? parentRunId : runId,
      };
      return perform(
        `approval:${key}`,
        CONTROL_SUCCESS_MESSAGES[decision],
        (idempotencyKey) =>
          resumeRun(
            getConnection(),
            interrupt.runId,
            { decision, invocationId: interrupt.invocationId },
            { idempotencyKey, fetchImpl },
          ),
        family,
      );
    },
  };
  return controls;
}
