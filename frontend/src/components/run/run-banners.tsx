import type { InterruptSummary, RunView } from "../../api/adapters/run";
import { deriveRunBanners } from "../../lib/run-detail-view";
import type { RunControls } from "../../queries/use-run-controls";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { CopyId } from "./copy-id";

/**
 * Actionable approval card for one pending interrupt. Resolving posts to the
 * interrupt's target run (`interrupt.runId` — a child approval targets the
 * child run, never the parent by proxy) with that interrupt's invocation id.
 */
function ApprovalCard({
  run,
  interrupt,
  controls,
}: {
  run: RunView;
  interrupt: InterruptSummary;
  controls: RunControls;
}) {
  const isChildTarget = interrupt.runId !== run.runId;
  const key = `${interrupt.runId}:${interrupt.invocationId}`;
  const approving = controls.pending.approvals.get(key) === true;

  return (
    <div role="status" className="border-l-2 border-l-warn bg-warn/5 px-4 py-2 md:px-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-13 font-medium text-warn">
          Approval required
          {interrupt.toolName !== null && (
            <span className="ml-2 font-mono text-xs text-fg-1">{interrupt.toolName}</span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            variant="primary"
            size="sm"
            disabled={controls.anyPending}
            onClick={() => void controls.resolveApproval(interrupt, "approve")}
          >
            {approving ? "Approving…" : "Approve"}
          </Button>
          <Button
            variant="danger"
            size="sm"
            disabled={controls.anyPending}
            onClick={() => void controls.resolveApproval(interrupt, "reject")}
          >
            {approving ? "Rejecting…" : "Reject"}
          </Button>
        </div>
      </div>

      <div className="mt-0.5 text-xs text-fg-1">{interrupt.reason}</div>

      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="inline-flex items-center gap-1.5 font-mono text-[11px] text-fg-2">
          invocation <CopyId id={interrupt.invocationId} head={18} />
        </span>
        <span
          className={cn(
            "inline-flex items-center gap-1.5 font-mono text-[11px]",
            isChildTarget ? "text-warn" : "text-fg-2",
          )}
        >
          {isChildTarget ? "child run" : "run"} <CopyId id={interrupt.runId} head={18} />
        </span>
      </div>

      <div className="mt-1 text-[11px] text-fg-2">
        Tool arguments are not exposed by the control-plane API.
      </div>
    </div>
  );
}

/**
 * Waiting / recovery banner stack plus one actionable approval card per
 * pending interrupt. Waiting/recovery banners stay read-only; approvals are
 * resolved inline (Approve / Reject, no nested confirms).
 */
export function RunBanners({ run, controls }: { run: RunView; controls: RunControls }) {
  // Approval entries of deriveRunBanners are rendered as actionable cards
  // from run.pendingInterrupts instead (they need structured interrupt data).
  const banners = deriveRunBanners(run).filter((banner) => banner.kind !== "approval");
  if (banners.length === 0 && run.pendingInterrupts.length === 0) return null;
  return (
    <div className="shrink-0 border-b border-border">
      {run.pendingInterrupts.map((interrupt) => (
        <ApprovalCard
          key={`${interrupt.runId}:${interrupt.invocationId}`}
          run={run}
          interrupt={interrupt}
          controls={controls}
        />
      ))}
      {banners.map((banner, index) => (
        <div
          key={`${banner.kind}-${index}`}
          role="status"
          className={cn(
            "border-l-2 px-4 py-2 md:px-6",
            banner.tone === "warn" ? "border-l-warn bg-warn/5" : "border-l-info bg-info/5",
            index > 0 && "border-t border-t-border/60",
          )}
        >
          <div className={cn("text-13 font-medium", banner.tone === "warn" ? "text-warn" : "text-info")}>
            {banner.title}
          </div>
          {banner.message !== undefined && (
            <div className="mt-0.5 text-xs text-fg-1">{banner.message}</div>
          )}
          {banner.fields.map((field) => (
            <div key={field.label} className="mt-0.5 flex gap-2 font-mono text-[11px]">
              <span className="shrink-0 text-fg-2">{field.label}</span>
              <span className="min-w-0 break-all text-fg-1">{field.value}</span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
