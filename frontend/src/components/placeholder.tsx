import type { LucideIcon } from "lucide-react";

/** Clean placeholder for pages that land in the next phase. */
export function PlaceholderPage({
  icon: Icon,
  title,
  phase,
}: {
  icon: LucideIcon;
  title: string;
  phase: string;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <div className="flex size-10 items-center justify-center rounded-md border border-border bg-bg-1">
        <Icon className="size-4.5 text-fg-2" />
      </div>
      <div className="text-sm font-medium text-fg-0">{title}</div>
      <div className="font-mono text-xs text-fg-2">
        Coming in next phase<span className="text-accent">_</span>
      </div>
      <div className="font-mono text-[11px] text-fg-2/70">{phase}</div>
    </div>
  );
}
