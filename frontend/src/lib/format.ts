/**
 * Shared formatting utilities. Components must not scatter `new Date(...)`
 * arithmetic — everything time-related goes through here. Pure functions,
 * `now` is injectable for tests.
 */

/** 180 ms · 1.8 s · 1m 24s · 1h 02m */
export function formatDurationMs(ms: number | null): string {
  if (ms === null || !Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) {
    const rounded = seconds < 10 ? seconds.toFixed(1) : String(Math.round(seconds));
    return `${rounded} s`;
  }
  const totalMinutes = Math.floor(seconds / 60);
  const restSeconds = Math.round(seconds % 60);
  if (totalMinutes < 60) return `${totalMinutes}m ${String(restSeconds).padStart(2, "0")}s`;
  const hours = Math.floor(totalMinutes / 60);
  const restMinutes = totalMinutes % 60;
  return `${hours}h ${String(restMinutes).padStart(2, "0")}m`;
}

/** "3s ago" · "2m ago" · "14:32" (today) · "Sep 17" (older) */
export function formatRelativeTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const diffMs = now - then;
  if (diffMs < 0) return formatClock(then);
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const thenDate = new Date(then);
  const nowDate = new Date(now);
  if (isSameDay(thenDate, nowDate)) return formatClock(then);
  return thenDate.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function formatFullTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return iso;
  return new Date(then).toLocaleString("en-US", { hour12: false });
}

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function formatClock(then: number): string {
  const d = new Date(then);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** "14:32:07.183" — event-feed timestamps. */
export function formatEventTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const d = new Date(then);
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

/** "run_a8312c9d4e5…" — IDs stay mono and truncated; full value lives in tooltip/copy. */
export function truncateId(id: string, head = 14): string {
  return id.length > head + 1 ? `${id.slice(0, head)}…` : id;
}
