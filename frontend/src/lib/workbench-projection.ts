import type { RuntimeEvent } from "../api/adapters/event";
import type { ThreadMessage } from "../api/messages";
import type { StreamPreview } from "../queries/use-runtime-events";
import { isTerminalRunStatus } from "./run-detail-view";
import { summarizeEvent } from "./events-view";
import { truncateId } from "./format";

/**
 * Pure projector: thread-wide `RuntimeEvent[]` → Workbench feed items.
 *
 * The Workbench feed is a conversation-first view, not the raw event stream
 * (that stays available in Run Inspector): user/assistant messages, paired
 * tool rows, run lifecycle markers, approvals, and errors. Known low-level
 * chatter (checkpoint.*, llm.*, step.*, …) is filtered out; genuinely unknown
 * event types degrade to a compact generic row — never a crash, never an
 * invented field.
 */

export type ToolRowStatus = "running" | "completed" | "error";
export type LifecycleTone = "neutral" | "accent" | "danger" | "info" | "warn";

export type WorkbenchItem =
  | {
      kind: "user";
      id: string;
      eventId: number | null;
      timestamp: string | null;
      text: string;
      runId: string | null;
      turnId: string | null;
      /** Local pending echo — replaced by the real user.message event. */
      pending: boolean;
    }
  | {
      kind: "assistant";
      id: string;
      eventId: number | null;
      timestamp: string | null;
      text: string;
      runId: string | null;
      turnId: string | null;
      /** True when the text came from the turn-POST response, not an event. */
      fromResponse: boolean;
      preview?: boolean;
    }
  | {
      kind: "tool";
      id: string;
      eventId: number | null;
      timestamp: string | null;
      name: string;
      status: ToolRowStatus;
      /** Only present when the payload actually carried them. */
      input: unknown;
      result: unknown;
      isError: boolean;
      reused: boolean;
      durationMs: number | null;
      callEvent: RuntimeEvent;
      resultEvent: RuntimeEvent | null;
    }
  | {
      kind: "lifecycle";
      id: string;
      eventId: number | null;
      timestamp: string | null;
      label: string;
      tone: LifecycleTone;
      runId: string | null;
      eventType: string;
    }
  | {
      kind: "approval";
      id: string;
      eventId: number | null;
      timestamp: string | null;
      toolName: string | null;
      reason: string | null;
    }
  | {
      kind: "error";
      id: string;
      eventId: number | null;
      timestamp: string | null;
      message: string;
    }
  | {
      kind: "other";
      id: string;
      eventId: number | null;
      timestamp: string | null;
      eventType: string;
      summary: string;
    };

/* ------------------------------------------------------------------ */
/* Noise filter                                                        */
/* ------------------------------------------------------------------ */

/** Known event prefixes that are too chatty for the conversation feed. */
const NOISE_PREFIXES = [
  "thread.",
  "turn.started",
  "run.admitted",
  "run.resumed",
  "resume.",
  "step.",
  "agent.step.",
  "llm.",
  "tool.",
  "checkpoint.",
  "plan.",
  "multi_agent.",
  "worker.",
  "review.",
  "synthesis.",
];

function isNoise(eventType: string): boolean {
  return NOISE_PREFIXES.some((prefix) => eventType.startsWith(prefix));
}

const LIFECYCLE_TYPES = new Set([
  "run.started",
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.interrupted",
  "turn.completed",
  "interrupt.resolved",
]);

/* ------------------------------------------------------------------ */
/* Payload helpers (payload keys are the backend's flattened envelope)  */
/* ------------------------------------------------------------------ */

function payloadStr(event: RuntimeEvent, key: string): string | null {
  const value = event.payload[key];
  return typeof value === "string" && value !== "" ? value : null;
}

function toolKey(event: RuntimeEvent): string | null {
  const key = payloadStr(event, "tool_call_id") ?? payloadStr(event, "invocation_id");
  return key === null ? null : JSON.stringify([event.threadId, event.runId, event.turnId, key]);
}

function durationBetween(a: RuntimeEvent, b: RuntimeEvent): number | null {
  const start = Date.parse(a.timestamp);
  const end = Date.parse(b.timestamp);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}

/* ------------------------------------------------------------------ */
/* Lifecycle labels                                                    */
/* ------------------------------------------------------------------ */

