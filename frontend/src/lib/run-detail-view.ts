import type { RunView, InterruptSummary } from "../api/adapters/run";
import type { RunMetrics } from "../api/adapters/metrics";
import type { Span } from "../api/adapters/trace";
import { formatDurationMs, formatFullTime } from "./format";
import { runDurationMs } from "./runs-view";

/**
 * Pure view logic for the Run Detail page: terminal-status classification,
 * polling intervals, URL search parsing, action-bar derivation, banners,
 * output state, metrics grouping and span-attribute promotion. No React,
 * no fetching — everything here is directly unit-testable.
 */

/* ------------------------------------------------------------------ */
/* Status / polling                                                    */
/* ------------------------------------------------------------------ */

export const TERMINAL_RUN_STATUSES = ["COMPLETED", "FAILED", "CANCELLED"] as const;

export function isTerminalRunStatus(status: string): boolean {
  return (TERMINAL_RUN_STATUSES as readonly string[]).includes(status);
}

export const RUN_REFETCH_INTERVAL_MS = 2_000;
export const DETAIL_REFETCH_INTERVAL_MS = 3_000;

/**
 * Poll while the run is non-terminal; stop on terminal status and before
 * the run status is known (undefined → no polling). Used by the query
 * hooks; kept pure so the policy is testable without rendering.
 */
export function runDetailRefetchInterval(
  status: string | undefined,
  intervalMs: number,
): number | false {
  if (status === undefined || status === "") return false;
  return isTerminalRunStatus(status) ? false : intervalMs;
}

/* ------------------------------------------------------------------ */
/* URL search state                                                    */
/* ------------------------------------------------------------------ */

export interface RunDetailSearch {
  /** Selected span id, mirrored into the waterfall/table/inspector. */
  span?: string;
  /** Selected event id (persisted event feed). Mutually exclusive with span. */
  event?: number;
}

export function parseRunDetailSearch(search: Record<string, unknown>): RunDetailSearch {
  const span = search.span;
  if (typeof span === "string" && span !== "") return { span };
  const event = search.event;
  if (typeof event === "number" && Number.isInteger(event) && event >= 0) return { event };
  // Tolerate stringified event ids from hand-edited URLs.
  if (typeof event === "string" && event !== "" && /^\d+$/.test(event)) {
    return { event: Number(event) };
  }
  return {};
}

/* ------------------------------------------------------------------ */
/* Action bar (read-only in Phase 3)                                   */
/* ------------------------------------------------------------------ */

export type RunActionTone = "primary" | "danger" | "default";

export interface RunActionSpec {
  operation: string;
  label: string;
  tone: RunActionTone;
  /** False for operations the backend added after this UI shipped. */
  known: boolean;
}

const KNOWN_ACTIONS: Record<string, { label: string; tone: RunActionTone }> = {
  resume: { label: "Resume", tone: "default" },
  approve: { label: "Approve", tone: "primary" },
  reject: { label: "Reject", tone: "danger" },
  cancel: { label: "Cancel", tone: "danger" },
  interrupt: { label: "Interrupt", tone: "default" },
  requeue: { label: "Requeue", tone: "default" },
};

