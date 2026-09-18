import { Link, useRouterState } from "@tanstack/react-router";
import { Boxes, ListTree, Settings } from "lucide-react";
import { cn } from "../lib/utils";

const ITEMS = [
  { to: "/", label: "Workbench", icon: Boxes },
  { to: "/runs", label: "Runs", icon: ListTree },
  { to: "/settings", label: "Settings", icon: Settings },
] as const;

/** Mobile bottom navigation (< md). */
export function MobileNav() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return (
    <nav
      className="fixed inset-x-0 bottom-0 z-40 flex border-t border-border bg-bg-1 pb-[env(safe-area-inset-bottom)] md:hidden"
      aria-label="Primary"
    >
      {ITEMS.map(({ to, label, icon: Icon }) => {
        const active = to === "/" ? pathname === "/" : pathname.startsWith(to);
        return (
          <Link
            key={to}
            to={to}
            className={cn(
              "flex flex-1 flex-col items-center gap-1 py-2 text-[11px]",
              active ? "text-accent" : "text-fg-2",
            )}
          >
            <Icon className="size-4" />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
