import { useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import type { RuntimeEvent } from "../../api/adapters/event";
import { eventCategory } from "../../lib/events-view";
import { formatFullTime } from "../../lib/format";
import { cn } from "../../lib/utils";
import { CopyButton } from "../ui/copy-button";
import { CopyId } from "../run/copy-id";

interface EventInspectorProps {
  event: RuntimeEvent | null;
  /** A `?event=` param is present but no event with that id is loaded. */
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

function IdRow({ label, id }: { label: string; id: string | null }) {
  if (id === null) return null;
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <span className="shrink-0 text-13 text-fg-2">{label}</span>
      <CopyId id={id} head={16} />
    </div>
  );
}

/** Selection-driven persisted-event inspector: envelope fields (ids only
 *  when present) plus a collapsed verbatim JSON view of the payload. */
export function EventInspector({ event, notFound }: EventInspectorProps) {
  const [payloadOpen, setPayloadOpen] = useState(false);

  if (event === null) {
    return notFound ? (
      <InspectorEmpty message="Event not found" hint="The selected event id is not in the replayed feed." />
    ) : (
      <InspectorEmpty message="Select an event to inspect" hint="Click a row in the events feed." />
    );
  }

  const category = eventCategory(event.eventType);
  const payloadJson = JSON.stringify(event.payload, null, 2);

  return (
    <div className="flex flex-col">
      <div className="border-b border-border/60 px-4 py-2.5">
        <div className="truncate font-mono text-13 text-fg-0">{event.eventType}</div>
        <div className="mt-1 flex items-center gap-2 font-mono text-[11px]">
          <span
            className={cn(
              "rounded-[2px] border px-1 tracking-wide uppercase",
              category === "ERROR" ? "border-danger/50 text-danger" : "border-border text-fg-2",
            )}
          >
            {category}
          </span>
          <span className="text-fg-1">event {event.eventId}</span>
        </div>
      </div>

      <div className="border-b border-border/60 px-4 py-2.5">
        <div className="flex items-baseline justify-between gap-3 py-0.5">
          <span className="shrink-0 text-13 text-fg-2">Event</span>
          <CopyId id={String(event.eventId)} head={18} />
        </div>
        <DefRow label="Timestamp">{formatFullTime(event.timestamp)}</DefRow>
        <IdRow label="Thread" id={event.threadId} />
        <IdRow label="Turn" id={event.turnId} />
        <IdRow label="Run" id={event.runId} />
        <IdRow label="Parent run" id={event.parentRunId} />
        <IdRow label="Parent step" id={event.parentStepId} />
        <IdRow label="Assignment" id={event.assignmentId} />
      </div>

      <div className="px-4 py-2.5">
        <button
          type="button"
          onClick={() => setPayloadOpen((open) => !open)}
          aria-expanded={payloadOpen}
          className="flex w-full items-center gap-1.5 text-left text-[11px] font-medium tracking-wider text-fg-2 uppercase hover:text-fg-1"
        >
          <ChevronRight className={cn("size-3 transition-transform", payloadOpen && "rotate-90")} />
          Payload ({Object.keys(event.payload).length})
        </button>
        {payloadOpen && (
          <div className="relative mt-2">
            <div className="absolute top-1 right-1">
              <CopyButton text={payloadJson} label="Copy JSON" />
            </div>
            <pre className="max-h-72 overflow-auto rounded border border-border bg-bg-1 p-2 font-mono text-[11px] break-all whitespace-pre-wrap text-fg-1">
              {payloadJson}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
