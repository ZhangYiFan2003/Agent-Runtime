import { ApiError, apiPost } from "./client";
import { z } from "zod";
import type { ConnectionConfig } from "../lib/connection";

/**
 * Threads + turns API.
 *
 * Local turns may block until completion; distributed turns return queued.
 * Both modes share required submission identities and an Idempotency-Key.
 * Requests are never retried automatically: explicit confirmation replays the
 * same key and original message after an uncertain result.
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
  turnId: string;
  runId: string;
  status: z.infer<typeof turnResultSchema>["status"];
  /** Final assistant text on a completed 200 response; null otherwise. */
  text: string | null;
  /** Distributed mode: the run was admitted to a queue, not executed inline. */
  queued: boolean;
}

const turnResultSchema = z.object({
  thread_id: z.string().trim().min(1),
  turn_id: z.string().trim().min(1),
  run_id: z.string().trim().min(1),
  status: z.enum([
    "RUNNING", "INTERRUPTED", "WAITING_APPROVAL", "WAITING_CHILD",
    "COMPLETED", "FAILED", "CANCELLED",
  ]),
  queued: z.boolean().optional(),
  text: z.string().optional(),
});

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
 * Validate the network boundary once; a malformed success may already have
 * been accepted, so callers retain its key for explicit confirmation.
 */
export function parseTurnResult(requestedThreadId: string, body: unknown): TurnResult {
  const parsed = turnResultSchema.safeParse(body);
  if (!parsed.success || parsed.data.thread_id !== requestedThreadId) {
    throw new ApiError({
      status: 0,
      code: "INVALID_TURN_RESPONSE",
      message: "Invalid submission response: expected matching thread_id, run_id, turn_id and a known status.",
    });
  }
  const dto = parsed.data;
  return {
    threadId: dto.thread_id,
    turnId: dto.turn_id,
    runId: dto.run_id,
    status: dto.status,
    text: dto.text ?? null,
    queued: dto.queued ?? false,
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
  options: RequestOptions & { idempotencyKey: string },
): Promise<TurnResult> {
  const body = await apiPost<unknown>(
    `/v1/threads/${encodeURIComponent(threadId)}/turns`,
    {
      baseUrl: config.baseUrl,
      apiKey: config.apiKey,
      body: { message },
      headers: { "Idempotency-Key": options.idempotencyKey },
      timeoutMs: TURN_TIMEOUT_MS,
      signal: options.signal,
      fetchImpl: options.fetchImpl,
    },
  );
  return parseTurnResult(threadId, body);
}
