import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ApiError } from "../api/client";
import { createThread } from "../api/threads";
import { getConnectionSnapshot, useConnection } from "../lib/connection";
import {
  composeDurableWorkbenchItems,
  composerState,
  messageAcknowledged,
  type ComposerState,
  type WorkbenchItem,
} from "../lib/workbench-projection";
import {
  loadLastThreadId,
  saveLastThreadId,
  getThreadRegistry,
  type ThreadRegistryEntry,
} from "../lib/thread-registry";
import type { RuntimeEvent } from "../api/adapters/event";
import type { RunView } from "../api/adapters/run";
import { useHealth } from "./use-health";
import { useRun } from "./use-run-detail";
import { useRuntimeEvents, type RuntimeEventsState } from "./use-runtime-events";
import { createWorkbenchSession, type SubmissionSnapshot } from "./workbench-session";
import { useThreadMessages } from "./use-thread-messages";
import { isTerminalRunStatus } from "../lib/run-detail-view";

export type SendOutcome = "ok" | "failed" | "lost";

export interface Workbench {
  threads: ThreadRegistryEntry[];
  threadId: string | null;
  events: RuntimeEvent[];
  replayStatus: RuntimeEventsState["status"];
  replayError: string | null;
  items: WorkbenchItem[];
  /** Run shown in the context panel (active, else most recent). */
  run: RunView | null;
  activeRunId: string | null;
  runQueryPending: boolean;
  submission: SubmissionSnapshot;
  composer: ComposerState;
  connected: boolean;
  createError: string | null;
  creatingThread: boolean;
  send: (text: string) => Promise<SendOutcome>;
  selectThread: (threadId: string | null) => void;
  createAndSelectThread: () => Promise<void>;
  removeThread: (threadId: string) => void;
  dismissSubmissionNotice: () => void;
  retrySubmission: () => Promise<void>;
  abandonSubmission: () => void;
  missingRunId: string | null;
  retryRunLookup: () => void;
  history: ReturnType<typeof useThreadMessages>;
}

/**
 * Workbench orchestration: selected thread (local registry + last-opened
 * persistence), durable message pages, bounded debug replay, and the
 * blocking-turn submission session. Only unconfirmed submission identities
 * are session-persisted; Run data and conversation come from the Runtime.
 */
