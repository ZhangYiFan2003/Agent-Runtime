import { Link, type ErrorComponentProps } from "@tanstack/react-router";
import { ShellFrame } from "../routes/root";
import { useDocumentTitle } from "../lib/use-document-title";
import { Button } from "./ui/button";
import { ErrorState } from "./ui/error-state";
import { Skeleton } from "./ui/skeleton";

/**
 * Router-level fallbacks (Stage 7): lazy-route pending skeleton, route error
 * boundary, and Not Found — all keep the App Shell frame so navigation and
 * the connection status stay reachable.
 */

/** Lightweight in-shell skeleton while a lazy route chunk loads. */
export function RoutePending() {
  return (
    <div className="flex h-full flex-col px-4 py-4 md:px-6" aria-busy="true" aria-label="Loading">
      <Skeleton className="h-4 w-40" />
      <Skeleton className="mt-3 h-3 w-64" />
      <Skeleton className="mt-6 h-3 w-full" />
      <Skeleton className="mt-2 h-3 w-full" />
      <Skeleton className="mt-2 h-3 w-2/3" />
    </div>
  );
}

/**
 * Root route error boundary. Shows a normalized message only; the raw error
 * message is dev-only and stacks/paths are never rendered in production.
 */
export function RouteError({ error, reset }: ErrorComponentProps) {
  useDocumentTitle("Error · Axiom");
  return (
    <ShellFrame>
      <div className="flex h-full flex-col items-start gap-3 px-4 py-10 md:px-6">
        <ErrorState
          title="Something went wrong"
          message="The console hit an unexpected error while rendering this view."
          className="w-full max-w-md"
        />
        {import.meta.env.DEV && error instanceof Error && (
          <pre className="max-w-2xl overflow-auto rounded border border-border bg-bg-1 p-2 font-mono text-[11px] break-all whitespace-pre-wrap text-fg-2">
            {error.message}
          </pre>
        )}
        <div className="flex gap-2">
          <Button variant="default" size="sm" onClick={reset}>
            Retry
          </Button>
          <Button variant="default" size="sm" onClick={() => window.location.reload()}>
            Reload page
          </Button>
        </div>
      </div>
    </ShellFrame>
  );
}

/**
 * Unmatched URL. TanStack renders `notFoundComponent` INSIDE the root
 * route's <Outlet/>, so this is content-only — the App Shell is already
 * present (wrapping it in ShellFrame here would double the sidebar).
 */
export function RouteNotFound() {
  useDocumentTitle("Page not found · Axiom");
  return (
    <div className="flex h-full flex-col items-start gap-3 px-4 py-10 md:px-6">
      <div className="text-sm font-medium text-fg-0">Page not found</div>
      <p className="max-w-md text-13 text-fg-1">This URL does not match any console view.</p>
      <Link to="/" className="text-xs text-info hover:text-fg-0">
        ← Return to Workbench
      </Link>
    </div>
  );
}
