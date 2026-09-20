import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type { RuntimeEvent } from "../api/adapters/event";
import { fetchRuntimeEvents } from "../sse/event-replay-client";
import { getConnection } from "../lib/connection";
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
/** Polls performed after the run reaches a terminal status before stopping. */
export const TERMINAL_DRAIN_POLLS = 2;

export interface RuntimeEventsState {
  events: RuntimeEvent[];
  status: ReplayStatus;
  error: string | null;
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
}

export interface ReplaySession {
  stop: () => void;
}

/**
 * Start a persisted-replay poll loop for one (threadId, runId). Accumulates
 * events with bounded-history + bounded-dedupe semantics (dropping old UI
 * entries never moves the cursor back), reconnects with backoff on network
 * failures, stops permanently on auth errors / thread 404, and performs a
 * bounded terminal drain (exactly `TERMINAL_DRAIN_POLLS` polls after the run
 * becomes terminal) so late events are still accepted. `stop()` aborts any
 * in-flight fetch and the pending sleep.
 */
export function startReplaySession(options: ReplaySessionOptions): ReplaySession {
  let cancelled = false;
  let controller: AbortController | null = null;

  const seen = new Set<number>();
  let kept: RuntimeEvent[] = [];
  let cursor: number | null = null;
  let failures = 0;
  let drainRemaining: number | null = null;
  let status: ReplayStatus = "connecting";

  const emit = (error: string | null = null) => {
    options.onUpdate({ events: [...kept], status, error });
  };
  emit();

  void (async () => {
    while (!cancelled) {
      const round = new AbortController();
      controller = round;
      try {
        const batch = await fetchRuntimeEvents(
          options.getConfig(),
          { threadId: options.threadId, runId: options.runId, afterId: cursor },
          { signal: round.signal, fetchImpl: options.fetchImpl },
        );
        failures = 0;

        for (const event of batch) {
          if (seen.has(event.eventId)) continue;
          seen.add(event.eventId);
          kept.push(event);
          cursor = cursor === null ? event.eventId : Math.max(cursor, event.eventId);
        }
        if (kept.length > MAX_KEPT_EVENTS) kept = kept.slice(kept.length - MAX_KEPT_EVENTS);
        if (seen.size > DEDUPE_WINDOW) {
          // The bounded UI history already covers this window.
          seen.clear();
          for (const event of kept) seen.add(event.eventId);
        }

        if (drainRemaining === null && isTerminalRunStatus(options.getRunStatus() ?? "")) {
          drainRemaining = TERMINAL_DRAIN_POLLS;
        }
        if (drainRemaining !== null) {
          drainRemaining -= 1;
          if (drainRemaining <= 0) {
            status = "stopped";
            emit();
            break;
          }
        }
        status = "following";
        emit();
        await sleep(pollIntervalMs(options.getRunStatus()), round.signal);
      } catch (error) {
        if (cancelled || isAbortError(error)) break;
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
}: {
  threadId: string | null;
  runId: string;
  runStatus: string | undefined;
}): RuntimeEventsState {
  const [state, setState] = useState<RuntimeEventsState>({
    events: [],
    status: "stopped",
    error: null,
  });
  // The loop reads the latest run status without restarting on each change;
  // the terminal drain depends on observing the transition inside the loop.
  const statusRef = useRef(runStatus);
  statusRef.current = runStatus;

  const idle = threadId === null || runStatus === undefined;

  useEffect(() => {
    if (idle) {
      setState({ events: [], status: "stopped", error: null });
      return;
    }
    const session = startReplaySession({
      threadId: threadId as string,
      runId,
      getConfig: getConnection,
      getRunStatus: () => statusRef.current,
      onUpdate: setState,
    });
    return () => session.stop();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- runStatus is read
    // via statusRef; only its defined-ness (idle) is a lifecycle dependency.
  }, [idle, threadId, runId]);

  return state;
}
