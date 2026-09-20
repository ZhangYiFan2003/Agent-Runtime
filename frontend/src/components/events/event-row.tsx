import type { RuntimeEvent } from "../../api/adapters/event";
import { eventCategory, summarizeEvent } from "../../lib/events-view";
import { formatEventTime } from "../../lib/format";
import { cn } from "../../lib/utils";

/**
 * Dense persisted-event row: mono timestamp · category chip · event_type ·
 * one-line payload summary. Hairline separators come from the parent list;
 * no cards, no avatars, no grouping.
 */
export function EventRow({
  event,
  selected,
  onSelect,
}: {
  event: RuntimeEvent;
  selected: boolean;
  onSelect: (eventId: number) => void;
}) {
  const category = eventCategory(event.eventType);
  const summary = summarizeEvent(event);

  return (
    <button
      type="button"
      onClick={() => onSelect(event.eventId)}
      aria-pressed={selected}
      aria-label={`Event ${event.eventType} ${event.eventId}`}
      className={cn(
        "flex w-full items-baseline gap-2 px-3 py-1.5 text-left transition-colors",
        selected ? "bg-bg-3" : "hover:bg-bg-2",
      )}
    >
      <span className="shrink-0 font-mono text-[11px] text-fg-2 tabular-nums">
        {formatEventTime(event.timestamp)}
      </span>
      <span
        className={cn(
          "shrink-0 rounded-[2px] border px-1 font-mono text-[10px] tracking-wide uppercase",
          category === "ERROR"
            ? "border-danger/50 text-danger"
            : "border-border text-fg-2",
        )}
      >
        {category}
      </span>
      <span
        className={cn(
          "shrink-0 font-mono text-xs break-all",
          category === "ERROR" ? "text-danger" : "text-fg-0",
        )}
      >
        {event.eventType}
      </span>
      {summary !== "" && (
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg-1">
          {summary}
        </span>
      )}
    </button>
  );
}
