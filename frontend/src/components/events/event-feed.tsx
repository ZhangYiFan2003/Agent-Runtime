import { ArrowDown } from "lucide-react";
import type { RuntimeEvent } from "../../api/adapters/event";
import {
  replayStatusLabel,
  type ReplayStatus,
} from "../../lib/events-view";
import { useFollowPin } from "../../lib/use-follow-pin";
import { Button } from "../ui/button";
import { EventRow } from "./event-row";

/**
 * Persisted-event feed for the Run Detail Events tab (desktop + mobile).
 * Plain dense rows with hairline separators — no virtualization, no cards.
 *
 * Follow behavior: while the user is near the bottom, new events keep the
 * list pinned to the latest; when the user scrolls up they are not yanked
 * back — a small "Jump to latest" button appears instead.
 */
export function EventFeed({
  events,
  status,
  runStatus,
  selectedEventId,
  onSelectEvent,
}: {
  events: RuntimeEvent[];
  status: ReplayStatus;
  runStatus: string | undefined;
  selectedEventId: number | null;
  onSelectEvent: (eventId: number) => void;
}) {
  const lastEventId = events.length > 0 ? events[events.length - 1].eventId : null;
  const { scrollRef, showJump, handleScroll, jumpToLatest } = useFollowPin(lastEventId);

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      <div className="flex h-7 shrink-0 items-center justify-between border-b border-border/60 px-3">
        <span className="font-mono text-[11px] text-fg-2">
          {replayStatusLabel(status, runStatus)}
        </span>
        <span className="font-mono text-[11px] text-fg-2 tabular-nums">
          {events.length} events
        </span>
      </div>

      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="min-h-0 flex-1 overflow-y-auto"
      >
        {events.length === 0 ? (
          <div className="px-6 py-16 text-center">
            <div className="text-sm font-medium text-fg-0">No events yet</div>
            <div className="mt-1 font-mono text-xs text-fg-2">
              {status === "connecting"
                ? "Loading persisted events…"
                : "Events appear here once the run records them."}
            </div>
          </div>
        ) : (
          <div className="divide-y divide-border/60">
            {events.map((event) => (
              <EventRow
                key={event.eventId}
                event={event}
                selected={event.eventId === selectedEventId}
                onSelect={onSelectEvent}
              />
            ))}
          </div>
        )}
      </div>

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
