import { Outlet } from "@tanstack/react-router";
import { AppSidebar } from "../components/app-sidebar";
import { MobileNav } from "../components/mobile-nav";

export function AppShell() {
  return (
    <div className="flex h-full">
      <AppSidebar />
      <main className="relative min-w-0 flex-1 overflow-y-auto">
        <Outlet />
      </main>
      <MobileNav />
    </div>
  );
}
