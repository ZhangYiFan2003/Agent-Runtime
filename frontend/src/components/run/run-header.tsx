import { useEffect } from "react";
import { Link } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import type { RunView } from "../../api/adapters/run";
import { formatDurationMs } from "../../lib/format";
import { childrenLabel, isChildRun, runDurationMs } from "../../lib/runs-view";
import { deriveRunActions, type RunActionSpec } from "../../lib/run-detail-view";
import { Button } from "../ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";
import { CopyId } from "./copy-id";
import { StatusPill } from "./status-pill";

const CONTROL_PHASE_HINT = "Run controls will be enabled in the control phase.";

function ActionButton({ action }: { action: RunActionSpec }) {
  const variant = action.tone === "danger" ? "danger" : "default";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* disabled buttons swallow pointer events — the span keeps the tooltip alive */}
        <span tabIndex={0} className="inline-flex">
          <Button
            variant={variant}
            size="sm"
            disabled
            className={action.tone === "primary" ? "border-accent/40 text-accent" : undefined}
          >
            {action.label}
          </Button>
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom">{CONTROL_PHASE_HINT}</TooltipContent>
    </Tooltip>
  );
}

/**
 * Read-only action bar: buttons come from `run.allowedOperations` only.
 * Phase 3 renders every action disabled; mutations arrive in Phase 5.
 */
export function RunActionBar({ operations }: { operations: readonly string[] }) {
  const actions = deriveRunActions(operations);

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    for (const action of actions) {
      if (!action.known) {
        console.warn(`[run-detail] unknown allowed operation ignored by label map: ${action.operation}`);
      }
    }
  }, [actions]);

  if (actions.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5" aria-label="Run actions (read-only)">
      {actions.map((action) => (
        <ActionButton key={action.operation} action={action} />
      ))}
    </div>
  );
}

interface RunHeaderProps {
  run: RunView;
  /** Parent run with children: jump to the Children tab. */
  onShowChildren: () => void;
}

export function RunHeader({ run, onShowChildren }: RunHeaderProps) {
  const lineage = childrenLabel(run);
  return (
    <header className="shrink-0 border-b border-border">
      <div className="flex items-center justify-between gap-3 px-4 pt-2.5 md:px-6">
        <Link
          to="/runs"
          className="inline-flex shrink-0 items-center gap-1 text-xs text-fg-2 transition-colors hover:text-fg-0"
        >
          <ArrowLeft className="size-3.5" />
          Runs
        </Link>
        <RunActionBar operations={run.allowedOperations} />
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 pt-2 md:px-6">
        <StatusPill status={run.status} statusGroup={run.statusGroup} />
        <CopyId id={run.runId} head={22} />
      </div>

      <div className="px-4 pt-1 font-mono text-xs text-fg-1 md:px-6">
        {run.executionStrategy} · {run.runKind} · {formatDurationMs(runDurationMs(run))}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 pt-1 pb-2.5 md:px-6">
        <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-fg-2">
          thread <CopyId id={run.threadId} head={12} />
        </span>
        {run.turnId !== null && (
          <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-fg-2">
            turn <CopyId id={run.turnId} head={12} />
          </span>
        )}
        {isChildRun(run) && run.parentRunId !== null ? (
          <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-fg-2">
            <Link
              to="/runs/$runId"
              params={{ runId: run.parentRunId }}
              search={{}}
              className="text-info hover:text-fg-0"
            >
              ↳ parent
            </Link>
            <CopyId id={run.parentRunId} head={12} />
          </span>
        ) : lineage !== "" ? (
          <button
            type="button"
            onClick={onShowChildren}
            className="font-mono text-[11px] text-info hover:text-fg-0"
          >
            {lineage}
          </button>
        ) : null}
      </div>
    </header>
  );
}