export function useWorkbench(): Workbench {
  const connection = useConnection();
  const registry = useMemo(() => getThreadRegistry(connection.scope), [connection.scope]);
  const identity = `${connection.scope}:${connection.generation}`;
  const [selection, setSelection] = useState(() => ({ identity, threads: registry.list(), threadId: loadLastThreadId(connection.scope) }));
  const { threads, threadId } = selection.identity === identity ? selection :
    { threads: registry.list(), threadId: loadLastThreadId(connection.scope) };
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const createController = useRef<AbortController | null>(null);
  useEffect(() => () => createController.current?.abort(), [identity]);

  const session = useMemo(() => createWorkbenchSession({ getConfig: () => connection.config }), [connection.scope, connection.generation]);
  const submission = useSyncExternalStore(
    useCallback((listener) => session.subscribe(listener), [session]),
    () => session.snapshot(),
  );
  useEffect(() => {
    session.restore(loadLastThreadId(connection.scope));
    return () => session.abortAndReset();
  }, [session]);

  const health = useHealth();
  const connected = !health.isError;

  // Thread-wide replay (no run_id filter). runStatus "" keeps the poll loop
  // running at the default cadence across the thread's whole lifetime — a
  // workbench thread outlives any single run, so the terminal drain used by
  // Run Detail must not stop it.
  const history = useThreadMessages(threadId);
  const eventsState = useRuntimeEvents({ threadId, runId: "", runStatus: "", latest: true });
  const events = eventsState.events;

  // The message endpoint returns the current root Run independently of debug
  // retention. A confirmed POST can bridge the next message poll's latency.
  const confirmedInHistory = submission.confirmedRunId !== null && history.messages.some((message) => message.run_id === submission.confirmedRunId);
  const activeRunId = history.currentRun !== null && (!isTerminalRunStatus(history.currentRun.status) || confirmedInHistory)
    ? history.currentRun.run_id : submission.confirmedRunId ?? history.currentRun?.run_id ?? null;

  const runQuery = useRun(activeRunId ?? "");
  const run = runQuery.data ?? null;
  // The existing query retries once. Two missing responses establish a
  // diagnosable inconsistency; they do not establish a completed Run.
  const missingRunId = runQuery.isError && runQuery.error instanceof ApiError &&
    runQuery.error.status === 404 && runQuery.failureCount >= 2 ? activeRunId : null;

  // The run query lags behind a newly discovered id; while a discovered
  // active run's view has not loaded, conservatively treat it as busy
  // (the runtime rejects concurrent turns with 409 anyway).
  const runForActive = run !== null && run.runId === activeRunId ? run : null;
  const activeStatus = runForActive !== null ? runForActive.status :
    submission.confirmedRunId === activeRunId && submission.confirmedStatus !== null
      ? submission.confirmedStatus : history.currentRun?.run_id === activeRunId
        ? history.currentRun.status : activeRunId !== null ? "RUNNING" : null;

  const composer = composerState({
    connected,
    submitting: submission.phase === "submitting",
    activeRunStatus: activeStatus,
    unconfirmed: submission.phase === "lost",
    runLookupMissing: missingRunId !== null,
  });

  // Once durable messages cover the local echoes,
  // drop them from the session so they cannot linger.
  useEffect(() => {
    if (
      submission.prompt !== null &&
      submission.threadId === threadId &&
      messageAcknowledged(history.messages, "user", { threadId, runId: submission.confirmedRunId, turnId: submission.confirmedTurnId, prompt: null, responseText: null })
    ) {
      session.acknowledgePrompt();
    }
  }, [history.messages, submission.prompt, submission.threadId, submission.confirmedRunId, submission.confirmedTurnId, threadId, session]);

  useEffect(() => {
    if (
      submission.responseText !== null &&
      messageAcknowledged(history.messages, "assistant", { threadId, runId: submission.confirmedRunId, turnId: submission.confirmedTurnId, prompt: null, responseText: null })
    ) {
      session.acknowledgeResponse();
    }
  }, [history.messages, submission.responseText, submission.confirmedRunId, submission.confirmedTurnId, threadId, session]);

  const selectThread = useCallback(
    (next: string | null) => {
      createController.current?.abort();
      session.restore(next);
      saveLastThreadId(connection.scope, next);
      setSelection({ identity, threadId: next, threads: next !== null ? registry.add(next) : registry.list() });
    },
    [session, connection.scope, identity, registry],
  );

  const createAndSelectThread = useCallback(async () => {
    if (creating) return;
    setCreating(true);
    setCreateError(null);
    const controller = new AbortController();
    createController.current = controller;
    try {
      const ref = await createThread(connection.config, { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (getConnectionSnapshot() !== connection) return;
      selectThread(ref.threadId);
    } catch (error) {
      if (!controller.signal.aborted && getConnectionSnapshot() === connection) setCreateError(error instanceof ApiError ? error.message : "Failed to create thread");
    } finally {
      if (getConnectionSnapshot() === connection) setCreating(false);
    }
  }, [creating, selectThread, connection]);

  const removeThread = useCallback((id: string) => {
    setSelection({ identity, threadId, threads: registry.remove(id) });
  }, [registry, identity, threadId]);

  const send = useCallback(
    async (text: string): Promise<SendOutcome> => {
      let id = threadId;
      if (id === null) {
        // Auto-create a thread for the first message from the empty state.
        setCreating(true);
        setCreateError(null);
        const controller = new AbortController();
        createController.current = controller;
        try {
          const ref = await createThread(connection.config, { signal: controller.signal });
          if (controller.signal.aborted) return "failed";
          if (getConnectionSnapshot() !== connection) return "failed";
          id = ref.threadId;
          selectThread(ref.threadId);
        } catch (error) {
          if (!controller.signal.aborted && getConnectionSnapshot() === connection) setCreateError(error instanceof ApiError ? error.message : "Failed to create thread");
          return "failed";
        } finally {
          if (getConnectionSnapshot() === connection) setCreating(false);
        }
      }
      await session.submit(id, text);
      const phase = session.snapshot().phase;
      if (phase === "failed") return "failed";
      if (phase === "lost") return "lost";
      return "ok";
    },
    [threadId, session, selectThread, connection],
  );

  const items = useMemo(
    () =>
      composeDurableWorkbenchItems(history.messages, events, {
        threadId,
        prompt: submission.threadId === threadId ? submission.prompt : null,
        responseText: submission.threadId === threadId ? submission.responseText : null,
        runId: submission.confirmedRunId,
        turnId: submission.confirmedTurnId,
      }, eventsState.preview),
    [history.messages, events, eventsState.preview, submission.prompt, submission.threadId, submission.responseText, submission.confirmedRunId, submission.confirmedTurnId, threadId],
  );

  return {
    threads,
    threadId,
    events,
    replayStatus: eventsState.status,
    replayError: eventsState.error,
    items,
    run: runForActive,
    activeRunId,
    runQueryPending: runQuery.isPending,
    submission,
    composer,
    connected,
    createError,
    creatingThread: creating,
    send,
    selectThread,
    createAndSelectThread,
    removeThread,
    dismissSubmissionNotice: session.dismissNotice,
    retrySubmission: session.retryUnconfirmed,
    abandonSubmission: session.abandonUnconfirmed,
    missingRunId,
    retryRunLookup: () => void runQuery.refetch(),
    history,
  };
}
