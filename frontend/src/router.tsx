import {
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { AppShell } from "./routes/root";
import { WorkbenchPage } from "./routes/workbench";
import { RunsPage } from "./routes/runs";
import { RunDetailPage } from "./routes/run-detail";
import { SettingsPage } from "./routes/settings";
import { parseRunDetailSearch } from "./lib/run-detail-view";

const rootRoute = createRootRoute({ component: AppShell });

const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: WorkbenchPage,
});

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: RunsPage,
});

const runDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$runId",
  component: RunDetailPage,
  // Span selection lives in the URL (?span=…) so views are shareable and
  // back/forward compatible.
  validateSearch: parseRunDetailSearch,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: SettingsPage,
});

const routeTree = rootRoute.addChildren([
  workbenchRoute,
  runsRoute,
  runDetailRoute,
  settingsRoute,
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
