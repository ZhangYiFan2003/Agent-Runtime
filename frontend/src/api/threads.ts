import { ApiError, apiPost } from "./client";
import type { ConnectionConfig } from "../lib/connection";

/**
 * Threads + turns API.
 *
 * `POST /v1/threads/{id}/turns` is a LONG-BLOCKING request: the server holds
 * the response until the run reaches a terminal or waiting state. It is not
 * an async "returns run_id immediately" API, so the turn POST:
 *   - uses an effectively unbounded timeout (the 10 s client default would
 *     kill every real turn),
 *   - is never auto-retried (a retry could double-execute a turn),
 *   - never sends an Idempotency-Key (the body key requires distributed
 *     Postgres mode and 422s otherwise; run controls keep their own keys),
 *   - parses every known response shape defensively (200 completed text,
 *     202 waiting/interrupted/distributed-queued) with everything optional
 *     except the thread id.
 */

/**
 * Effectively unbounded turn timeout: just under the 32-bit timer clamp
 * (~24.8 days). A real transport drop still surfaces via fetch rejection;
 * an explicit client-side timeout would only abort healthy long turns.
 */
export const TURN_TIMEOUT_MS = 2_147_483_647;

export interface ThreadRef {
  threadId: string;
}

export interface TurnResult {
  threadId: string;
  turnId: string | null;
  runId: string | null;
  /** e.g. WAITING_APPROVAL / WAITING_CHILD / INTERRUPTED / CANCELLED / queued. */
  status: string | null;
  /** Final assistant text on a completed 200 response; null otherwise. */
  text: string | null;
  /** Distributed mode: the run was admitted to a queue, not executed inline. */
  queued: boolean;
}

interface RequestOptions {
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/** `POST /v1/threads` → `{id}`. No request body. */
export async function createThread(
  config: ConnectionConfig,
  options: RequestOptions = {},
): Promise<ThreadRef> {
  const body = await apiPost<unknown>("/v1/threads", {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
  const id = isRecord(body) ? str(body.id) : null;
  if (id === null) {
    throw new ApiError({ status: 0, message: "Malformed thread response (missing id)" });
  }
  return { threadId: id };
}

/**
 * Parse any turn-POST response shape into one tolerant view model. Exported
 * for tests. A 200 completed root turn carries only `{thread_id, text}`
 * (no run_id) — callers must not fabricate a run from it.
 */
export function parseTurnResult(requestedThreadId: string, body: unknown): TurnResult {
  if (!isRecord(body)) {
    return {
      threadId: requestedThreadId,
      turnId: null,
      runId: null,
      status: null,
      text: null,
      queued: false,
    };
  }
  const status = str(body.status);
  return {
    threadId: str(body.thread_id) ?? requestedThreadId,
    turnId: str(body.turn_id),
    runId: str(body.run_id),
    status,
    text: str(body.text),
    queued: body.queued === true || status === "queued",
  };
}

/**
 * Submit one user message as a blocking turn. Resolves when the run reaches
 * a terminal/waiting state (or rejects with ApiError). Transport failures
 * surface once as `ApiError` with `status === 0` — the caller decides what
 * to do; nothing here retries.
 */
export async function submitTurn(
  config: ConnectionConfig,
  threadId: string,
  message: string,
  options: RequestOptions = {},
): Promise<TurnResult> {
  const body = await apiPost<unknown>(
    `/v1/threads/${encodeURIComponent(threadId)}/turns`,
    {
      baseUrl: config.baseUrl,
      apiKey: config.apiKey,
      body: { message },
      timeoutMs: TURN_TIMEOUT_MS,
      signal: options.signal,
      fetchImpl: options.fetchImpl,
    },
  );
  return parseTurnResult(threadId, body);
}
