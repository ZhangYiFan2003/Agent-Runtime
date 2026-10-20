import { z } from "zod";
import { ApiError } from "../api/client";
import { mintIdempotencyKey } from "../api/idempotency";
import { submitTurn, type TurnResult } from "../api/threads";
import { getConnection, normalizeRuntimeBaseUrl, type ConnectionConfig } from "../lib/connection";
export { normalizeRuntimeBaseUrl } from "../lib/connection";
import type { StorageLike } from "../lib/thread-registry";

/** One logical message keeps its original payload/key until its outcome is known.
 * No automatic POST retries. Only unresolved submissions are session-persisted.
 */
export type SubmissionPhase = "idle" | "submitting" | "lost" | "failed";

export interface SubmissionSnapshot {
  phase: SubmissionPhase;
  threadId: string | null;
  prompt: string | null;
  errorStatus: number | null;
  errorMessage: string | null;
  responseText: string | null;
  confirmedRunId: string | null;
  confirmedTurnId: string | null;
  confirmedStatus: TurnResult["status"] | null;
  rejectedDraft: string | null;
}

export interface WorkbenchSession {
  snapshot(): SubmissionSnapshot;
  subscribe(listener: () => void): () => void;
  submit(threadId: string, message: string): Promise<void>;
  retryUnconfirmed(): Promise<void>;
  restore(threadId: string | null): void;
  abortAndReset(): void;
  abandonUnconfirmed(): void;
  dismissNotice(): void;
  acknowledgePrompt(): void;
  acknowledgeResponse(): void;
}

export const TURN_ALREADY_RUNNING_MESSAGE = "A turn is already running in this thread.";
export const SUBMISSION_LOST_MESSAGE =
  "Submission outcome unconfirmed. The run may still be executing. Confirm this submission using its original message and key.";

const IDLE: SubmissionSnapshot = {
  phase: "idle", threadId: null, prompt: null,
  errorStatus: null, errorMessage: null, responseText: null,
  confirmedRunId: null, confirmedTurnId: null, confirmedStatus: null,
  rejectedDraft: null,
};

const pendingSchema = z.object({
  threadId: z.string().min(1),
  message: z.string(),
  key: z.string().min(1),
});
type PendingSubmission = z.infer<typeof pendingSchema>;

const PRE_ADMISSION_FAILURE_CODES = new Set([
  "QUEUE_CAPACITY_EXCEEDED",
  "PRINCIPAL_QUEUE_CAPACITY_EXCEEDED",
  "SUBMISSION_INITIALIZATION_FAILED",
]);

function definitelyRejected(error: unknown): error is ApiError {
  return error instanceof ApiError && (
    (error.status >= 400 && error.status < 500) ||
    (error.status === 503 && error.code !== undefined && PRE_ADMISSION_FAILURE_CODES.has(error.code))
  );
}

