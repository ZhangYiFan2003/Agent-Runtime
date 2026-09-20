import { useEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import { ArrowLeft, MoreHorizontal, X } from "lucide-react";
import type { RunView } from "../../api/adapters/run";
import { formatDurationMs } from "../../lib/format";
import { childrenLabel, isChildRun, runDurationMs } from "../../lib/runs-view";
import {
  actionPendingLabel,
  deriveRunActions,
  selfPendingInterrupt,
  type RunActionSpec,
} from "../../lib/run-detail-view";
import { useMediaQuery } from "../../lib/use-media-query";
import type { RunControls } from "../../queries/use-run-controls";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../ui/dialog";
import { Sheet, SheetContent, SheetTitle } from "../ui/sheet";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";
import { CopyId } from "./copy-id";
import { StatusPill } from "./status-pill";

const UNKNOWN_OP_HINT = "This operation is not supported by the console yet.";

/** Dismissible inline feedback for control mutations (success / conflict / error). */
function ControlFeedback({ controls }: { controls: RunControls }) {
  const feedback = controls.feedback;
  if (feedback === null) return null;
  return (
    <div
      role={feedback.tone === "error" ? "alert" : "status"}
      className="flex items-center gap-1.5 px-4 pb-1 md:px-6"
    >
      <span
        className={cn(
          "min-w-0 truncate text-xs",
          feedback.tone === "error" ? "text-danger" : "text-accent",
        )}
      >
        {feedback.message}
      </span>
      <button
        type="button"
        onClick={controls.dismissFeedback}
        aria-label="Dismiss message"
        className="shrink-0 rounded p-0.5 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
      >
        <X className="size-3" />
      </button>
    </div>
  );
}

interface ActionButtonProps {
  action: RunActionSpec;
  run: RunView;
  controls: RunControls;
  onOpenCancel: () => void;
  onOpenInterrupt: () => void;
}

function ActionButton({ action, run, controls, onOpenCancel, onOpenInterrupt }: ActionButtonProps) {
  const variant =
    action.tone === "danger" ? "danger" : action.tone === "primary" ? "primary" : "default";
  const pendingLabel = actionPendingLabel(action.operation);

  // Unknown future operations degrade to a disabled button with a hint.
  if (!action.known || pendingLabel === null) {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          {/* disabled buttons swallow pointer events — the span keeps the tooltip alive */}
          <span tabIndex={0} className="inline-flex">
            <Button variant={variant} size="sm" disabled>
              {action.label}
            </Button>
          </span>
        </TooltipTrigger>
        <TooltipContent side="bottom">{UNKNOWN_OP_HINT}</TooltipContent>
      </Tooltip>
    );
  }

  let pending = false;
  let disabled = controls.anyPending;
  let onClick: (() => void) | undefined;

  switch (action.operation) {
    case "resume":
      pending = controls.pending.resume;
      onClick = () => void controls.resume();
      break;
    case "requeue":
      pending = controls.pending.requeue;
      onClick = () => void controls.requeue();
      break;
    case "interrupt":
      pending = controls.pending.interrupt;
      onClick = onOpenInterrupt;
      break;
    case "cancel":
      pending = controls.pending.cancel;
      onClick = onOpenCancel;
      break;
    case "approve":
    case "reject": {
      const target = selfPendingInterrupt(run);
      if (target === null) {
        disabled = true;
      } else {
        pending = controls.pending.approvals.get(`${target.runId}:${target.invocationId}`) === true;
        const decision = action.operation as "approve" | "reject";
        onClick = () => void controls.resolveApproval(target, decision);
      }
      break;
    }
  }

  return (
    <Button variant={variant} size="sm" disabled={disabled} onClick={onClick}>
      {pending ? pendingLabel : action.label}
    </Button>
  );
}

