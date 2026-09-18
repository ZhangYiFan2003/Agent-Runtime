import { useMemo } from "react";
import type { Span } from "../../api/adapters/trace";
import { formatDurationMs } from "../../lib/format";
import { computeTraceWindow, spanBarGeometry } from "../../lib/timeline";
import { buildSpanTree, flattenSpanTree } from "../../lib/trace-tree";
import { cn } from "../../lib/utils";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

interface TraceWaterfallProps {
  spans: Span[];
  selectedSpanId: string | null;
  onSelectSpan: (spanId: string) => void;
}

/**
 * Trace waterfall — plain divs/CSS, no chart library. Left: indented span
 * labels; right: proportional latency bars inside the trace window.
 * Selected = accent, failed = danger, open span = pulsing bar.
 */
export function TraceWaterfall({ spans, selectedSpanId, onSelectSpan }: TraceWaterfallProps) {
  const rows = useMemo(() => flattenSpanTree(buildSpanTree(spans)), [spans]);
  // `now` only shifts open (in-flight) span ends; recomputed when spans change.
  const window = useMemo(() => computeTraceWindow(spans, Date.now()), [spans]);

  if (rows.length === 0) {
    return (
      <div className="px-4 py-8 text-center font-mono text-xs text-fg-2">No spans recorded</div>
    );
  }

  return (
    <div className="flex flex-col">
      {window !== null && (
        <div className="flex h-6 shrink-0 items-center justify-between border-b border-border/60 px-2 font-mono text-[10px] text-fg-2">
          <span>0 ms</span>
          <span>{formatDurationMs(window.durationMs)}</span>
        </div>
      )}
      <div className="flex flex-col py-1">
        {rows.map(({ span, depth }, index) => {
          const bar = window !== null ? spanBarGeometry(span, window, Date.now()) : null;
          const selected = span.spanId === selectedSpanId;
          const failed = span.status === "FAILED";
          return (
            <Tooltip key={`${span.spanId}:${index}`}>
              <TooltipTrigger asChild>
                <button
                  type="button"
                  onClick={() => onSelectSpan(span.spanId)}
                  aria-pressed={selected}
                  aria-label={`Span ${span.name}, type ${span.spanType}, status ${span.status}`}
                  className={cn(
                    "flex h-7 w-full items-center gap-2 px-2 text-left transition-colors focus-visible:outline-none",
                    selected ? "bg-accent-dim" : "hover:bg-bg-2 focus-visible:bg-bg-3",
                  )}
                >
                  <span
                    className="flex min-w-0 flex-1 items-center gap-1.5"
                    style={{ paddingLeft: depth * 12 }}
                  >
                    <span
                      className={cn(
                        "shrink-0 rounded-[2px] border border-border px-1 font-mono text-[9px] tracking-wide uppercase",
                        failed ? "border-danger/40 text-danger" : "text-fg-2",
                      )}
                    >
                      {span.spanType}
                    </span>
                    <span
                      className={cn(
                        "truncate font-mono text-xs",
                        selected ? "text-accent" : failed ? "text-danger" : "text-fg-1",
                      )}
                    >
                      {span.name}
                    </span>
                  </span>
                  <span className="relative h-3.5 w-28 shrink-0">
                    {bar !== null && (
                      <span
                        className={cn(
                          "absolute top-1/2 h-2 -translate-y-1/2 rounded-[2px]",
                          selected
                            ? "bg-accent"
                            : failed
                              ? "bg-danger/70"
                              : "bg-fg-2/50",
                          bar.openEnded && "animate-pulse",
                        )}
                        style={{
                          left: `${bar.leftPct}%`,
                          width: `max(${bar.widthPct}%, 3px)`,
                        }}
                      />
                    )}
                  </span>
                </button>
              </TooltipTrigger>
              <TooltipContent side="right" className="font-mono text-[11px]">
                {span.name} · {formatDurationMs(span.latencyMs)}
                {bar?.openEnded ? " · in flight" : ""}
              </TooltipContent>
            </Tooltip>
          );
        })}
      </div>
    </div>
  );
}