function sessionStorage(): StorageLike | null {
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

export interface WorkbenchSessionOptions {
  getConfig?: () => ConnectionConfig;
  fetchImpl?: typeof fetch;
  storage?: StorageLike | null;
  mintKey?: () => string;
}

export function createWorkbenchSession(options: WorkbenchSessionOptions = {}): WorkbenchSession {
  const getConfig = options.getConfig ?? getConnection;
  const store = options.storage === undefined ? sessionStorage() : options.storage;
  const mintKey = options.mintKey ?? mintIdempotencyKey;
  const memory = new Map<string, PendingSubmission>();
  let state: SubmissionSnapshot = IDLE;
  let controller: AbortController | null = null;
  let generation = 0;
  let selectedStorageKey: string | null = null;
  const listeners = new Set<() => void>();

  const storageKey = (config: ConnectionConfig, threadId: string) =>
    `axiom.workbench.pending:${encodeURIComponent(normalizeRuntimeBaseUrl(config.baseUrl))}:${encodeURIComponent(threadId)}`;

  const setState = (next: SubmissionSnapshot) => {
    state = next;
    for (const listener of listeners) listener();
  };

  function load(key: string): PendingSubmission | null {
    const cached = memory.get(key);
    if (cached) return cached;
    try {
      const raw = store?.getItem(key);
      if (!raw) return null;
      const parsed = pendingSchema.safeParse(JSON.parse(raw));
      return parsed.success ? parsed.data : null;
    } catch {
      return null;
    }
  }

  function persist(key: string, pending: PendingSubmission | null) {
    if (pending) memory.set(key, pending);
    else memory.delete(key);
    try {
      if (pending) store?.setItem(key, JSON.stringify(pending));
      else store?.removeItem(key);
    } catch {
      // A blocked/full sessionStorage still permits confirmation in this tab.
    }
  }

  function restore(threadId: string | null) {
    generation += 1;
    controller?.abort();
    controller = null;
    selectedStorageKey = threadId === null ? null : storageKey(getConfig(), threadId);
    const pending = selectedStorageKey === null ? null : load(selectedStorageKey);
    setState(pending
      ? { ...IDLE, phase: "lost", threadId: pending.threadId, prompt: pending.message }
      : { ...IDLE, threadId });
  }

  async function attempt(config: ConnectionConfig, pending: PendingSubmission, key: string) {
    controller?.abort();
    const myController = new AbortController();
    controller = myController;
    const myGeneration = ++generation;
    selectedStorageKey = key;
    persist(key, pending);
    setState({ ...IDLE, phase: "submitting", threadId: pending.threadId, prompt: pending.message });
    try {
      const result = await submitTurn(config, pending.threadId, pending.message, {
        idempotencyKey: pending.key,
        signal: myController.signal,
        fetchImpl: options.fetchImpl,
      });
      if (myGeneration !== generation) return;
      persist(key, null);
      setState({
        ...state, phase: "idle", responseText: result.text,
        confirmedRunId: result.runId, confirmedTurnId: result.turnId,
        confirmedStatus: result.status,
      });
    } catch (error) {
      if (myGeneration !== generation) return;
      // Structured capacity/initialization errors are guaranteed pre-admission.
      // Other server failures may follow acceptance, so their key is retained.
      if (definitelyRejected(error)) {
        persist(key, null);
        const details = error.details;
        const activeRunId = error.code === "THREAD_BUSY" && typeof details === "object" && details !== null &&
          "active_run_id" in details && typeof details.active_run_id === "string"
          ? details.active_run_id : null;
        setState({
          ...IDLE, phase: "failed", threadId: pending.threadId,
          rejectedDraft: pending.message, errorStatus: error.status,
          errorMessage: error.code === "THREAD_BUSY" ? TURN_ALREADY_RUNNING_MESSAGE : error.message,
          confirmedRunId: activeRunId,
        });
      } else {
        setState({
          ...state, phase: "lost",
          errorStatus: error instanceof ApiError && error.status > 0 ? error.status : null,
          errorMessage: error instanceof Error ? error.message : "Submission response unavailable",
        });
      }
    }
  }

  return {
    snapshot: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    async submit(threadId, message) {
      if (state.phase === "submitting") return;
      const config = getConfig();
      const key = storageKey(config, threadId);
      if (load(key)) {
        restore(threadId);
        return;
      }
      await attempt(config, { threadId, message, key: mintKey() }, key);
    },
    async retryUnconfirmed() {
      if (state.phase !== "lost" || state.threadId === null) return;
      const config = getConfig();
      const key = storageKey(config, state.threadId);
      if (key !== selectedStorageKey) {
        restore(state.threadId);
        return;
      }
      const pending = load(key);
      if (pending) await attempt(config, pending, key);
      else restore(state.threadId);
    },
    restore,
    abortAndReset: () => restore(null),
    abandonUnconfirmed() {
      if (state.phase !== "lost" || state.threadId === null) return;
      if (selectedStorageKey !== null) persist(selectedStorageKey, null);
      setState({ ...IDLE, threadId: state.threadId });
    },
    dismissNotice() {
      if (state.phase === "failed") {
        setState({ ...state, phase: "idle", errorStatus: null, errorMessage: null });
      }
    },
    acknowledgePrompt() {
      if (state.prompt !== null) setState({ ...state, prompt: null });
    },
    acknowledgeResponse() {
      if (state.responseText !== null) setState({ ...state, responseText: null });
    },
  };
}
