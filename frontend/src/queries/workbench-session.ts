import { ApiError } from "../api/client";
import { submitTurn } from "../api/threads";
import { getConnection, type ConnectionConfig } from "../lib/connection";

/**
 * Framework-free turn-submission engine behind the Workbench — same pattern
 * as `run-controls-engine`: every blocking-turn semantic lives here so it is
 * unit-testable with an injected fetch, no React required. The hook is a
 * thin `useSyncExternalStore` wrapper.
 *
 * Semantics:
 * - One submission per thread at a time; a second submit while submitting
 *   is ignored.
 * - NEVER auto-retries and never resends: a transport failure (status 0)
 *   surfaces once as the "lost" phase — the run may still be executing, so
 *   event replay keeps running and active-run discovery proceeds normally.
 * - Explicit HTTP errors surface as "failed" with a normalized message
 *   (409 → "A turn is already running in this thread.").
 * - The pending prompt echo is kept until the real `user.message` event
 *   acknowledges it (the hook calls `acknowledgePrompt`); a 200 completed
 *   response's text is kept as a fallback until `assistant.message`
 *   acknowledges it (`acknowledgeResponse`).
 * - `abortAndReset` (thread switch / new thread) aborts the in-flight POST
 *   and swallows its late rejection — no cross-thread leakage.
 */

export type SubmissionPhase = "idle" | "submitting" | "lost" | "failed";

export interface SubmissionSnapshot {
  phase: SubmissionPhase;
  /** Thread the submission belongs to (null when idle with no history). */
  threadId: string | null;
  /** In-flight / unacknowledged user message text (local echo). */
  prompt: string | null;
  errorStatus: number | null;
  errorMessage: string | null;
  /** Assistant text from a 200 completed response, pending event ack. */
  responseText: string | null;
  /** run_id / turn_id confirmed by a 202 waiting/queued response. */
  confirmedRunId: string | null;
  confirmedTurnId: string | null;
}

export interface WorkbenchSession {
  snapshot(): SubmissionSnapshot;
  subscribe(listener: () => void): () => void;
  submit(threadId: string, message: string): Promise<void>;
  /** Thread switch: abort the in-flight POST, reset to a clean idle. */
  abortAndReset(): void;
  /** Dismiss a failed/lost notice (back to idle, keeps nothing stale). */
  dismissNotice(): void;
  acknowledgePrompt(): void;
  acknowledgeResponse(): void;
}

export const TURN_ALREADY_RUNNING_MESSAGE = "A turn is already running in this thread.";
export const SUBMISSION_LOST_MESSAGE =
  "The submission connection was lost. The run may still be executing. Checking Runtime events…";

const IDLE: SubmissionSnapshot = {
  phase: "idle",
  threadId: null,
  prompt: null,
  errorStatus: null,
  errorMessage: null,
  responseText: null,
  confirmedRunId: null,
  confirmedTurnId: null,
};

export interface WorkbenchSessionOptions {
  getConfig?: () => ConnectionConfig;
  fetchImpl?: typeof fetch;
}

export function createWorkbenchSession(options: WorkbenchSessionOptions = {}): WorkbenchSession {
  const getConfig = options.getConfig ?? getConnection;

  let state: SubmissionSnapshot = IDLE;
  let controller: AbortController | null = null;
  /** Monotonic generation: a late resolution from a stale submission is dropped. */
  let generation = 0;
  const listeners = new Set<() => void>();

  const setState = (next: SubmissionSnapshot) => {
    state = next;
    for (const listener of listeners) listener();
  };

  return {
    snapshot: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },

    async submit(threadId, message) {
      if (state.phase === "submitting") return;
      controller?.abort();
      const myController = new AbortController();
      controller = myController;
      generation += 1;
      const myGeneration = generation;

      setState({
        ...IDLE,
        phase: "submitting",
        threadId,
        prompt: message,
      });

      try {
        const result = await submitTurn(getConfig(), threadId, message, {
          signal: myController.signal,
          fetchImpl: options.fetchImpl,
        });
        if (myGeneration !== generation) return; // stale: thread switched meanwhile
        setState({
          ...state,
          phase: "idle",
          responseText: result.text,
          confirmedRunId: result.runId,
          confirmedTurnId: result.turnId,
        });
      } catch (error) {
        if (myGeneration !== generation) return; // aborted by abortAndReset
        if (error instanceof ApiError && error.status === 0) {
          // Transport failure: never resend — the run may still be executing.
          setState({ ...state, phase: "lost", errorStatus: null, errorMessage: null });
          return;
        }
        if (error instanceof ApiError) {
          setState({
            ...state,
            phase: "failed",
            prompt: null, // rejected before the turn started — no echo pending
            errorStatus: error.status,
            errorMessage: error.status === 409 ? TURN_ALREADY_RUNNING_MESSAGE : error.message,
          });
          return;
        }
        setState({
          ...state,
          phase: "failed",
          prompt: null,
          errorStatus: null,
          errorMessage: error instanceof Error ? error.message : "Submission failed",
        });
      }
    },

    abortAndReset() {
      generation += 1;
      controller?.abort();
      controller = null;
      setState(IDLE);
    },

    dismissNotice() {
      if (state.phase === "failed") {
        setState({ ...state, phase: "idle", errorStatus: null, errorMessage: null });
      } else if (state.phase === "lost") {
        // Keep confirmed ids; drop only the notice.
        setState({ ...state, phase: "idle" });
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