function lifecycleItem(event: RuntimeEvent): WorkbenchItem | null {
  const runTag = event.runId !== null ? ` · ${truncateId(event.runId, 10)}` : "";
  const base = {
    id: `e${event.eventId}`,
    eventId: event.eventId,
    timestamp: event.timestamp,
    runId: event.runId,
    eventType: event.eventType,
  };
  switch (event.eventType) {
    case "run.started":
      return { kind: "lifecycle", label: `Run started${runTag}`, tone: "accent", ...base };
    case "run.completed":
      return { kind: "lifecycle", label: `Run completed${runTag}`, tone: "neutral", ...base };
    case "run.failed": {
      const error = payloadStr(event, "error");
      return {
        kind: "lifecycle",
        label: `Run failed${runTag}${error !== null ? ` — ${error}` : ""}`,
        tone: "danger",
        ...base,
      };
    }
    case "run.cancelled":
      return { kind: "lifecycle", label: `Run cancelled${runTag}`, tone: "neutral", ...base };
    case "run.interrupted":
      return { kind: "lifecycle", label: `Run interrupted${runTag}`, tone: "info", ...base };
    case "turn.completed": {
      const tokens = event.payload.total_tokens;
      const suffix = typeof tokens === "number" && Number.isFinite(tokens) ? ` · ${tokens} tokens` : "";
      return { kind: "lifecycle", label: `Turn completed${suffix}`, tone: "neutral", ...base };
    }
    case "interrupt.resolved":
      return { kind: "lifecycle", label: "Approval resolved", tone: "neutral", ...base };
    default:
      return null;
  }
}

/* ------------------------------------------------------------------ */
/* Main projection                                                     */
/* ------------------------------------------------------------------ */

export function projectWorkbenchEvents(events: RuntimeEvent[]): WorkbenchItem[] {
  const items: WorkbenchItem[] = [];
  /** tool pairing: key → index into items (the tool row to update). */
  const openTools = new Map<string, number>();
  const seenMessages = new Set<string>();

  for (const event of events) {
    const type = event.eventType;

    if (type === "user.message" || type === "assistant.message") {
      if (payloadStr(event, "text") === null) continue;
      if (event.parentRunId !== null) {
        items.push({
          kind: "other", id: `e${event.eventId}`, eventId: event.eventId,
          timestamp: event.timestamp, eventType: `child.${type}`,
          summary: payloadStr(event, "text") ?? "",
        });
        continue;
      }
      if (event.runId !== null && event.turnId !== null) {
        const key = JSON.stringify([type, event.threadId, event.runId, event.turnId]);
        if (seenMessages.has(key)) continue;
        seenMessages.add(key);
      }
    }

    if (type === "user.message") {
      const text = payloadStr(event, "text");
      if (text === null) continue;
      items.push({
        kind: "user",
        id: `e${event.eventId}`,
        eventId: event.eventId,
        timestamp: event.timestamp,
        text,
        runId: event.runId,
        turnId: event.turnId,
        pending: false,
      });
      continue;
    }

    if (type === "assistant.message") {
      const text = payloadStr(event, "text");
      if (text === null) continue;
      items.push({
        kind: "assistant",
        id: `e${event.eventId}`,
        eventId: event.eventId,
        timestamp: event.timestamp,
        text,
        runId: event.runId,
        turnId: event.turnId,
        fromResponse: false,
      });
      continue;
    }

    if (type === "tool_call") {
      const name = payloadStr(event, "name") ?? "tool";
      const key = toolKey(event);
      const item: WorkbenchItem = {
        kind: "tool",
        id: key !== null ? `tool-${key}` : `e${event.eventId}`,
        eventId: event.eventId,
        timestamp: event.timestamp,
        name,
        status: "running",
        input: "input" in event.payload ? event.payload.input : undefined,
        result: undefined,
        isError: false,
        reused: false,
        durationMs: null,
        callEvent: event,
        resultEvent: null,
      };
      if (key !== null && openTools.has(key)) {
        // Duplicate call id (retry/re-emit): update in place rather than dup.
        items[openTools.get(key) as number] = item;
      } else {
        if (key !== null) openTools.set(key, items.length);
        items.push(item);
      }
      continue;
    }

    if (type === "tool_result") {
      const key = toolKey(event);
      const isError = event.payload.is_error === true;
      const reused = event.payload.reused === true;
      const result = "result" in event.payload ? event.payload.result : undefined;
      const openIndex = key !== null ? openTools.get(key) : undefined;
      if (openIndex !== undefined) {
        const existing = items[openIndex];
        if (existing.kind === "tool") {
          items[openIndex] = {
            ...existing,
            status: isError ? "error" : "completed",
            result,
            isError,
            reused,
            durationMs: durationBetween(existing.callEvent, event),
            resultEvent: event,
          };
          openTools.delete(key as string);
          continue;
        }
      }
      // Orphan result (no matching call seen): still render a completed row.
      items.push({
        kind: "tool",
        id: `e${event.eventId}`,
        eventId: event.eventId,
        timestamp: event.timestamp,
        name: payloadStr(event, "name") ?? "tool",
        status: isError ? "error" : "completed",
        input: undefined,
        result,
        isError,
        reused,
        durationMs: null,
        callEvent: event,
        resultEvent: event,
      });
      continue;
    }

    if (type === "interrupt.created") {
      items.push({
        kind: "approval",
        id: `e${event.eventId}`,
        eventId: event.eventId,
        timestamp: event.timestamp,
        toolName: payloadStr(event, "tool_name"),
        reason: payloadStr(event, "reason"),
      });
      continue;
    }

    if (type === "error") {
      items.push({
        kind: "error",
        id: `e${event.eventId}`,
        eventId: event.eventId,
        timestamp: event.timestamp,
        message: summarizeEvent(event) || "Runtime error",
      });
      continue;
    }

    if (LIFECYCLE_TYPES.has(type)) {
      const item = lifecycleItem(event);
      if (item !== null) items.push(item);
      continue;
    }

    if (isNoise(type)) continue;

    // Unknown future event type: compact generic row, never a crash.
    items.push({
      kind: "other",
      id: `e${event.eventId}`,
      eventId: event.eventId,
      timestamp: event.timestamp,
      eventType: type,
      summary: summarizeEvent(event),
    });
  }

  return items;
}

