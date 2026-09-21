import { useState } from "react";
import { ArrowDown, ChevronRight, Loader2 } from "lucide-react";
import type { WorkbenchItem } from "../../lib/workbench-projection";
import { replayStatusLabel, type ReplayStatus } from "../../lib/events-view";
import { formatDurationMs, formatEventTime } from "../../lib/format";
import { useFollowPin } from "../../lib/use-follow-pin";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";

/**
 * Workbench conversation/execution feed. Developer-tool rows over the
 * projected thread events — no chat bubbles, no avatars. Follow-latest
 * scrolling matches the Run Detail event feed (shared `useFollowPin`).
 */

function Timestamp({ iso }: { iso: string | null }) {
  if (iso === null) return null;
  return <span className="shrink-0 font-mono text-[10px] text-fg-2">{formatEventTime(iso)}</span>;
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  if (value === undefined) return null;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div className="mt-1.5">
      <div className="font-mono text-[10px] tracking-wider text-fg-2 uppercase">{label}</div>
      <pre className="mt-0.5 max-h-56 overflow-auto rounded border border-border/60 bg-bg-0 p-2 font-mono text-[11px] break-all whitespace-pre-wrap text-fg-1">
        {text}
      </pre>
    </div>
  );
}

function ToolRow({ item }: { item: Extract<WorkbenchItem, { kind: "tool" }> }) {
  const [expanded, setExpanded] = useState(false);
  const hasDetails = item.input !== undefined || item.result !== undefined;
  const statusLabel =
    item.status === "running" ? "running" : item.status === "error" ? "failed" : "completed";
  const statusTone =
    item.status === "running"
      ? "text-accent"
      : item.status === "error"
        ? "text-danger"
        : "text-fg-2";

  return (
    <div className="px-4 py-1.5 md:px-6">
      <button
        type="button"
        disabled={!hasDetails}
        onClick={() => setExpanded((open) => !open)}
        className="flex w-full min-w-0 items-center gap-1.5 text-left font-mono text-xs focus-visible:outline-none"
        aria-expanded={expanded}
      >
        <ChevronRight
          className={cn(
            "size-3 shrink-0 text-fg-2 transition-transform",
            expanded && "rotate-90",
            !hasDetails && "invisible",
          )}
        />
        <span className="text-fg-2">Tool</span>
        <span className="text-fg-2">·</span>
        <span className="truncate text-fg-0">{item.name}</span>
        <span className="text-fg-2">—</span>
        <span className={cn("inline-flex shrink-0 items-center gap-1", statusTone)}>
          {item.status === "running" && <Loader2 className="size-3 animate-spin" />}
          {statusLabel}
        </span>
        {item.durationMs !== null && (
          <span className="shrink-0 text-fg-2">· {formatDurationMs(item.durationMs)}</span>
        )}
        {item.reused && <span className="shrink-0 text-fg-2">· reused</span>}
      </button>
      {expanded && hasDetails && (
        <div className="ml-7">
          <JsonBlock label="input" value={item.input} />
          <JsonBlock label="result" value={item.result} />
        </div>
      )}
    </div>
  );
}

const LIFECYCLE_TONE: Record<string, string> = {
  neutral: "text-fg-2",
  accent: "text-accent",
  danger: "text-danger",
  info: "text-info",
  warn: "text-warn",
};