function humanizeOperation(operation: string): string {
  const words = operation.replace(/[_-]+/g, " ").trim();
  if (words === "") return operation;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/**
 * Buttons come from `allowed_operations` only — never from local status
 * logic. Unknown future operations degrade to a generic disabled action.
 */
export function deriveRunActions(operations: readonly string[]): RunActionSpec[] {
  return operations.map((operation) => {
    const known = KNOWN_ACTIONS[operation];
    if (known) return { operation, label: known.label, tone: known.tone, known: true };
    return { operation, label: humanizeOperation(operation), tone: "default", known: false };
  });
}

/** Pending labels replace button labels while a control mutation is in flight. */
const ACTION_PENDING_LABELS: Record<string, string> = {
  resume: "Resuming…",
  cancel: "Cancelling…",
  interrupt: "Interrupting…",
  approve: "Approving…",
  reject: "Rejecting…",
  requeue: "Requeuing…",
};

/** Pending label for a known operation, or null for unknown operations. */
export function actionPendingLabel(operation: string): string | null {
  return ACTION_PENDING_LABELS[operation] ?? null;
}

/**
 * The pending interrupt that targets the run itself — what the header
 * Approve / Reject buttons resolve. Interrupts aggregated from children are
 * excluded (those resolve from the approval banner, against the child run).
 */
export function selfPendingInterrupt(
  run: Pick<RunView, "runId" | "interrupt" | "pendingInterrupts">,
): InterruptSummary | null {
  return run.pendingInterrupts.find((entry) => entry.runId === run.runId) ?? run.interrupt;
}

/* ------------------------------------------------------------------ */
/* Waiting / recovery banners                                          */
/* ------------------------------------------------------------------ */

export interface BannerField {
  label: string;
  value: string;
}

export interface RunBanner {
  kind: "approval" | "waiting" | "recovery";
  tone: "warn" | "info";
  title: string;
  message?: string;
  fields: BannerField[];
}

export function deriveRunBanners(run: RunView): RunBanner[] {
  const banners: RunBanner[] = [];

  for (const interrupt of run.pendingInterrupts) {
    const fields: BannerField[] = [];
    if (interrupt.toolName !== null) fields.push({ label: "Tool", value: interrupt.toolName });
    fields.push({ label: "Reason", value: interrupt.reason });
    fields.push({ label: "Invocation", value: interrupt.invocationId });
    if (interrupt.runId !== run.runId) fields.push({ label: "Child run", value: interrupt.runId });
    banners.push({ kind: "approval", tone: "warn", title: "Approval required", fields });
  }

  if (run.pendingInterrupts.length === 0 && run.waitingReason !== null) {
    const waiting: Record<string, string> = {
      approval: "Waiting for approval",
      child: "Waiting for child runs",
      manual_interrupt: "Interrupted — awaiting resume",
      recovery: "Awaiting recovery",
    };
    const fields: BannerField[] = [];
    if (run.waitingReason === "child") {
      fields.push({
        label: "Children",
        value: `${run.activeChildrenCount} active · ${run.terminalChildrenCount} terminal`,
      });
    }
    if (run.interrupt !== null) {
      fields.push({ label: "Reason", value: run.interrupt.reason });
    }
    banners.push({
      kind: "waiting",
      tone: "info",
      title: waiting[run.waitingReason] ?? `Waiting: ${run.waitingReason}`,
      fields,
    });
  }

  if (run.recoveryAction === "client_resume") {
    banners.push({
      kind: "recovery",
      tone: "info",
      title: "Recovery required",
      message: "This run has no active execution handle. Resume is required for recovery.",
      fields: [],
    });
  } else if (run.recoveryAction !== null) {
    banners.push({
      kind: "recovery",
      tone: "info",
      title: "Recovery required",
      message: `Recovery action: ${run.recoveryAction}`,
      fields: [],
    });
  }

  return banners;
}

/* ------------------------------------------------------------------ */
/* Output tab                                                          */
/* ------------------------------------------------------------------ */

export type OutputState =
  | { kind: "output"; text: string }
  | { kind: "error"; errorType: string; message: string; step: string | null }
  | { kind: "empty" };

export function outputState(run: RunView): OutputState {
  if (run.status === "FAILED" && run.error !== null) {
    return { kind: "error", errorType: run.error.type, message: run.error.message, step: run.error.step };
  }
  if (run.output !== null && run.output !== "") return { kind: "output", text: run.output };
  if (run.error !== null) {
    return { kind: "error", errorType: run.error.type, message: run.error.message, step: run.error.step };
  }
  return { kind: "empty" };
}

/* ------------------------------------------------------------------ */
/* Metric / value formatting                                           */
/* ------------------------------------------------------------------ */

/** 8200 → "8.2k", 1_500_000 → "1.5M", 0 → "0", null → "—". */
export function formatCompactNumber(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const format = (scaled: number, suffix: string) => {
    const fixed = scaled.toFixed(1);
    return `${fixed.endsWith(".0") ? fixed.slice(0, -2) : fixed}${suffix}`;
  };
  if (abs >= 1_000_000) return format(value / 1_000_000, "M");
  if (abs >= 10_000) return `${Math.round(value / 1_000)}k`;
  if (abs >= 1_000) return format(value / 1_000, "k");
  return String(value);
}

/** 0.924 → "92.4%", null → "—". */
export function formatPercent(ratio: number | null): string {
  if (ratio === null || !Number.isFinite(ratio)) return "—";
  return `${(ratio * 100).toFixed(1)}%`;
}

export function formatCount(value: number | null): string {
  if (value === null) return "—";
  return String(value);
}

export function formatYesNo(value: boolean | null): string {
  if (value === null) return "—";
  return value ? "yes" : "no";
}

/* ------------------------------------------------------------------ */
/* Metrics tab grouping                                                */
/* ------------------------------------------------------------------ */

export interface MetricEntry {
  label: string;
  value: string;
}

export interface MetricGroup {
  title: string;
  entries: MetricEntry[];
}

function recordEntries(
  record: Record<string, number>,
  format: (value: number) => string,
): MetricEntry[] {
  return Object.entries(record).map(([key, value]) => ({ label: key, value: format(value) }));
}

/** Group the flat RunMetrics into definition-list sections. Groups with no
 *  meaningful entries (e.g. an empty budget record set) are dropped. */
export function groupMetrics(metrics: RunMetrics): MetricGroup[] {
  const budget: MetricEntry[] = [
    ...recordEntries(metrics.budgetUtilization, formatPercent).map((entry) => ({
      ...entry,
      label: `utilization · ${entry.label}`,
    })),
    ...recordEntries(metrics.aggregateBudgetUtilization, formatPercent).map((entry) => ({
      ...entry,
      label: `aggregate utilization · ${entry.label}`,
    })),
    { label: "Soft limit reached", value: formatYesNo(metrics.budgetSoftLimitReached) },
    { label: "Hard limit reached", value: formatYesNo(metrics.budgetHardLimitReached) },
    { label: "Exceeded dimension", value: metrics.budgetExceededDimension ?? "—" },
  ];

  const groups: MetricGroup[] = [
    {
      title: "Execution",
      entries: [
        { label: "Duration", value: formatDurationMs(metrics.durationMs) },
        { label: "Steps", value: String(metrics.stepCount) },
        {
          label: "Elapsed",
          value: metrics.elapsedSeconds === null ? "—" : formatDurationMs(metrics.elapsedSeconds * 1000),
        },
        { label: "Checkpoints", value: String(metrics.checkpointCount) },
        { label: "Interrupts", value: String(metrics.interruptCount) },
        { label: "Resumes", value: String(metrics.resumeCount) },
      ],
    },
    {
      title: "Model",
      entries: [
        { label: "LLM calls", value: String(metrics.llmCalls) },
        { label: "Prompt tokens", value: formatCompactNumber(metrics.promptTokens) },
        { label: "Completion tokens", value: formatCompactNumber(metrics.completionTokens) },
        { label: "Total tokens", value: formatCompactNumber(metrics.totalTokens) },
        { label: "Cached input tokens", value: formatCompactNumber(metrics.cachedInputTokens) },
        { label: "Reasoning tokens", value: formatCompactNumber(metrics.reasoningTokens) },
        {
          label: "Cost",
          value: metrics.costKnown && metrics.costUsd !== null ? `$${metrics.costUsd.toFixed(4)}` : "—",
        },
      ],
    },
    {
      title: "Tools",
      entries: [
        { label: "Tool calls", value: String(metrics.toolCalls) },
        { label: "Successes", value: String(metrics.toolSuccesses) },
        { label: "Failures", value: String(metrics.toolFailures) },
        { label: "Success rate", value: formatPercent(metrics.toolSuccessRate) },
      ],
    },
    {
      title: "Retry",
      entries: [
        { label: "Retries", value: String(metrics.retryCount) },
        { label: "Retried model calls", value: formatCount(metrics.retriedModelCalls) },
        { label: "Retried tool calls", value: formatCount(metrics.retriedToolCalls) },
        { label: "Dependency timeouts", value: formatCount(metrics.dependencyTimeouts) },
        { label: "Rate limit failures", value: formatCount(metrics.rateLimitFailures) },
        { label: "Retry exhausted", value: formatCount(metrics.retryExhaustedCount) },
        { label: "Backoff", value: formatDurationMs(metrics.retryBackoffMs) },
      ],
    },
    { title: "Budget", entries: budget },
    {
      title: "Progress",
      entries: [
        { label: "No-progress detected", value: formatYesNo(metrics.progressDetected) },
        { label: "Detector", value: metrics.progressDetectorType ?? "—" },
        { label: "Action repeats", value: String(metrics.progressActionRepeatCount) },
        { label: "Error repeats", value: String(metrics.progressErrorRepeatCount) },
        { label: "Stagnant steps", value: String(metrics.progressStagnantSteps) },
        { label: "Cycle length", value: formatCount(metrics.progressCycleLength) },
        { label: "Recovery attempts", value: String(metrics.progressRecoveryAttempts) },
        { label: "Last progress step", value: String(metrics.progressLastProgressStep) },
      ],
    },
    {
      title: "Verification",
      entries: [
        { label: "Status", value: metrics.verificationStatus },
        { label: "Verified", value: formatYesNo(metrics.completionVerified) },
        { label: "Attempts", value: String(metrics.verificationAttempts) },
        {
          label: "Failed checks",
          value: metrics.verificationFailedChecks.length > 0 ? metrics.verificationFailedChecks.join(", ") : "—",
        },
      ],
    },
  ];
  return groups;
}

/* ------------------------------------------------------------------ */
/* Overview tab                                                        */
/* ------------------------------------------------------------------ */

export interface DefRow {
  label: string;
  value: string;
}

export interface DefSection {
  title: string;
  rows: DefRow[];
}

/** Definition-row sections from fields that actually exist; metrics rows
 *  appear only when metrics are available (404 → omitted, not "—"). */
export function buildOverviewSections(run: RunView, metrics: RunMetrics | null): DefSection[] {
  const lifecycle: DefRow[] = [
    { label: "Status", value: run.status },
    { label: "Created", value: formatFullTime(run.createdAt) },
    { label: "Started", value: formatFullTime(run.startedAt) },
  ];
  if (run.completedAt !== null) {
    lifecycle.push({ label: "Completed", value: formatFullTime(run.completedAt) });
  }
  lifecycle.push({ label: "Duration", value: formatDurationMs(runDurationMs(run)) });

  const execution: DefRow[] = [
    { label: "Strategy", value: run.executionStrategy },
    { label: "Run kind", value: run.runKind },
  ];
  if (metrics !== null) {
    execution.push(
      { label: "Steps", value: String(metrics.stepCount) },
      { label: "LLM calls", value: String(metrics.llmCalls) },
      { label: "Tool calls", value: String(metrics.toolCalls) },
      { label: "Total tokens", value: formatCompactNumber(metrics.totalTokens) },
      { label: "Retries", value: String(metrics.retryCount) },
    );
  }

  const observability: DefRow[] = [{ label: "Thread", value: run.threadId }];
  if (run.turnId !== null) observability.push({ label: "Turn", value: run.turnId });
  if (metrics !== null) observability.push({ label: "Trace", value: metrics.traceId });

  const recovery: DefRow[] = [];
  if (run.waitingReason !== null) recovery.push({ label: "Waiting", value: run.waitingReason });
  if (run.recoveryAction !== null) recovery.push({ label: "Recovery action", value: run.recoveryAction });
  if (run.pendingInterrupts.length > 0) {
    recovery.push({ label: "Pending interrupts", value: String(run.pendingInterrupts.length) });
  }
  if (run.childrenCount > 0) {
    recovery.push({
      label: "Children",
      value: `${run.childrenCount} total · ${run.activeChildrenCount} active · ${run.terminalChildrenCount} terminal`,
    });
  }

  const sections: DefSection[] = [
    { title: "Lifecycle", rows: lifecycle },
    { title: "Execution", rows: execution },
    { title: "Observability", rows: observability },
  ];
  if (recovery.length > 0) sections.push({ title: "Recovery", rows: recovery });
  return sections;
}

/* ------------------------------------------------------------------ */
/* Span inspector attribute promotion                                  */
/* ------------------------------------------------------------------ */

export interface SpanAttributeEntry {
  label: string;
  value: string;
}

interface Promotion {
  key: string;
  label: string;
  format?: (value: number) => string;
}

/** High-value attributes verified against runtime instrumentation
 *  (durable.py / plan_strategy.py / multi_agent_strategy.py). Promoted only
 *  when present with a primitive value; everything else stays in the raw
 *  attribute viewer. */
const PROMOTED_ATTRIBUTES: Promotion[] = [
  { key: "provider", label: "Provider" },
  { key: "model", label: "Model" },
  { key: "temperature", label: "Temperature" },
  { key: "prompt_tokens", label: "Prompt tokens", format: formatCompactNumber },
  { key: "completion_tokens", label: "Completion tokens", format: formatCompactNumber },
  { key: "total_tokens", label: "Total tokens", format: formatCompactNumber },
  { key: "cached_input_tokens", label: "Cached tokens", format: formatCompactNumber },
  { key: "reasoning_tokens", label: "Reasoning tokens", format: formatCompactNumber },
  { key: "ttft_ms", label: "TTFT", format: formatDurationMs },
  { key: "latency_ms", label: "Latency", format: formatDurationMs },
  { key: "retry_count", label: "Retries" },
  { key: "attempt", label: "Attempt" },
  { key: "step_index", label: "Step" },
  { key: "tool_call_id", label: "Tool call" },
  { key: "approval_required", label: "Approval required" },
  { key: "permission_action", label: "Permission" },
  { key: "finish_reason", label: "Finish reason" },
  { key: "error", label: "Error" },
];

function primitiveToString(value: unknown, format?: (value: number) => string): string | null {
  if (typeof value === "string") return value === "" ? null : value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    return format ? format(value) : String(value);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  return null;
}

function promotedEntries(span: Span): Array<SpanAttributeEntry & { key: string }> {
  const entries: Array<SpanAttributeEntry & { key: string }> = [];
  for (const promotion of PROMOTED_ATTRIBUTES) {
    if (!(promotion.key in span.attributes)) continue;
    const value = primitiveToString(span.attributes[promotion.key], promotion.format);
    if (value !== null) entries.push({ key: promotion.key, label: promotion.label, value });
  }
  return entries;
}

export function promotedSpanAttributes(span: Span): SpanAttributeEntry[] {
  return promotedEntries(span).map(({ key: _key, ...entry }) => entry);
}

/** Attributes not promoted — rendered verbatim in the JSON viewer. */
export function remainingSpanAttributes(span: Span): Record<string, unknown> {
  const promotedKeys = new Set(promotedEntries(span).map((entry) => entry.key));
  const remaining: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(span.attributes)) {
    if (!promotedKeys.has(key)) remaining[key] = value;
  }
  return remaining;
}
