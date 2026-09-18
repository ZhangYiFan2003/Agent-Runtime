import { cn } from "../../lib/utils";

type Tone = "accent" | "warn" | "danger" | "info" | "muted";

const TONE_CLASSES: Record<Tone, string> = {
  accent: "bg-accent",
  warn: "bg-warn",
  danger: "bg-danger",
  info: "bg-info",
  muted: "bg-fg-2",
};

interface StatusDotProps {
  tone?: Tone;
  /** Slow pulse — reserved for RUNNING / in-flight states. */
  pulse?: boolean;
  className?: string;
}

/** 10px status dot; the one allowed flicker of cyberpunk. */
export function StatusDot({ tone = "muted", pulse = false, className }: StatusDotProps) {
  return (
    <span className={cn("relative inline-flex size-2.5 shrink-0", className)} aria-hidden>
      {pulse && (
        <span
          className={cn(
            "absolute inline-flex size-full animate-ping rounded-full opacity-40",
            TONE_CLASSES[tone],
          )}
        />
      )}
      <span className={cn("relative inline-flex size-2.5 rounded-full", TONE_CLASSES[tone])} />
    </span>
  );
}