function FeedRow({ item }: { item: WorkbenchItem }) {
  switch (item.kind) {
    case "user":
      return (
        <div className="px-4 py-2 md:px-6">
          <div className="flex items-baseline gap-2">
            <span className="shrink-0 font-mono text-[10px] tracking-wider text-accent uppercase">
              you
            </span>
            <Timestamp iso={item.timestamp} />
            {item.pending && (
              <span className="font-mono text-[10px] text-fg-2">submitting…</span>
            )}
          </div>
          <div
            className={cn(
              "mt-0.5 text-13 break-words whitespace-pre-wrap",
              item.pending ? "text-fg-1 opacity-70" : "text-fg-0",
            )}
          >
            {item.text}
          </div>
        </div>
      );
    case "assistant":
      return (
        <div className="px-4 py-2 md:px-6">
          <div className="flex items-baseline gap-2">
            <span className="shrink-0 font-mono text-[10px] tracking-wider text-fg-2 uppercase">
              axiom
            </span>
            <Timestamp iso={item.timestamp} />
          </div>
          <div className="mt-0.5 text-13 break-words whitespace-pre-wrap text-fg-1">
            {item.text}
          </div>
        </div>
      );
    case "tool":
      return <ToolRow item={item} />;
    case "lifecycle":
      return (
        <div className="flex items-center gap-3 px-4 py-1 md:px-6" role="separator">
          <span className="h-px min-w-4 flex-1 bg-border/60" aria-hidden />
          <span className={cn("font-mono text-[11px]", LIFECYCLE_TONE[item.tone])}>
            {item.label}
          </span>
          <span className="h-px min-w-4 flex-1 bg-border/60" aria-hidden />
        </div>
      );
    case "approval":
      return (
        <div className="border-l-2 border-l-warn bg-warn/5 px-4 py-1.5 md:px-6" role="status">
          <div className="text-xs font-medium text-warn">
            Approval requested
            {item.toolName !== null && (
              <span className="ml-2 font-mono text-[11px] text-fg-1">{item.toolName}</span>
            )}
          </div>
          {item.reason !== null && <div className="mt-0.5 text-xs text-fg-1">{item.reason}</div>}
          <div className="mt-0.5 font-mono text-[10px] text-fg-2">
            resolve from the run context panel
          </div>
        </div>
      );
    case "error":
      return (
        <div className="border-l-2 border-l-danger bg-danger/5 px-4 py-1.5 md:px-6" role="alert">
          <div className="text-xs text-danger">{item.message}</div>
        </div>
      );
    case "other":
      return (
        <div className="flex items-baseline gap-2 px-4 py-1 md:px-6">
          <span className="shrink-0 font-mono text-[10px] text-fg-2">{item.eventType}</span>
          {item.summary !== "" && (
            <span className="min-w-0 truncate font-mono text-[11px] text-fg-2">{item.summary}</span>
          )}
          <Timestamp iso={item.timestamp} />
        </div>
      );
  }
}

export function WorkbenchFeed({
  items,
  status,
  replayError,
}: {
  items: WorkbenchItem[];
  status: ReplayStatus;
  replayError: string | null;
}) {
  const lastId = items.length > 0 ? items[items.length - 1].id : null;
  const { scrollRef, showJump, handleScroll, jumpToLatest } = useFollowPin(lastId);

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div className="flex h-7 shrink-0 items-center justify-between border-b border-border/60 px-3">
        <span className="font-mono text-[11px] text-fg-2">{replayStatusLabel(status, "")}</span>
        <span className="font-mono text-[11px] text-fg-2 tabular-nums">
          {items.length} entries
        </span>
      </div>

      <div ref={scrollRef} onScroll={handleScroll} className="min-h-0 flex-1 overflow-y-auto">
        {items.length === 0 ? (
          <div className="px-6 py-16 text-center">
            <div className="text-sm font-medium text-fg-0">No activity yet</div>
            <div className="mt-1 font-mono text-xs text-fg-2">
              {status === "connecting"
                ? "Loading persisted events…"
                : status === "not-found"
                  ? "This thread does not exist on the connected runtime."
                  : "Send a message below to start a turn."}
            </div>
          </div>
        ) : (
          <div className="divide-y divide-border/40 py-2">{items.map((item) => <FeedRow key={item.id} item={item} />)}</div>
        )}
      </div>

      {replayError !== null && status !== "following" && (
        <div className="shrink-0 border-t border-border/60 px-3 py-1 font-mono text-[11px] text-fg-2">
          {replayError}
        </div>
      )}

      {showJump && (
        <div className="absolute bottom-3 left-1/2 -translate-x-1/2">
          <Button variant="default" size="sm" onClick={jumpToLatest}>
            <ArrowDown />
            Jump to latest
          </Button>
        </div>
      )}
    </div>
  );
}
