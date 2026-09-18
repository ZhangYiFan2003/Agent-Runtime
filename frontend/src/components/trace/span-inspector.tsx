import { useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import type { Span } from "../../api/adapters/trace";
import { formatDurationMs, formatFullTime } from "../../lib/format";
import { promotedSpanAttributes, remainingSpanAttributes } from "../../lib/run-detail-view";
import { cn } from "../../lib/utils";
import { CopyButton } from "../ui/copy-button";
import { CopyId } from "../run/copy-id";

interface SpanInspectorProps {
  span: Span | null;
  /** A `?span=` param is present but no span with that id exists. */
  notFound: boolean;
}

function InspectorEmpty({ message, hint }: { message: string; hint: string }) {
  return (
    <div className="flex flex-col items-center gap-1.5 px-4 py-12 text-center">
      <div className="text-13 font-medium text-fg-0">{message}</div>
      <div className="font-mono text-xs text-fg-2">{hint}</div>
    </div>
  );
}

function DefRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="shrink-0 text-13 text-fg-2">{label}</span>
      <span className="min-w-0 text-right font-mono text-xs break-all text-fg-0">{children}</span>
    </div>
  );
}

/** Selection-driven span inspector: identity, timing, promoted high-value
 *  attributes, and a collapsed verbatim JSON view of everything else. */
export function SpanInspector({ span, notFound }: SpanInspectorProps) {
  const [attrsOpen, setAttrsOpen] = useState(false);

  if (span === null) {
    return notFound ? (
      <InspectorEmpty message="Span not found" hint="The selected span id is not in this trace." />
    ) : (
      <InspectorEmpty message="Select a span to inspect" hint="Click a span in the timeline or trace table." />
    );
  }

  const promoted = promotedSpanAttributes(span);
  const remaining = remainingSpanAttributes(span);
  const remainingJson = JSON.stringify(remaining, null, 2);

  return (
    <div className="flex flex-col">
      <div className="border-b border-border/60 px-4 py-2.5">
        <div className="truncate font-mono text-13 text-fg-0">{span.name}</div>
        <div className="mt-1 flex items-center gap-2 font-mono text-[11px]">
          <span className="rounded-[2px] border border-border px-1 tracking-wide text-fg-2 uppercase">
            {span.spanType}
          </span>
          <span className={cn(span.status === "FAILED" ? "text-danger" : "text-fg-1")}>
            {span.status}
          </span>
        </div>
      </div>

      <div className="border-b border-border/60 px-4 py-2.5">
        <div className="flex items-baseline justify-between gap-3 py-0.5">
          <span className="shrink-0 text-13 text-fg-2">Span</span>
          <CopyId id={span.spanId} head={18} />
        </div>
        <div className="flex items-baseline justify-between gap-3 py-0.5">
          <span className="shrink-0 text-13 text-fg-2">Parent span</span>
          {span.parentSpanId !== null ? (
            <CopyId id={span.parentSpanId} head={18} />
          ) : (
            <span className="font-mono text-xs text-fg-2">—</span>
          )}
        </div>
        <DefRow label="Started">{formatFullTime(span.startedAt)}</DefRow>
        <DefRow label="Ended">
          {span.endedAt !== null ? formatFullTime(span.endedAt) : "in flight"}
        </DefRow>
        <DefRow label="Duration">{formatDurationMs(span.latencyMs)}</DefRow>
      </div>

      {promoted.length > 0 && (
        <div className="border-b border-border/60 px-4 py-2.5">
          {promoted.map((entry) => (
            <DefRow key={entry.label} label={entry.label}>
              {entry.value}
            </DefRow>
          ))}
        </div>
      )}

      <div className="px-4 py-2.5">
        <button
          type="button"
          onClick={() => setAttrsOpen((open) => !open)}
          aria-expanded={attrsOpen}
          className="flex w-full items-center gap-1.5 text-left text-[11px] font-medium tracking-wider text-fg-2 uppercase hover:text-fg-1"
        >
          <ChevronRight className={cn("size-3 transition-transform", attrsOpen && "rotate-90")} />
          Attributes ({Object.keys(remaining).length})
        </button>
        {attrsOpen && (
          <div className="relative mt-2">
            <div className="absolute top-1 right-1">
              <CopyButton text={remainingJson} label="Copy JSON" />
            </div>
            <pre className="max-h-72 overflow-auto rounded border border-border bg-bg-1 p-2 font-mono text-[11px] break-all whitespace-pre-wrap text-fg-1">
              {remainingJson}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
