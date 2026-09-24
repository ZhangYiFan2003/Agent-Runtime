import { useState } from "react";
import { Menu, X } from "lucide-react";
import { useWorkbench } from "../queries/use-workbench";
import { SUBMISSION_LOST_MESSAGE } from "../queries/workbench-session";
import { useMediaQuery } from "../lib/use-media-query";
import { useDocumentTitle } from "../lib/use-document-title";
import { Composer } from "../components/workbench/composer";
import { RunContextPanel } from "../components/workbench/context-panel";
import { ThreadRail } from "../components/workbench/thread-rail";
import { WorkbenchFeed } from "../components/workbench/workbench-feed";
import { CopyId } from "../components/run/copy-id";
import { StatusPill } from "../components/run/status-pill";
import { Button } from "../components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "../components/ui/sheet";

/**
 * Workbench — the thread + turn + live execution loop (Phase 6).
 *
 * Desktop: thread rail | conversation feed + composer | run context panel.
 * Mobile: rail becomes a left drawer, run context a bottom sheet behind the
 * status chip, composer sticky above the bottom nav. Runtime state is never
 * held locally: the feed projects the thread event replay, the context
 * panel reads the RunView queries, controls go through the Phase-5 engine.
 */
export function WorkbenchPage() {
  useDocumentTitle("Workbench · Axiom");
  const wb = useWorkbench();
  const isDesktop = useMediaQuery("(min-width: 768px)");
  const [railOpen, setRailOpen] = useState(false);
  const [runSheetOpen, setRunSheetOpen] = useState(false);

  const rail = (
    <ThreadRail
      threads={wb.threads}
      selectedThreadId={wb.threadId}
      creating={wb.creatingThread}
      createError={wb.createError}
      onCreate={() => void wb.createAndSelectThread()}
      onSelect={(id) => {
        wb.selectThread(id);
        setRailOpen(false);
      }}
      onRemove={wb.removeThread}
      onOpenById={(id) => {
        wb.selectThread(id);
        setRailOpen(false);
      }}
    />
  );

  const composer = (
    <Composer
      disabled={wb.composer.disabled}
      disabledReason={wb.composer.reason}
      submitting={wb.submission.phase === "submitting"}
      autoFocus={isDesktop}
      onSend={wb.send}
    />
  );

  const notices = (
    <>
      {wb.submission.phase === "lost" && (
        <div
          role="status"
          className="flex items-center justify-between gap-2 border-t border-border bg-info/5 px-4 py-1.5 md:px-6"
        >
          <span className="text-xs text-info">{SUBMISSION_LOST_MESSAGE}</span>
          <button
            type="button"
            onClick={wb.dismissSubmissionNotice}
            aria-label="Dismiss"
            className="shrink-0 rounded p-0.5 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          >
            <X className="size-3" />
          </button>
        </div>
      )}
      {wb.submission.phase === "failed" && (
        <div
          role="alert"
          className="flex items-center justify-between gap-2 border-t border-border bg-danger/5 px-4 py-1.5 md:px-6"
        >
          <span className="text-xs text-danger">
            {wb.submission.errorMessage ?? "Turn submission failed"}
            {wb.submission.errorStatus !== null && wb.submission.errorStatus > 0 && (
              <span className="ml-2 font-mono text-[10px] text-fg-2">
                HTTP {wb.submission.errorStatus}
              </span>
            )}
          </span>
          <button
            type="button"
            onClick={wb.dismissSubmissionNotice}
            aria-label="Dismiss"
            className="shrink-0 rounded p-0.5 text-fg-2 hover:bg-bg-2 hover:text-fg-0"
          >
            <X className="size-3" />
          </button>
        </div>
      )}
    </>
  );

  const runChip = wb.run !== null && (
    <button
      type="button"
      onClick={() => setRunSheetOpen(true)}
      className="inline-flex items-center gap-1.5 rounded border border-border bg-bg-2 px-2 py-1 hover:bg-bg-3"
      aria-label="Open run context"
    >
      <StatusPill status={wb.run.status} statusGroup={wb.run.statusGroup} />
    </button>
  );

  // Slim header below xl (where the context column is hidden) — on mobile it
  // also carries the thread-rail drawer button.
  const header = (
    <div className="flex h-11 shrink-0 items-center justify-between gap-2 border-b border-border px-3 xl:hidden">
      <div className="flex min-w-0 items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          className="md:hidden"
          onClick={() => setRailOpen(true)}
          aria-label="Open threads"
        >
          <Menu />
        </Button>
        {wb.threadId !== null ? (
          <CopyId id={wb.threadId} head={16} />
        ) : (
          <span className="font-mono text-xs text-fg-2">no thread selected</span>
        )}
      </div>
      {runChip}
    </div>
  );

  const emptyHero = (
    <div className="relative flex min-h-0 flex-1 flex-col items-center justify-center px-6 pb-16 md:pb-0">
      <div className="absolute top-2 left-2 md:hidden">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setRailOpen(true)}
          aria-label="Open threads"
        >
          <Menu />
        </Button>
      </div>
      <div className="w-full max-w-xl">
        <div className="text-center font-mono text-[11px] tracking-widest text-fg-2 uppercase">
          Axiom Runtime
        </div>
        <h1 className="mt-2 text-center text-xl font-medium text-fg-0">
          What do you want Axiom to do?
        </h1>
        <div className="mt-6">
          <Composer
            disabled={wb.composer.disabled}
            disabledReason={wb.composer.reason}
            submitting={wb.submission.phase === "submitting" || wb.creatingThread}
            autoFocus={isDesktop}
            hero
            onSend={wb.send}
          />
        </div>
        {notices}
        <div className="mt-4 text-center font-mono text-[11px] text-fg-2">
          ReAct · Plan · Multi-Agent
        </div>
      </div>
    </div>
  );

  const conversation = (
    <section className="flex min-w-0 flex-1 flex-col">
      {header}
      <div className="min-h-0 flex-1">
        <WorkbenchFeed items={wb.items} status={wb.replayStatus} replayError={wb.replayError} />
      </div>
      {notices}
      <div className="mb-[calc(52px+env(safe-area-inset-bottom))] shrink-0 border-t border-border px-4 py-3 md:mb-0 md:px-6">
        {composer}
      </div>
    </section>
  );

  const runContextSheet = (
    <Sheet open={runSheetOpen} onOpenChange={setRunSheetOpen}>
      <SheetContent side={isDesktop ? "right" : "bottom"} className="overflow-y-auto p-0">
        <SheetTitle className="sr-only">Run context</SheetTitle>
        <RunContextPanel runId={wb.activeRunId} />
      </SheetContent>
    </Sheet>
  );

  return (
    <div className="flex h-full">
      {/* Thread rail: fixed column on desktop, left drawer on mobile */}
      <aside className="hidden w-[272px] shrink-0 border-r border-border bg-bg-1/40 md:flex md:flex-col">
        {rail}
      </aside>
      {!isDesktop && (
        <Sheet open={railOpen} onOpenChange={setRailOpen}>
          <SheetContent side="left" className="w-72 p-0">
            <SheetTitle className="sr-only">Threads</SheetTitle>
            {rail}
          </SheetContent>
        </Sheet>
      )}

      {wb.threadId === null ? emptyHero : conversation}

      {/* Run context: right column on wide desktop, sheet otherwise */}
      {wb.threadId !== null && (
        <aside className="hidden w-[320px] shrink-0 flex-col border-l border-border xl:flex">
          <div className="shrink-0 border-b border-border/60 px-3 py-2 text-[11px] font-medium tracking-wider text-fg-2 uppercase">
            Run context
          </div>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
            <RunContextPanel runId={wb.activeRunId} />
          </div>
        </aside>
      )}
      {runContextSheet}
    </div>
  );
}
