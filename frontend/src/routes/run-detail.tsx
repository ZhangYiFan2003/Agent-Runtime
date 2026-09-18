import { getRouteApi } from "@tanstack/react-router";
import { ListTree } from "lucide-react";
import { CopyId } from "../components/run/copy-id";

const routeApi = getRouteApi("/runs/$runId");

/** Phase 3 will implement the real Run Detail. The route is live and the
 *  run ID travels in the URL, but no run data is fetched here yet. */
export function RunDetailPage() {
  const { runId } = routeApi.useParams();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <div className="flex size-10 items-center justify-center rounded-md border border-border bg-bg-1">
        <ListTree className="size-4.5 text-fg-2" />
      </div>
      <div className="text-sm font-medium text-fg-0">Run Detail</div>
      <CopyId id={runId} head={24} className="text-13" />
      <div className="font-mono text-xs text-fg-2">
        Coming in Phase 3<span className="text-accent">_</span>
      </div>
    </div>
  );
}
