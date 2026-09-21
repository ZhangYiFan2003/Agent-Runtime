import { Link } from "@tanstack/react-router";
import { ArrowUpRight } from "lucide-react";
import { formatCompactNumber } from "../../lib/run-detail-view";
import { formatDurationMs } from "../../lib/format";
import { runDurationMs } from "../../lib/runs-view";
import { useRun, useRunMetrics } from "../../queries/use-run-detail";
import { useRunControls } from "../../queries/use-run-controls";
import { ControlFeedback, RunActionBar } from "../run/run-header";
import { RunBanners } from "../run/run-banners";
import { CopyId } from "../run/copy-id";
import { StatusPill } from "../run/status-pill";
import { Skeleton } from "../ui/skeleton";

/**
 * Run Context panel (right column on desktop, sheet on mobile). Compact
 * readout of the thread's active (or most recent) run: status, identity,
 * strategy/kind/duration, a few real metrics when they exist, pending
 * approvals, and the Phase-5 run controls — reused as-is, targeting the
 * exact run_id / invocation_id from the RunView. Nothing here is derived
 * from local state.
 */
export function RunContextPanel({ runId }: { runId: string | null }) {
  const runQuery = useRun(runId ?? "");
  const run = runQuery.data ?? null;
  const metricsQuery = useRunMetrics(runId ?? "", run?.status);
  const metrics = metricsQuery.data ?? null;
  const controls = useRunControls(runId ?? "", { parentRunId: run?.parentRunId ?? null });

  if (runId === null) {
    return (
      <div className="px-4 py-10 text-center">
        <div className="text-13 font-medium text-fg-1">No run yet</div>
        <div className="mt-1 font-mono text-[11px] text-fg-2">
          Runs appear here once a turn starts in this thread.
        </div>
      </div>
    );
  }

  if (run === null) {
    return (
      <div className="flex flex-col gap-2 px-4 py-4" aria-hidden>
        <Skeleton className="h-4 w-28" />
        <Skeleton className="h-3 w-40" />
        <Skeleton className="h-3 w-32" />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-col">
      <div className="shrink-0 px-4 pt-3 pb-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <StatusPill status={run.status} statusGroup={run.statusGroup} />
          <CopyId id={run.runId} head={18} />
        </div>
        <div className="mt-1.5 font-mono text-xs text-fg-1">
          {run.executionStrategy} · {run.runKind} · {formatDurationMs(runDurationMs(run))}
        </div>

        {metrics !== null && (
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-border/60 pt-2">
            <div className="flex items-baseline justify-between gap-2">
              <dt className="font-mono text-[10px] text-fg-2">steps</dt>
              <dd className="font-mono text-xs text-fg-0 tabular-nums">{metrics.stepCount}</dd>
            </div>
            <div className="flex items-baseline justify-between gap-2">
              <dt className="font-mono text-[10px] text-fg-2">tools</dt>
              <dd className="font-mono text-xs text-fg-0 tabular-nums">{metrics.toolCalls}</dd>
            </div>
            <div className="flex items-baseline justify-between gap-2">
              <dt className="font-mono text-[10px] text-fg-2">llm</dt>
              <dd className="font-mono text-xs text-fg-0 tabular-nums">{metrics.llmCalls}</dd>
            </div>
            <div className="flex items-baseline justify-between gap-2">
              <dt className="font-mono text-[10px] text-fg-2">tokens</dt>
              <dd className="font-mono text-xs text-fg-0 tabular-nums">
                {formatCompactNumber(metrics.totalTokens)}
              </dd>
            </div>
          </dl>
        )}
      </div>

      <ControlFeedback controls={controls} />
      <RunBanners run={run} controls={controls} />

      <div className="shrink-0 px-4 py-2.5">
        <RunActionBar run={run} controls={controls} />
      </div>

      <div className="mt-auto shrink-0 border-t border-border/60 px-4 py-2.5">
        <Link
          to="/runs/$runId"
          params={{ runId: run.runId }}
          search={{}}
          className="inline-flex items-center gap-1 text-xs text-info hover:text-fg-0"
        >
          Open Inspector
          <ArrowUpRight className="size-3" />
        </Link>
      </div>
    </div>
  );
}
