import { Link, useRouterState } from "@tanstack/react-router";
import { Boxes, ListTree, Settings as SettingsIcon } from "lucide-react";
import { cn } from "../lib/utils";
import { useHealth } from "../queries/use-health";
import { StatusDot } from "./ui/status-dot";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";

const NAV_ITEMS = [
  { to: "/", label: "Workbench", icon: Boxes },
  { to: "/runs", label: "Runs", icon: ListTree },
  { to: "/settings", label: "Settings", icon: SettingsIcon },
] as const;

function ConnectionStatus() {
  const health = useHealth();
  if (health.isPending) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 text-xs text-fg-2">
        <StatusDot tone="muted" />
        <span className="truncate">Runtime · connecting…</span>
      </div>
    );
  }
  if (health.isError) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 text-xs text-fg-2">
        <StatusDot tone="danger" />
        <span className="truncate">Runtime · disconnected</span>
      </div>
    );
  }
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div className="flex items-center gap-2 px-3 py-2 text-xs text-fg-2">
          <StatusDot tone="accent" />
          <span className="truncate">
            Runtime · {health.data.storageBackend}
          </span>
        </div>
      </TooltipTrigger>
      <TooltipContent side="right">
        {health.data.storageBackend} · {health.data.workers} workers
      </TooltipContent>
    </Tooltip>
  );
}

export function AppSidebar() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return (
    <aside className="hidden w-58 shrink-0 flex-col border-r border-border bg-bg-1 md:flex">
      <div className="flex h-13 items-center gap-2 border-b border-border px-4">
        <span className="font-mono text-sm font-semibold tracking-widest text-fg-0">
          AXIOM
        </span>
        <span className="size-1 rounded-full bg-accent" aria-hidden />
        <span className="font-mono text-[10px] text-fg-2">runtime</span>
      </div>
      <nav className="flex flex-col gap-0.5 p-2">
        {NAV_ITEMS.map(({ to, label, icon: Icon }) => {
          const active = to === "/" ? pathname === "/" : pathname.startsWith(to);
          return (
            <Link
              key={to}
              to={to}
              className={cn(
                "flex h-8 items-center gap-2.5 rounded px-2.5 text-13 transition-colors",
                active
                  ? "bg-bg-3 text-fg-0"
                  : "text-fg-1 hover:bg-bg-2 hover:text-fg-0",
              )}
            >
              <Icon className={cn("size-3.5", active ? "text-accent" : "text-fg-2")} />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto border-t border-border">
        <ConnectionStatus />
      </div>
    </aside>
  );
}
