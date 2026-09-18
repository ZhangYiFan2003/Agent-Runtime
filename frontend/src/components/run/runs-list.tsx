import { useMemo } from "react";
import type { RunView } from "../../api/adapters/run";
import { formatDurationMs, formatFullTime, formatRelativeTime } from "../../lib/format";
import { childrenLabel, isChildRun, runDurationMs } from "../../lib/runs-view";
import { CopyId } from "./copy-id";
import { StatusPill } from "./status-pill";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

function LineageCell({ run }: { run: RunView }) {
  if (isChildRun(run)) {
    return (
      <span className="font-mono text-xs whitespace-nowrap text-info">
        ↳ {run.runKind === "agent" ? "child" : run.runKind.replace(/_/g, " ")}
      </span>
    );
  }
  const label = childrenLabel(run);
  if (label === "") return <span className="text-fg-2">—</span>;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="cursor-default font-mono text-xs whitespace-nowrap text-fg-1">
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" className="font-mono text-[11px]">
        {run.activeChildRunIds.length > 0
          ? `active: ${run.activeChildRunIds.join(", ")}`
          : `${run.childrenCount} direct children`}
      </TooltipContent>
    </Tooltip>
  );
}

interface RunsTableProps {
  runs: RunView[];
  onOpenRun: (runId: string) => void;
}

/** Dense desktop table — hairline rows, no cards. */
export function RunsTable({ runs, onOpenRun }: RunsTableProps) {
  return (
    <table className="w-full border-collapse text-left">
      <thead>
        <tr className="border-b border-border">
          {["Status", "Run", "Strategy", "Kind", "Lineage", "Duration", "Updated"].map((h) => (
            <th
              key={h}
              scope="col"
              className="px-3 py-2 text-[11px] font-medium tracking-wider text-fg-2 uppercase first:pl-4 last:pr-4"
            >
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const duration = runDurationMs(run);
          return (
            <tr
              key={run.runId}
              tabIndex={0}
              role="link"
              aria-label={`Run ${run.runId}, status ${run.status}`}
              onClick={() => onOpenRun(run.runId)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onOpenRun(run.runId);
                }
              }}
              className="h-10 cursor-pointer border-b border-border/60 transition-colors last:border-b-0 hover:bg-bg-2 focus-visible:bg-bg-3 focus-visible:outline-none"
            >
              <td className="px-3 first:pl-4">
                <StatusPill status={run.status} statusGroup={run.statusGroup} />
              </td>
              <td className="px-3">
                <div className="flex flex-col leading-tight">
                  <CopyId id={run.runId} />
                  <span className="font-mono text-[11px] text-fg-2">{run.threadId}</span>
                </div>
              </td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">
                {run.executionStrategy}
              </td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">{run.runKind}</td>
              <td className="px-3">
                <LineageCell run={run} />
              </td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">
                {formatDurationMs(duration)}
              </td>
              <td className="px-3 last:pr-4">
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="cursor-default font-mono text-xs whitespace-nowrap text-fg-2">
                      {formatRelativeTime(run.updatedAt)}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent side="top" className="font-mono text-[11px]">
                    {formatFullTime(run.updatedAt)}
                  </TooltipContent>
                </Tooltip>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Compact mobile list rows — border-separated, never shrunken tables. */
export function RunsMobileList({ runs, onOpenRun }: RunsTableProps) {
  const rows = useMemo(
    () =>
      runs.map((run) => {
        const label = childrenLabel(run);
        const meta = [
          isChildRun(run)
            ? `↳ ${run.runKind === "agent" ? "child" : run.runKind.replace(/_/g, " ")}`
            : null,
          label !== "" ? label : null,
        ]
          .filter(Boolean)
          .join(" · ");
        return { run, meta };
      }),
    [runs],
  );

  return (
    <ul className="flex flex-col">
      {rows.map(({ run, meta }) => (
        <li key={run.runId} className="border-b border-border/60 last:border-b-0">
          <button
            type="button"
            onClick={() => onOpenRun(run.runId)}
            aria-label={`Run ${run.runId}, status ${run.status}`}
            className="flex w-full flex-col gap-1 px-4 py-2.5 text-left transition-colors hover:bg-bg-2 focus-visible:bg-bg-3 focus-visible:outline-none"
          >
            <div className="flex items-center justify-between gap-2">
              <StatusPill status={run.status} statusGroup={run.statusGroup} />
              <span className="font-mono text-xs text-fg-2">
                {formatDurationMs(runDurationMs(run))}
              </span>
            </div>
            <div className="flex items-center justify-between gap-2">
              <CopyId id={run.runId} head={18} />
              <span className="font-mono text-[11px] text-fg-2">
                {formatRelativeTime(run.updatedAt)}
              </span>
            </div>
            <div className="font-mono text-[11px] text-fg-2">
              {run.executionStrategy} · {run.runKind}
              {run.threadId ? ` · ${run.threadId}` : ""}
            </div>
            {meta !== "" && <div className="font-mono text-[11px] text-info">{meta}</div>}
          </button>
        </li>
      ))}
    </ul>
  );
}