/** Cancel confirmation — the cascade to non-terminal children is called out. */
function CancelDialog({
  open,
  onOpenChange,
  controls,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  controls: RunControls;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogTitle>Cancel this run?</DialogTitle>
        <DialogDescription className="mt-1.5 text-13 text-fg-1">
          The run will enter a terminal CANCELLED state. Non-terminal direct child runs will also
          be cancelled.
        </DialogDescription>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="default" size="sm" onClick={() => onOpenChange(false)}>
            Keep running
          </Button>
          <Button
            variant="danger"
            size="sm"
            disabled={controls.anyPending}
            onClick={() => {
              // Close on any definitive outcome; the inline feedback line
              // carries the result (success note or conflict/error message).
              void controls.cancel().then(() => onOpenChange(false));
            }}
          >
            {controls.pending.cancel ? "Cancelling…" : "Cancel run"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Interrupt with a single optional reason (server default: "manual interrupt"). */
function InterruptDialog({
  open,
  onOpenChange,
  controls,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  controls: RunControls;
}) {
  const [reason, setReason] = useState("");
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) setReason("");
      }}
    >
      <DialogContent>
        <DialogTitle>Interrupt this run?</DialogTitle>
        <DialogDescription className="mt-1.5 text-13 text-fg-1">
          The run stops executing and waits until it is resumed.
        </DialogDescription>
        <input
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="manual interrupt"
          aria-label="Interrupt reason"
          className="mt-3 h-8 w-full rounded border border-border bg-bg-1 px-2 font-mono text-xs text-fg-0 placeholder:text-fg-2 focus-visible:ring-1 focus-visible:ring-accent focus-visible:outline-none"
        />
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="default" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            size="sm"
            disabled={controls.anyPending}
            onClick={() => {
              const trimmed = reason.trim();
              void controls.interrupt(trimmed === "" ? undefined : trimmed).then(() =>
                onOpenChange(false),
              );
            }}
          >
            {controls.pending.interrupt ? "Interrupting…" : "Interrupt"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Actionable control bar: buttons come from `run.allowedOperations` only.
 * Resume / Requeue act directly, Interrupt and Cancel open dialogs, and
 * Approve / Reject resolve the run's own pending interrupt. While any
 * control mutation for this run is pending every button here is disabled.
 */
export function RunActionBar({
  run,
  controls,
}: {
  run: RunView;
  controls: RunControls;
}) {
  const actions = deriveRunActions(run.allowedOperations);
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const [cancelOpen, setCancelOpen] = useState(false);
  const [interruptOpen, setInterruptOpen] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);

  useEffect(() => {
    if (!import.meta.env.DEV) return;
    for (const action of actions) {
      if (!action.known) {
        console.warn(`[run-detail] unknown allowed operation ignored by label map: ${action.operation}`);
      }
    }
  }, [actions]);

  if (actions.length === 0) return null;

  // Mobile: with more than two actions keep the primary one visible and
  // collapse the rest into a "More" sheet.
  const collapse = !isDesktop && actions.length > 2;
  const primary =
    collapse
      ? (actions.find(
          (action) => action.operation === "approve" || action.operation === "resume",
        ) ?? actions[0])
      : null;
  const visible = primary === null ? actions : [primary];
  const hidden = primary === null ? [] : actions.filter((action) => action !== primary);

  const buttonProps = {
    run,
    controls,
    onOpenCancel: () => setCancelOpen(true),
    onOpenInterrupt: () => setInterruptOpen(true),
  };

  return (
    <>
      <div className="flex flex-wrap items-center gap-1.5" aria-label="Run actions">
        {visible.map((action) => (
          <ActionButton key={action.operation} action={action} {...buttonProps} />
        ))}
        {hidden.length > 0 && (
          <>
            <Button
              variant="default"
              size="sm"
              aria-label="More actions"
              onClick={() => setMoreOpen(true)}
            >
              <MoreHorizontal className="size-3.5" />
              More
            </Button>
            <Sheet open={moreOpen} onOpenChange={setMoreOpen}>
              <SheetContent side="bottom" className="overflow-y-auto">
                <SheetTitle className="sr-only">Run actions</SheetTitle>
                <div className="flex flex-col items-stretch gap-2 pt-2">
                  {hidden.map((action) => (
                    <ActionButton key={action.operation} action={action} {...buttonProps} />
                  ))}
                </div>
              </SheetContent>
            </Sheet>
          </>
        )}
      </div>
      <CancelDialog open={cancelOpen} onOpenChange={setCancelOpen} controls={controls} />
      <InterruptDialog open={interruptOpen} onOpenChange={setInterruptOpen} controls={controls} />
    </>
  );
}

interface RunHeaderProps {
  run: RunView;
  /** Parent run with children: jump to the Children tab. */
  onShowChildren: () => void;
  controls: RunControls;
}

export function RunHeader({ run, onShowChildren, controls }: RunHeaderProps) {
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
        <RunActionBar run={run} controls={controls} />
      </div>
      <ControlFeedback controls={controls} />

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
