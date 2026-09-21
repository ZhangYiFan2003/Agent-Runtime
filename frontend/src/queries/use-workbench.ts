import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { ApiError } from "../api/client";
import { createThread } from "../api/threads";
import { getConnection } from "../lib/connection";
import {
  composeWorkbenchItems,
  composerState,
  discoverActiveRunId,
  latestRunId,
  promptAcknowledged,
  responseAcknowledged,
  type ComposerState,
  type WorkbenchItem,
} from "../lib/workbench-projection";
import {
  loadLastThreadId,
  saveLastThreadId,
  threadRegistry,
  type ThreadRegistryEntry,
} from "../lib/thread-registry";
import type { RuntimeEvent } from "../api/adapters/event";
import type { RunView } from "../api/adapters/run";
import { useHealth } from "./use-health";
import { useRun } from "./use-run-detail";
import { useRuntimeEvents, type RuntimeEventsState } from "./use-runtime-events";
import { createWorkbenchSession, type SubmissionSnapshot } from "./workbench-session";

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
}

/**
 * Workbench orchestration: selected thread (local registry + last-opened
 * persistence), thread-wide event replay, active-run discovery, and the
 * blocking-turn submission session. No runtime state is stored locally —
 * run data comes from `useRun`, conversation from the event replay.
 */
export function useWorkbench(): Workbench {
  const [threads, setThreads] = useState<ThreadRegistryEntry[]>(() => threadRegistry.list());
  const [threadId, setThreadIdState] = useState<string | null>(() => loadLastThreadId());
  const [promptBoundary, setPromptBoundary] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const session = useMemo(() => createWorkbenchSession({ getConfig: getConnection }), []);
  const submission = useSyncExternalStore(
    useCallback((listener) => session.subscribe(listener), [session]),
    () => session.snapshot(),
  );

  const health = useHealth();
  const connected = !health.isError;

  // Thread-wide replay (no run_id filter). runStatus "" keeps the poll loop
  // running at the default cadence across the thread's whole lifetime — a
  // workbench thread outlives any single run, so the terminal drain used by
  // Run Detail must not stop it.
  const eventsState = useRuntimeEvents({ threadId, runId: "", runStatus: "" });
  const events = eventsState.events;

  // Active run discovery: turn.started / user.message events carry run_id as
  // soon as a turn starts; a 202 POST response may confirm it before events
  // arrive (e.g. distributed queued mode). Without an active run the context
  // panel falls back to the most recent run seen in the thread.
  const discoveredActiveId = discoverActiveRunId(events);
  const activeRunId = discoveredActiveId ?? submission.confirmedRunId ?? latestRunId(events);

  const runQuery = useRun(activeRunId ?? "");
  const run = runQuery.data ?? null;

  // The run query lags behind a newly discovered id; while a discovered
  // active run's view has not loaded, conservatively treat it as busy
  // (the runtime rejects concurrent turns with 409 anyway).
  const runForActive = run !== null && run.runId === activeRunId ? run : null;
  const activeStatus =
    runForActive !== null ? runForActive.status : discoveredActiveId !== null ? "RUNNING" : null;

  const composer = composerState({
    connected,
    submitting: submission.phase === "submitting",
    activeRunStatus: activeStatus,
  });

  // POST/event reconciliation: once the real events render the local echoes,
  // drop them from the session so they cannot linger.
  useEffect(() => {
    if (
      submission.prompt !== null &&
      submission.threadId === threadId &&
      promptAcknowledged(events, submission.prompt, promptBoundary)
    ) {
      session.acknowledgePrompt();
    }
  }, [events, submission.prompt, submission.threadId, threadId, promptBoundary, session]);

  useEffect(() => {
    if (
      submission.responseText !== null &&
      responseAcknowledged(events, submission.responseText, promptBoundary)
    ) {
      session.acknowledgeResponse();
    }
  }, [events, submission.responseText, promptBoundary, session]);

  const selectThread = useCallback(
    (next: string | null) => {
      session.abortAndReset();
      setPromptBoundary(null);
      setThreadIdState(next);
      saveLastThreadId(next);
      if (next !== null) setThreads(threadRegistry.touch(next));
    },
    [session],
  );

  const createAndSelectThread = useCallback(async () => {
    if (creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const ref = await createThread(getConnection());
      setThreads(threadRegistry.add(ref.threadId));
      selectThread(ref.threadId);
    } catch (error) {
      setCreateError(error instanceof ApiError ? error.message : "Failed to create thread");
    } finally {
      setCreating(false);
    }
  }, [creating, selectThread]);

  const removeThread = useCallback((id: string) => {
    setThreads(threadRegistry.remove(id));
  }, []);

  const send = useCallback(
    async (text: string): Promise<SendOutcome> => {
      let id = threadId;
      if (id === null) {
        // Auto-create a thread for the first message from the empty state.
        setCreating(true);
        setCreateError(null);
        try {
          const ref = await createThread(getConnection());
          id = ref.threadId;
          setThreads(threadRegistry.add(ref.threadId));
          selectThread(ref.threadId);
        } catch (error) {
          setCreateError(error instanceof ApiError ? error.message : "Failed to create thread");
          return "failed";
        } finally {
          setCreating(false);
        }
      }
      const lastEvent = events.length > 0 ? events[events.length - 1] : null;
      setPromptBoundary(lastEvent !== null ? lastEvent.eventId : null);
      await session.submit(id, text);
      const phase = session.snapshot().phase;
      if (phase === "failed") return "failed";
      if (phase === "lost") return "lost";
      return "ok";
    },
    [threadId, events, session, selectThread],
  );

  const items = useMemo(
    () =>
      composeWorkbenchItems(events, {
        prompt: submission.threadId === threadId ? submission.prompt : null,
        boundaryEventId: promptBoundary,
        responseText: submission.responseText,
      }),
    [events, submission.prompt, submission.threadId, submission.responseText, threadId, promptBoundary],
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
  };
}
