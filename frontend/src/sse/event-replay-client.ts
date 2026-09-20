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
}

function buildEventsUrl(baseUrl: string, params: FetchRuntimeEventsParams): string {
  const base = baseUrl.replace(/\/+$/, "");
  const query = new URLSearchParams();
  if (params.afterId !== null && params.afterId !== undefined) {
    // after_id is exclusive — never cursor+1.
    query.set("after_id", String(params.afterId));
  }
  if (params.runId !== undefined && params.runId !== "") query.set("run_id", params.runId);
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return `${base}/v1/threads/${encodeURIComponent(params.threadId)}/events${suffix}`;
}

/**
 * Fetch one replay batch: stream the SSE body, parse frames, validate each
 * `data:` JSON against the zod envelope, and return adapted view models
 * sorted by `event_id` ascending. Malformed frames are skipped (with a
 * dev-mode warning) without failing the batch. Non-2xx responses throw
 * `ApiError` (both backend error shapes normalized).
 */
export async function fetchRuntimeEvents(
  config: ConnectionConfig,
  params: FetchRuntimeEventsParams,
  options: { signal?: AbortSignal; fetchImpl?: typeof fetch } = {},
): Promise<RuntimeEvent[]> {
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

  // Replay bodies are bounded (the response closes); reading the full text
  // is the simple, correct transport here.
  const text = await response.text();
  const frames = parseSseFrames(text);

  const events: RuntimeEvent[] = [];
  for (const frame of frames) {
    let raw: unknown;
    try {
      raw = JSON.parse(frame.data);
    } catch {
      warnSkipped(frame.data);
      continue;
    }
    const envelope = parseEventEnvelope(raw);
    if (envelope === null) {
      warnSkipped(frame.data);
      continue;
    }
    events.push(adaptRuntimeEvent(envelope));
  }

  events.sort((a, b) => a.eventId - b.eventId);
  return events;
}

function warnSkipped(data: string): void {
  if (import.meta.env?.DEV) {
    console.warn("[events] skipped malformed SSE frame data:", data.slice(0, 200));
  }
}
