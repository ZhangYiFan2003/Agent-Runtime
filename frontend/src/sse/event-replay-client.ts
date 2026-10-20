import { ApiError, normalizeErrorBody } from "../api/client";
import { adaptRuntimeEvent, parseEventEnvelope, type RuntimeEvent } from "../api/adapters/event";
import { parseSseFrames } from "./parser";
import type { ConnectionConfig } from "../lib/connection";

/**
 * Fetch-based persisted-event replay client.
 *
 * `GET /v1/threads/{thread_id}/events?after_id={int}&run_id={run_id}` returns
 * a bounded `text/event-stream` body that closes after the stored frames
 * ("replay mode"). The caller polls with an exclusive `after_id` cursor; a
 * future `follow=1` long-lived mode would replace this module's transport
 * only — the frame parsing and envelope validation stay identical.
 */

export interface FetchRuntimeEventsParams {
  threadId: string;
  runId?: string;
  /** Exclusive cursor: only events with event_id > afterId are returned. */
  afterId?: number | null;
  limit?: number;
  /** Start with the newest debug page, then follow forwards. */
  latest?: boolean;
}

export interface RuntimeEventsPage {
  events: RuntimeEvent[];
  afterId: number;
  hasMore: boolean;
  watermark: number;
  hasOlder: boolean;
}

export class EventProtocolError extends Error {}

function buildEventsUrl(baseUrl: string, params: FetchRuntimeEventsParams): string {
  const base = baseUrl.replace(/\/+$/, "");
  const query = new URLSearchParams();
  if (params.afterId !== null && params.afterId !== undefined) {
    // after_id is exclusive — never cursor+1.
    query.set("after_id", String(params.afterId));
  }
  if (params.runId !== undefined && params.runId !== "") query.set("run_id", params.runId);
  query.set("limit", String(params.limit ?? 200));
  if (params.latest) query.set("latest", "1");
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return `${base}/v1/threads/${encodeURIComponent(params.threadId)}/events${suffix}`;
}

/**
 * Fetch one replay batch: stream the SSE body, parse frames, validate each
 * `data:` JSON against the zod envelope, and return adapted view models
 * in `event_id` ascending order. Invalid envelopes or pagination metadata
 * fail the batch without advancing the cursor. Non-2xx responses throw
 * `ApiError` (both backend error shapes normalized).
 */
export async function fetchRuntimeEvents(
  config: ConnectionConfig,
  params: FetchRuntimeEventsParams,
  options: { signal?: AbortSignal; fetchImpl?: typeof fetch } = {},
): Promise<RuntimeEventsPage> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (config.apiKey) headers.Authorization = `Bearer ${config.apiKey}`;

  let response: Response;
  try {
    response = await fetchImpl(buildEventsUrl(config.baseUrl, params), {
      method: "GET",
      headers,
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError({
      status: 0,
      message: error instanceof Error ? error.message : "Network request failed",
    });
  }

  if (!response.ok) {
    const text = await response.text();
    let body: unknown = text;
    if (text !== "") {
      try {
        body = JSON.parse(text);
      } catch {
        /* keep raw text — normalizeErrorBody accepts strings */
      }
    }
    throw new ApiError(normalizeErrorBody(response.status, body));
  }

  if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("text/event-stream")) {
    throw new EventProtocolError("Expected text/event-stream for event replay");
  }

  const integerHeader = (name: string): number => {
    const raw = response.headers.get(name);
    if (raw === null || !/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
      throw new EventProtocolError(`Invalid event replay header: ${name}`);
    }
    return Number(raw);
  };
  const afterId = integerHeader("X-Axiom-Events-After-Id");
  const watermark = integerHeader("X-Axiom-Events-Watermark");
  const more = response.headers.get("X-Axiom-Events-Has-More");
  if ((more !== "true" && more !== "false") || afterId > watermark || afterId < (params.afterId ?? 0)) {
    throw new EventProtocolError("Invalid event replay pagination metadata");
  }
  const text = await response.text();
  const frames = parseSseFrames(text);

  const events: RuntimeEvent[] = [];
  for (const frame of frames) {
    let raw: unknown;
    try {
      raw = JSON.parse(frame.data);
    } catch {
      throw new EventProtocolError("Invalid JSON in event replay frame");
    }
    const envelope = parseEventEnvelope(raw);
    if (envelope === null || envelope.thread_id !== params.threadId ||
      (params.runId && envelope.run_id !== params.runId) ||
      (frame.id !== undefined && frame.id !== String(envelope.event_id)) ||
      (frame.event !== undefined && frame.event !== envelope.event_type)) {
      throw new EventProtocolError("Invalid event replay envelope");
    }
    events.push(adaptRuntimeEvent(envelope));
  }

  let previous = params.afterId ?? 0;
  for (const event of events) {
    if (event.eventId <= previous || event.eventId > afterId) throw new EventProtocolError("Invalid event replay ordering");
    previous = event.eventId;
  }
  if ((events.length > 0 && previous !== afterId) || (events.length === 0 && afterId !== (params.afterId ?? 0) && !params.latest) ||
    (more === "true" && (events.length === 0 || afterId >= watermark)) || events.length > (params.limit ?? 200)) {
    throw new EventProtocolError("Invalid event replay boundary");
  }
  return { events, afterId, watermark, hasMore: more === "true", hasOlder: response.headers.get("X-Axiom-Events-Has-Older") === "true" };
}
