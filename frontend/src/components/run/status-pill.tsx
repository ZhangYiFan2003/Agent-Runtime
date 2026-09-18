import { Check } from "lucide-react";
import { cn } from "../../lib/utils";
import type { RunStatusGroup } from "../../api/adapters/run";
import { StatusDot } from "../ui/status-dot";

type DotTone = "accent" | "warn" | "danger" | "info" | "muted";

interface StatusStyle {
  tone: DotTone;
  label: string;
  pulse?: boolean;
}

/**
 * Single status mapping for the whole app (plan §I). Unknown statuses
 * render neutrally via their adapter-classified group — never crash,
 * never invent per-status colors beyond the known set.
 */
const STATUS_STYLES: Record<string, StatusStyle> = {
  RUNNING: { tone: "accent", label: "RUNNING", pulse: true },
  WAITING_APPROVAL: { tone: "warn", label: "WAITING APPROVAL" },
  WAITING_CHILD: { tone: "info", label: "WAITING CHILD" },
  INTERRUPTED: { tone: "info", label: "INTERRUPTED" },
  COMPLETED: { tone: "muted", label: "COMPLETED" },
  FAILED: { tone: "danger", label: "FAILED" },
  CANCELLED: { tone: "muted", label: "CANCELLED" },
};

const GROUP_FALLBACK: Record<RunStatusGroup, StatusStyle> = {
  active: { tone: "accent", label: "", pulse: true },
  waiting: { tone: "warn", label: "" },
  failure: { tone: "danger", label: "" },
  idle: { tone: "info", label: "" },
  success: { tone: "muted", label: "" },
  unknown: { tone: "muted", label: "" },
};

const TONE_TEXT: Record<DotTone, string> = {
  accent: "text-accent",
  warn: "text-warn",
  danger: "text-danger",
  info: "text-info",
  muted: "text-fg-1",
};

export function StatusPill({
  status,
  statusGroup,
}: {
  status: string;
  statusGroup: RunStatusGroup;
}) {
  const known = STATUS_STYLES[status];
  const fallback = GROUP_FALLBACK[statusGroup];
  const tone = known?.tone ?? fallback.tone;
  const pulse = known?.pulse ?? fallback.pulse ?? false;
  const label = known?.label ?? (status || "UNKNOWN");
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 font-mono text-[11px] tracking-wide",
        TONE_TEXT[tone],
      )}
      aria-label={`status ${status || "UNKNOWN"}`}
    >
      <StatusDot tone={tone} pulse={pulse} />
      <span className="whitespace-nowrap">{label}</span>
      {status === "COMPLETED" && <Check className="size-3 text-accent" aria-hidden />}
    </span>
  );
}
