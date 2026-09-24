import {
  createRootRoute,
  createRoute,
  createRouter,
  lazyRouteComponent,
} from "@tanstack/react-router";
import { AppShell } from "./routes/root";
import { RouteError, RouteNotFound, RoutePending } from "./components/route-fallback";
import { parseRunDetailSearch } from "./lib/run-detail-view";

const rootRoute = createRootRoute({
  component: AppShell,
  // One boundary for every child route: unexpected render errors degrade to
  // an in-shell error view instead of a blank console.
  errorComponent: RouteError,
  notFoundComponent: RouteNotFound,
});

// Route-level code splitting only (Stage 7): each page ships as its own
// chunk; the shell + contract layer stay in the entry bundle.
const workbenchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: lazyRouteComponent(() => import("./routes/workbench"), "WorkbenchPage"),
});

const runsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs",
  component: lazyRouteComponent(() => import("./routes/runs"), "RunsPage"),
});

const runDetailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$runId",
  component: lazyRouteComponent(() => import("./routes/run-detail"), "RunDetailPage"),
  // Span selection lives in the URL (?span=…) so views are shareable and
  // back/forward compatible.
  validateSearch: parseRunDetailSearch,
});

const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: lazyRouteComponent(() => import("./routes/settings"), "SettingsPage"),
});

const routeTree = rootRoute.addChildren([
  workbenchRoute,
  runsRoute,
  runDetailRoute,
  settingsRoute,
]);

export const router = createRouter({
  routeTree,
  defaultPendingComponent: RoutePending,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
