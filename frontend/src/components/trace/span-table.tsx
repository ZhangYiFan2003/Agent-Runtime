import { useMemo } from "react";
import type { Span } from "../../api/adapters/trace";
import { formatDurationMs } from "../../lib/format";
import { buildSpanTree, flattenSpanTree } from "../../lib/trace-tree";
import { cn } from "../../lib/utils";

interface SpanTableProps {
  spans: Span[];
  selectedSpanId: string | null;
  onSelectSpan: (spanId: string) => void;
}

function spanStatusClass(status: string): string {
  if (status === "FAILED") return "text-danger";
  if (status === "RUNNING") return "text-accent";
  return "text-fg-1";
}

/** Trace tab — flattened span tree as a dense table; row click selects the
 *  span (same URL `span` param as the waterfall). */
export function SpanTable({ spans, selectedSpanId, onSelectSpan }: SpanTableProps) {
  const rows = useMemo(() => flattenSpanTree(buildSpanTree(spans)), [spans]);

  if (rows.length === 0) {
    return (
      <div className="px-6 py-16 text-center">
        <div className="text-sm font-medium text-fg-0">No spans recorded</div>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-border">
            {["Type", "Name", "Status", "Duration"].map((h) => (
              <th
                key={h}
                scope="col"
                className="px-3 py-2 text-[11px] font-medium tracking-wider text-fg-2 uppercase first:pl-4 last:pr-4"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ span, depth }, index) => {
            const selected = span.spanId === selectedSpanId;
            return (
              <tr
                key={`${span.spanId}:${index}`}
                tabIndex={0}
                aria-pressed={selected}
                onClick={() => onSelectSpan(span.spanId)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelectSpan(span.spanId);
                  }
                }}
                className={cn(
                  "h-9 cursor-pointer border-b border-border/60 transition-colors last:border-b-0 focus-visible:outline-none",
                  selected ? "bg-accent-dim" : "hover:bg-bg-2 focus-visible:bg-bg-3",
                )}
              >
                <td className="px-3 font-mono text-[11px] whitespace-nowrap text-fg-2 uppercase first:pl-4">
                  {span.spanType}
                </td>
                <td className="max-w-0 px-3">
                  <span
                    className={cn(
                      "block truncate font-mono text-xs",
                      selected ? "text-accent" : "text-fg-0",
                    )}
                    style={{ paddingLeft: depth * 12 }}
                  >
                    {span.name}
                  </span>
                </td>
                <td
                  className={cn(
                    "px-3 font-mono text-xs whitespace-nowrap",
                    spanStatusClass(span.status),
                  )}
                >
                  {span.status}
                </td>
                <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1 last:pr-4">
                  {span.endedAt === null && span.latencyMs === null
                    ? "…"
                    : formatDurationMs(span.latencyMs)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
