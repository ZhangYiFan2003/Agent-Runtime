import { Outlet } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { AppSidebar } from "../components/app-sidebar";
import { MobileNav } from "../components/mobile-nav";

/** App frame (sidebar + main + mobile nav) — shared by the root route and
 *  the router-level error / not-found fallbacks so navigation survives. */
export function ShellFrame({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full">
      <AppSidebar />
      <main className="relative min-w-0 flex-1 overflow-y-auto">{children}</main>
      <MobileNav />
    </div>
  );
}

export function AppShell() {
  return (
    <ShellFrame>
      <Outlet />
    </ShellFrame>
  );
}
