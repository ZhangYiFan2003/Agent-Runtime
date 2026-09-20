import type { RuntimeEvent } from "../api/adapters/event";
import { isTerminalRunStatus } from "./run-detail-view";

/**
 * Pure view logic for the runtime-events feed: event-category chips, one-line
 * payload summaries, and replay-status labels. Prefix-driven and total:
 * unknown event types degrade to OTHER + the raw event_type, never a crash.
 */

export type EventCategory =
  | "RUN"
  | "STEP"
  | "LLM"
  | "TOOL"
  | "CHECKPOINT"
  | "INTERRUPT"
  | "PLAN"
  | "WORKER"
  | "ERROR"
  | "OTHER";

const CATEGORY_PREFIXES: Array<[prefix: string, category: EventCategory]> = [
  ["run.", "RUN"],
  ["step.", "STEP"],
  ["agent.step.", "STEP"],
  ["llm.", "LLM"],
  ["tool.", "TOOL"],
  ["tool_call", "TOOL"],
  ["tool_result", "TOOL"],
  ["checkpoint.", "CHECKPOINT"],
  ["interrupt.", "INTERRUPT"],
  ["plan.", "PLAN"],
  ["multi_agent.", "WORKER"],
  ["worker.", "WORKER"],
  ["error", "ERROR"],
];

export function eventCategory(eventType: string): EventCategory {
  for (const [prefix, category] of CATEGORY_PREFIXES) {
    if (eventType.startsWith(prefix)) return category;
  }
  return "OTHER";
}

/* ------------------------------------------------------------------ */
/* Summary line                                                        */
/* ------------------------------------------------------------------ */

function primitive(value: unknown): string | null {
  if (typeof value === "string") return value === "" ? null : value;
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : null;
  if (typeof value === "boolean") return value ? "yes" : "no";
  return null;
}

function firstPrimitive(
  payload: Record<string, unknown>,
  keys: readonly string[],
): string | null {
  for (const key of keys) {
    if (!(key in payload)) continue;
    const value = primitive(payload[key]);
    if (value !== null) return value;
  }
  return null;
}

/**
 * One compact summary line built only from fields that actually exist in the
 * payload — nothing invented. Empty string when no useful field is present.
 */
export function summarizeEvent(event: RuntimeEvent): string {
  const p = event.payload;
  const type = event.eventType;

  if (type === "error" || eventCategory(type) === "ERROR") {
    return firstPrimitive(p, ["error", "message", "reason", "type"]) ?? "";
  }

  if (type.startsWith("tool.") || type === "tool_call" || type === "tool_result") {
    return firstPrimitive(p, ["tool_name", "name"]) ?? "";
  }

  if (type.startsWith("llm.")) {
    const provider = firstPrimitive(p, ["provider"]);
    const parts: string[] = [];
    if (provider !== null) parts.push(provider);
    const tokens = firstPrimitive(p, ["total_tokens", "tokens", "token_count"]);
    if (tokens !== null) parts.push(`${tokens} tokens`);
    const latency = firstPrimitive(p, ["latency_ms", "latency"]);
    if (latency !== null) parts.push(`${latency} ms`);
    if (parts.length === 0) {
      const model = firstPrimitive(p, ["model"]);
      if (model !== null) parts.push(model);
    }
    return parts.join(" · ");
  }

  if (type.startsWith("run.")) {
    return firstPrimitive(p, ["error", "message", "reason"]) ?? "";
  }

  return firstPrimitive(p, ["message", "reason", "status"]) ?? "";
}

/* ------------------------------------------------------------------ */
/* Replay status                                                       */
/* ------------------------------------------------------------------ */

export type ReplayStatus =
  | "connecting"
  | "following"
  | "reconnecting"
  | "stopped"
  | "auth-error"
  | "not-found";

/** Tiny status-line label above the feed. No "live socket" language. */
export function replayStatusLabel(
  status: ReplayStatus,
  runStatus: string | undefined,
): string {
  switch (status) {
    case "connecting":
      return "Connecting…";
    case "following":
      return isTerminalRunStatus(runStatus ?? "") ? "Catching up" : "Following";
    case "reconnecting":
      return "Reconnecting…";
    case "stopped":
      return "Stopped";
    case "auth-error":
      return "Authentication failed";
    case "not-found":
      return "Thread not found";
  }
}
