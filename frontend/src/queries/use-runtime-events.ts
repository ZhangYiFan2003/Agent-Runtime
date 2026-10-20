import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { RuntimeEvent } from "../api/adapters/event";
import { EventProtocolError, fetchRuntimeEvents } from "../sse/event-replay-client";
import { useConnection } from "../lib/connection";
import { isTerminalRunStatus } from "../lib/run-detail-view";
import type { ReplayStatus } from "../lib/events-view";

/**
 * Accumulated runtime-events replay state for one run. Deliberately not a
 * TanStack Query: a cursor-driven replay stream with dedupe and terminal
 * drain does not fit query refetch semantics, and this is transient
 * replay-cache state — nothing here is a source of truth.
 *
 * The poll loop lives in `startReplaySession` (exported so tests can drive it
 * with fake timers and an injected fetch); the hook is a thin lifecycle
 * wrapper: one loop per (threadId, runId), torn down on unmount or id change.
 */

export const MAX_KEPT_EVENTS = 1500;
export const DEDUPE_WINDOW = 2000;
/** Preview storage is independent of the bounded debug event window. */
export const MAX_PREVIEW_CHARS = 65_536;

export interface StreamPreview {
  runId: string;
  turnId: string;
  eventId: number;
  timestamp: string;
  text: string;
  incomplete: boolean;
}

export interface RuntimeEventsState {
  events: RuntimeEvent[];
  status: ReplayStatus;
  error: string | null;
  preview: StreamPreview | null;
}

/** Poll cadence: brisk catch-up during the terminal drain, ~1.5 s otherwise. */
export function pollIntervalMs(status: string | undefined): number {
  if (status !== undefined && status !== "" && isTerminalRunStatus(status)) return 500;
  return 1500;
}

/**
 * Exponential backoff with jitter for network failures: 1 s → 2 s → 4 s →
 * 8 s cap, ±25 % jitter. `random` is injectable for deterministic tests.
 */
export function nextBackoffMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(8000, 1000 * 2 ** Math.max(0, attempt - 1));
  return Math.round(base * (0.75 + 0.5 * random()));
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export interface ReplaySessionOptions {
  threadId: string;
  runId: string;
  getConfig: () => { baseUrl: string; apiKey: string };
  /** Latest run status (from the run query) — read fresh each iteration. */
  getRunStatus: () => string | undefined;
  onUpdate: (state: RuntimeEventsState) => void;
  /** Injectable fetch for tests. */
  fetchImpl?: typeof fetch;
  /** Injectable backoff for tests (defaults to `nextBackoffMs`). */
  backoff?: (attempt: number) => number;
  latest?: boolean;
}

export interface ReplaySession {
  stop: () => void;
}

/**
 * Start a persisted-replay poll loop for one (threadId, runId). Accumulates
 * events with bounded-history + bounded-dedupe semantics (dropping old UI
 * entries never moves the cursor back), reconnects with backoff on network
 * failures, stops permanently on auth errors / thread 404, and performs a
 * terminal drain through the watermark observed by a round begun after the
 * Run became terminal. `stop()` aborts any
 * in-flight fetch and the pending sleep.
 */