/* ------------------------------------------------------------------ */
/* Pending-submission reconciliation                                   */
/* ------------------------------------------------------------------ */

export interface PendingSubmissionView {
  threadId: string | null;
  /** Local echo of the in-flight user message (null when acknowledged). */
  prompt: string | null;
  /** Assistant text from a completed turn-POST 200 (fallback when the
   *  assistant.message event has not arrived). */
  responseText: string | null;
  /** Run confirmed by the turn response, used to link fallback citations. */
  runId: string | null;
  turnId: string | null;
}

/** True when a real user.message event covers the pending local echo. */
export function promptAcknowledged(
  events: RuntimeEvent[],
  threadId: string | null,
  runId: string | null,
  turnId: string | null,
): boolean {
  if (threadId === null || runId === null || turnId === null) return false;
  return events.some(
    (event) =>
      event.threadId === threadId && event.runId === runId && event.turnId === turnId && event.parentRunId === null &&
      event.eventType === "user.message" &&
      payloadStr(event, "text") !== null,
  );
}

/** True when an assistant.message event already rendered the POST text. */
export function responseAcknowledged(
  events: RuntimeEvent[],
  threadId: string | null,
  runId: string | null,
  turnId: string | null,
): boolean {
  if (threadId === null || runId === null || turnId === null) return false;
  return events.some(
    (event) =>
      event.threadId === threadId && event.runId === runId && event.turnId === turnId && event.parentRunId === null &&
      event.eventType === "assistant.message" &&
      payloadStr(event, "text") !== null,
  );
}

/**
 * Projected events plus the local pending/response echoes. Events are the
 * conversation source of truth: echoes appear only while no real event
 * covers them (no permanent duplicates, no double-rendered assistant text).
 */
export function composeWorkbenchItems(
  events: RuntimeEvent[],
  pending: PendingSubmissionView,
): WorkbenchItem[] {
  const items = projectWorkbenchEvents(events);

  if (pending.prompt !== null && !promptAcknowledged(events, pending.threadId, pending.runId, pending.turnId)) {
    items.push({
      kind: "user",
      id: "pending-prompt",
      eventId: null,
      timestamp: null,
      text: pending.prompt,
      runId: pending.runId,
      turnId: pending.turnId,
      pending: true,
    });
  }

  if (
    pending.responseText !== null &&
    !responseAcknowledged(events, pending.threadId, pending.runId, pending.turnId)
  ) {
    items.push({
      kind: "assistant",
      id: "response-text",
      eventId: null,
      timestamp: null,
      text: pending.responseText,
      runId: pending.runId,
      turnId: pending.turnId,
      fromResponse: true,
    });
  }

  return items;
}

export function messageAcknowledged(messages: ThreadMessage[], role: ThreadMessage["role"], pending: PendingSubmissionView): boolean {
  return messages.some((message) => message.role === role && message.thread_id === pending.threadId &&
    message.run_id === pending.runId && message.turn_id === pending.turnId);
}