export function startReplaySession(options: ReplaySessionOptions): ReplaySession {
  let cancelled = false;
  let controller: AbortController | null = null;

  const seen = new Set<number>();
  let kept: RuntimeEvent[] = [];
  let cursor: number | null = null;
  let failures = 0;
  let status: ReplayStatus = "connecting";
  let preview: StreamPreview | null = null;
  let observedStart: string | null = null;
  let initial = true;

  const emit = (error: string | null = null) => {
    if (!cancelled) options.onUpdate({ events: [...kept], status, error, preview });
  };
  emit();

  void (async () => {
    while (!cancelled) {
      const round = new AbortController();
      controller = round;
      try {
        const terminalAtStart = isTerminalRunStatus(options.getRunStatus() ?? "");
        const page = await fetchRuntimeEvents(
          options.getConfig(),
          { threadId: options.threadId, runId: options.runId, afterId: cursor, latest: initial && options.latest },
          { signal: round.signal, fetchImpl: options.fetchImpl },
        );
        if (cancelled) break;
        failures = 0;

        for (const event of page.events) {
          if (seen.has(event.eventId)) continue;
          seen.add(event.eventId);
          kept.push(event);
          cursor = cursor === null ? event.eventId : Math.max(cursor, event.eventId);
          const currentPreview = preview as StreamPreview | null;
          if (event.parentRunId === null && event.runId !== null) {
            if (event.eventType === "run.started" || event.eventType === "turn.started") {
              observedStart = event.runId;
              if (currentPreview?.runId !== event.runId) preview = null;
            }
            if ((event.eventType === "text_delta" || event.eventType === "assistant.delta") && event.turnId !== null && typeof event.payload.text === "string") {
              const previous: StreamPreview | null = currentPreview?.runId === event.runId && currentPreview.turnId === event.turnId ? currentPreview : null;
              const incomplete: boolean = previous?.incomplete ?? (!!options.latest && observedStart !== event.runId);
              const text: string = (previous?.text ?? "") + event.payload.text;
              preview = { runId: event.runId, turnId: event.turnId, eventId: event.eventId, timestamp: event.timestamp,
                incomplete: incomplete || text.length > MAX_PREVIEW_CHARS, text: incomplete || text.length > MAX_PREVIEW_CHARS ? "" : text };
            }
          }
        }
        cursor = page.afterId;
        initial = false;
        if (kept.length > MAX_KEPT_EVENTS) kept = kept.slice(kept.length - MAX_KEPT_EVENTS);
        if (seen.size > DEDUPE_WINDOW) {
          // The bounded UI history already covers this window.
          seen.clear();
          for (const event of kept) seen.add(event.eventId);
        }

        if (terminalAtStart && !page.hasMore && page.afterId >= page.watermark) {
          status = "stopped";
          emit();
          break;
        }
        status = "following";
        emit();
        if (!page.hasMore) await sleep(pollIntervalMs(options.getRunStatus()), round.signal);
      } catch (error) {
        if (cancelled || isAbortError(error)) break;
        if (error instanceof EventProtocolError) {
          status = "stopped";
          emit(error.message);
          break;
        }
        if (error instanceof ApiError) {
          if (error.status === 401 || error.status === 403) {
            status = "auth-error";
            emit(error.message);
            break;
          }
          if (error.status === 404) {
            status = "not-found";
            emit(error.message);
            break;
          }
        }
        failures += 1;
        status = "reconnecting";
        emit(error instanceof Error ? error.message : "Connection lost");
        try {
          await sleep((options.backoff ?? nextBackoffMs)(failures), round.signal);
        } catch {
          break; // aborted during backoff
        }
      }
    }
    controller = null;
  })();

  return {
    stop() {
      cancelled = true;
      controller?.abort();
    },
  };
}

export function useRuntimeEvents({
  threadId,
  runId,
  runStatus,
  latest = false,
}: {
  threadId: string | null;
  runId: string;
  runStatus: string | undefined;
  latest?: boolean;
}): RuntimeEventsState {
  const connection = useConnection();
  const key = JSON.stringify([connection.scope, connection.generation, threadId, runId, latest]);
  const empty: RuntimeEventsState = {
    events: [],
    status: "stopped",
    error: null,
    preview: null,
  };
  const [scopedState, setScopedState] = useState({ key, state: empty });
  // The loop reads the latest run status without restarting on each change;
  // the terminal drain depends on observing the transition inside the loop.
  const statusRef = useRef(runStatus);
  statusRef.current = runStatus;

  const idle = threadId === null || runStatus === undefined;

  useEffect(() => {
    if (idle) {
      setScopedState({ key, state: empty });
      return;
    }
    const session = startReplaySession({
      threadId: threadId as string,
      runId,
      getConfig: () => connection.config,
      getRunStatus: () => statusRef.current,
      onUpdate: (state) => setScopedState({ key, state }),
      latest,
    });
    return () => session.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runStatus is read
    // via statusRef; only its defined-ness (idle) is a lifecycle dependency.
  }, [idle, threadId, runId, connection.scope, connection.generation, latest]);

  return scopedState.key === key ? scopedState.state : { ...empty, status: idle ? "stopped" : "connecting" };
}