/** Only durable message pages establish conversation history; debug rows stay bounded. */
export function composeDurableWorkbenchItems(
  messages: ThreadMessage[],
  events: RuntimeEvent[],
  pending: PendingSubmissionView,
  preview: StreamPreview | null,
): WorkbenchItem[] {
  const items = projectWorkbenchEvents(events.filter((event) =>
    event.eventType !== "user.message" && event.eventType !== "assistant.message" &&
    event.eventType !== "text_delta" && event.eventType !== "assistant.delta"));
  for (const message of messages) {
    const base = { id: `m${message.id}`, eventId: message.id, timestamp: message.created_at,
      text: message.text, runId: message.run_id, turnId: message.turn_id };
    items.push(message.role === "user" ? { kind: "user", ...base, pending: false } : { kind: "assistant", ...base, fromResponse: false });
  }
  if (preview !== null && !messages.some((message) => message.role === "assistant" && message.run_id === preview.runId && message.turn_id === preview.turnId) &&
    !(pending.responseText !== null && pending.runId === preview.runId && pending.turnId === preview.turnId)) {
    items.push({ kind: "assistant", id: `preview-${preview.runId}-${preview.turnId}`, eventId: preview.eventId,
      timestamp: preview.timestamp, text: preview.incomplete ? "Live preview unavailable. Waiting for the saved result…" : preview.text,
      runId: preview.runId, turnId: preview.turnId, fromResponse: false, preview: true });
  }
  items.sort((a, b) => (a.eventId ?? Number.MAX_SAFE_INTEGER) - (b.eventId ?? Number.MAX_SAFE_INTEGER));
  if (pending.prompt !== null && !messageAcknowledged(messages, "user", pending)) {
    items.push({ kind: "user", id: "pending-prompt", eventId: null, timestamp: null, text: pending.prompt,
      runId: pending.runId, turnId: pending.turnId, pending: true });
  }
  if (pending.responseText !== null && !messageAcknowledged(messages, "assistant", pending)) {
    items.push({ kind: "assistant", id: "response-text", eventId: null, timestamp: null, text: pending.responseText,
      runId: pending.runId, turnId: pending.turnId, fromResponse: true });
  }
  return items;
}

/* ------------------------------------------------------------------ */
/* Run discovery from events                                           */
/* ------------------------------------------------------------------ */

export interface ThreadRunMarker {
  runId: string;
  turnId: string | null;
  parentRunId: string | null;
  firstEventId: number;
  terminal: boolean;
}

const RUN_TERMINAL_EVENTS = new Set(["run.completed", "run.failed", "run.cancelled"]);

/**
 * Runs observed in the thread's event stream, in first-appearance order.
 * A run is registered by the first event carrying its run_id (turn.started
 * and user.message carry it immediately when a turn starts) and marked
 * terminal by its run.completed/failed/cancelled event.
 */
export function deriveThreadRuns(events: RuntimeEvent[]): ThreadRunMarker[] {
  const markers = new Map<string, ThreadRunMarker>();
  for (const event of events) {
    if (event.runId === null) continue;
    let marker = markers.get(event.runId);
    if (marker === undefined) {
      marker = {
        runId: event.runId,
        turnId: event.turnId,
        parentRunId: event.parentRunId,
        firstEventId: event.eventId,
        terminal: false,
      };
      markers.set(event.runId, marker);
    }
    if (RUN_TERMINAL_EVENTS.has(event.eventType)) marker.terminal = true;
  }
  return [...markers.values()];
}

/**
 * The run the Workbench should treat as active: the most recently started
 * non-terminal ROOT run (child runs belong to their parent's context).
 * Null when every observed run is terminal.
 */
export function discoverActiveRunId(events: RuntimeEvent[]): string | null {
  const markers = deriveThreadRuns(events);
  for (let i = markers.length - 1; i >= 0; i -= 1) {
    const marker = markers[i];
    if (!marker.terminal && marker.parentRunId === null) return marker.runId;
  }
  return null;
}

/** Most recently observed run, terminal or not (context-panel fallback). */
export function latestRunId(events: RuntimeEvent[]): string | null {
  const markers = deriveThreadRuns(events).filter((marker) => marker.parentRunId === null);
  return markers.length > 0 ? markers[markers.length - 1].runId : null;
}

/* ------------------------------------------------------------------ */
/* Composer state                                                      */
/* ------------------------------------------------------------------ */

export interface ComposerState {
  disabled: boolean;
  /** Human-readable reason when disabled for an external cause. */
  reason: string | null;
}

/**
 * Send is disabled while: the runtime is disconnected, a turn submission is
 * in flight for this thread, or the thread's active run is non-terminal
 * (the runtime rejects concurrent turns with 409 anyway).
 */
export function composerState(options: {
  connected: boolean;
  submitting: boolean;
  activeRunStatus: string | null;
  unconfirmed?: boolean;
  runLookupMissing?: boolean;
}): ComposerState {
  if (!options.connected) {
    return { disabled: true, reason: "Runtime service is unreachable." };
  }
  if (options.submitting) {
    return { disabled: true, reason: null };
  }
  if (options.unconfirmed) {
    return { disabled: true, reason: "Confirm or forget the unconfirmed submission before sending another message." };
  }
  if (options.runLookupMissing) {
    return { disabled: false, reason: "Run lookup repeatedly returned 404. Retry lookup or submit to check the Thread's real availability." };
  }
  if (options.activeRunStatus !== null && !isTerminalRunStatus(options.activeRunStatus)) {
    return { disabled: true, reason: "A turn is already running in this thread." };
  }
  return { disabled: false, reason: null };
}
