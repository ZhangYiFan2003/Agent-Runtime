import { useMemo, useState } from "react";
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ApiError } from "../api/client";
import {
  useRun,
  useRunChildren,
  useRunMetrics,
  useRunTrace,
} from "../queries/use-run-detail";
import { useRuntimeEvents } from "../queries/use-runtime-events";
import { isChildRun } from "../lib/runs-view";
import { useMediaQuery } from "../lib/use-media-query";
import { cn } from "../lib/utils";
import { Button } from "../components/ui/button";
import { ErrorState } from "../components/ui/error-state";
import { Sheet, SheetContent, SheetTitle } from "../components/ui/sheet";
import { Skeleton } from "../components/ui/skeleton";
import { CopyId } from "../components/run/copy-id";
import { RunBanners } from "../components/run/run-banners";
import { RunChildrenTable } from "../components/run/run-children";
import { RunHeader } from "../components/run/run-header";
import { RunMetricsPanel } from "../components/run/run-metrics";
import { RunOutput } from "../components/run/run-output";
import { RunOverview } from "../components/run/run-overview";
import { EventFeed } from "../components/events/event-feed";
import { EventInspector } from "../components/events/event-inspector";
import { SpanInspector } from "../components/trace/span-inspector";
import { SpanTable } from "../components/trace/span-table";
import { TraceWaterfall } from "../components/trace/waterfall";

const routeApi = getRouteApi("/runs/$runId");

type DetailTab = "overview" | "timeline" | "events" | "trace" | "metrics" | "children" | "output";

const DESKTOP_TABS: Array<{ key: DetailTab; label: string }> = [
  { key: "overview", label: "Overview" },
  { key: "events", label: "Events" },
  { key: "trace", label: "Trace" },
  { key: "metrics", label: "Metrics" },
  { key: "children", label: "Children" },
  { key: "output", label: "Output" },
];

const MOBILE_TABS: Array<{ key: DetailTab; label: string }> = [
  { key: "overview", label: "Overview" },
  { key: "timeline", label: "Timeline" },
  { key: "events", label: "Events" },
  { key: "trace", label: "Trace" },
  { key: "metrics", label: "Metrics" },
  { key: "children", label: "Children" },
  { key: "output", label: "Output" },
];

function TabBar({
  tabs,
  active,
  onChange,
  className,
}: {
  tabs: Array<{ key: DetailTab; label: string }>;
  active: DetailTab;
  onChange: (tab: DetailTab) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={cn(
        "flex shrink-0 items-center gap-1 overflow-x-auto border-b border-border px-2",
        className,
      )}
    >
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          role="tab"
          aria-selected={active === tab.key}
          onClick={() => onChange(tab.key)}
          className={cn(
            "h-9 shrink-0 border-b-2 px-2.5 text-13 transition-colors focus-visible:outline-none",
            active === tab.key
              ? "border-b-accent text-fg-0"
              : "border-b-transparent text-fg-2 hover:text-fg-1",
          )}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

function PanelSkeleton() {
  return (
    <div className="flex flex-col gap-2 px-4 py-3" aria-hidden>
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="flex items-center justify-between gap-6">
          <Skeleton className="h-3 w-24" />
          <Skeleton className="h-3 w-36" />
        </div>
      ))}
    </div>
  );
}

function NeutralState({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="px-6 py-16 text-center">
      <div className="text-sm font-medium text-fg-0">{title}</div>
      <div className="mt-1 font-mono text-xs text-fg-2">{hint}</div>
    </div>
  );
}

function QueryError({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-start gap-3 px-4 py-6">
      <ErrorState
        title={
          error instanceof ApiError && error.status > 0
            ? `Request failed · HTTP ${error.status}`
            : "Request failed"
        }
        message={error instanceof ApiError ? error.message : "Unexpected error"}
        className="w-full max-w-md"
      />
      <Button variant="default" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

export function RunDetailPage() {
  const { runId } = routeApi.useParams();
  const search = routeApi.useSearch();
  const navigate = useNavigate();

  const runQuery = useRun(runId);
  const run = runQuery.data;
  const runStatus = run?.status;
  const traceQuery = useRunTrace(runId, runStatus);
  const metricsQuery = useRunMetrics(runId, runStatus);
  const childrenQuery = useRunChildren(runId, runStatus);
  const runtimeEvents = useRuntimeEvents({
    threadId: run?.threadId ?? null,
    runId,
    runStatus,
  });

  const [tab, setTab] = useState<DetailTab>("overview");
  // The mobile bottom-sheet inspector is portaled to document.body, so it
  // cannot be hidden with a `md:hidden` wrapper — gate rendering instead.
  const isDesktop = useMediaQuery("(min-width: 768px)");

  const spans = useMemo(() => traceQuery.data?.spans ?? [], [traceQuery.data]);
  const selectedSpanId = search.span ?? null;
  const selectedSpan = useMemo(
    () =>
      selectedSpanId === null
        ? null
        : (spans.find((span) => span.spanId === selectedSpanId) ?? null),
    [spans, selectedSpanId],
  );
  const spanNotFound =
    selectedSpanId !== null && traceQuery.data !== undefined && selectedSpan === null;

  // Event selection (Events tab) shares the inspector with span selection;
  // the two are mutually exclusive in the URL.
  const events = runtimeEvents.events;
  const selectedEventId = search.event ?? null;
  const selectedEvent = useMemo(
    () =>
      selectedEventId === null
        ? null
        : (events.find((event) => event.eventId === selectedEventId) ?? null),
    [events, selectedEventId],
  );
  const eventNotFound =
    selectedEventId !== null &&
    selectedEvent === null &&
    runtimeEvents.status !== "connecting";

  // Each select replaces the whole search object, so selecting one kind
  // automatically clears the other.
  const selectSpan = (spanId: string | null) =>
    void navigate({
      to: "/runs/$runId",
      params: { runId },
      search: spanId === null ? {} : { span: spanId },
    });
  const toggleSpan = (spanId: string) =>
    selectSpan(spanId === selectedSpanId ? null : spanId);
  const selectEvent = (eventId: number | null) =>
    void navigate({
      to: "/runs/$runId",
      params: { runId },
      search: eventId === null ? {} : { event: eventId },
    });
  const toggleEvent = (eventId: number) =>
    selectEvent(eventId === selectedEventId ? null : eventId);
  const openRun = (id: string) =>
    void navigate({ to: "/runs/$runId", params: { runId: id }, search: {} });

  if (runQuery.isPending) {
    return (
      <div className="flex h-full flex-col">
        <div className="shrink-0 border-b border-border px-4 py-4 md:px-6">
          <Skeleton className="h-3 w-16" />
          <Skeleton className="mt-3 h-4 w-56" />
          <Skeleton className="mt-2 h-3 w-40" />
        </div>
        <PanelSkeleton />
      </div>
    );
  }

  if (runQuery.isError || run === undefined) {
    const error = runQuery.error;
    const notFound = error instanceof ApiError && error.status === 404;
    return (
      <div className="flex h-full flex-col items-start gap-3 px-4 py-10 md:px-6">
        <Link to="/runs" className="text-xs text-fg-2 hover:text-fg-0">
          ← Runs
        </Link>
        <ErrorState
          title={notFound ? "Run not found" : "Failed to load run"}
          message={
            notFound
              ? `No run with id ${runId} exists on this Runtime.`
              : error instanceof ApiError
                ? error.message
                : "Unexpected error"
          }
          className="w-full max-w-md"
        />
        {!notFound && (
          <Button variant="default" size="sm" onClick={() => void runQuery.refetch()}>
            Retry
          </Button>
        )}
      </div>
    );
  }

  const traceArea = (() => {
    if (traceQuery.isPending) return <PanelSkeleton />;
    if (traceQuery.isError)
      return <QueryError error={traceQuery.error} onRetry={() => void traceQuery.refetch()} />;
    if (traceQuery.data === null)
      return (
        <NeutralState title="Trace not available yet" hint="Spans appear once execution records them." />
      );
    return (
      <TraceWaterfall spans={spans} selectedSpanId={selectedSpanId} onSelectSpan={toggleSpan} />
    );
  })();

  const tabContent = (() => {
    switch (tab) {
      case "overview":
        return <RunOverview run={run} metrics={metricsQuery.data ?? null} />;
      case "events":
        return (
          <div className="flex h-full min-h-0 flex-col">
            <EventFeed
              events={events}
              status={runtimeEvents.status}
              runStatus={runStatus}
              selectedEventId={selectedEventId}
              onSelectEvent={toggleEvent}
            />
          </div>
        );
      case "timeline":
        return traceArea;
      case "trace": {
        if (traceQuery.isPending) return <PanelSkeleton />;
        if (traceQuery.isError)
          return <QueryError error={traceQuery.error} onRetry={() => void traceQuery.refetch()} />;
        if (traceQuery.data === null)
          return (
            <NeutralState
              title="Trace not available yet"
              hint="Spans appear once execution records them."
            />
          );
        return (
          <SpanTable spans={spans} selectedSpanId={selectedSpanId} onSelectSpan={toggleSpan} />
        );
      }
      case "metrics": {
        if (metricsQuery.isPending) return <PanelSkeleton />;
        if (metricsQuery.isError)
          return (
            <QueryError error={metricsQuery.error} onRetry={() => void metricsQuery.refetch()} />
          );
        if (metricsQuery.data === null)
          return (
            <NeutralState
              title="Metrics not available yet"
              hint="Metrics are computed once the run has a trace."
            />
          );
        return <RunMetricsPanel metrics={metricsQuery.data} />;
      }
      case "children": {
        if (isChildRun(run)) {
          return (
            <div className="px-6 py-16 text-center">
              <div className="text-sm font-medium text-fg-0">Child run</div>
              <div className="mt-2 inline-flex items-center gap-1.5 font-mono text-xs text-fg-2">
                parent
                {run.parentRunId !== null && (
                  <>
                    <Link
                      to="/runs/$runId"
                      params={{ runId: run.parentRunId }}
                      search={{}}
                      className="text-info hover:text-fg-0"
                    >
                      open
                    </Link>
                    <CopyId id={run.parentRunId} head={16} />
                  </>
                )}
              </div>
            </div>
          );
        }
        if (childrenQuery.isPending) return <PanelSkeleton />;
        if (childrenQuery.isError)
          return (
            <QueryError error={childrenQuery.error} onRetry={() => void childrenQuery.refetch()} />
          );
        return (
          <RunChildrenTable
            children={childrenQuery.data?.children ?? []}
            onOpenRun={openRun}
          />
        );
      }
      case "output":
        return <RunOutput run={run} />;
    }
  })();

  return (
    <div className="flex h-full flex-col">
      <RunHeader run={run} onShowChildren={() => setTab("children")} />
      <RunBanners run={run} />

      <div className="flex min-h-0 flex-1">
        {/* Timeline column (desktop) */}
        <aside className="hidden w-[380px] shrink-0 flex-col border-r border-border md:flex">
          <div className="shrink-0 border-b border-border/60 px-3 py-2 text-[11px] font-medium tracking-wider text-fg-2 uppercase">
            Timeline
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">{traceArea}</div>
        </aside>

        {/* Main view */}
        <section className="flex min-w-0 flex-1 flex-col">
          <TabBar
            tabs={DESKTOP_TABS}
            active={tab === "timeline" ? "overview" : tab}
            onChange={setTab}
            className="hidden md:flex"
          />
          <TabBar tabs={MOBILE_TABS} active={tab} onChange={setTab} className="md:hidden" />
          <div className="min-h-0 flex-1 overflow-y-auto pb-16 md:pb-0">{tabContent}</div>
        </section>

        {/* Inspector column (desktop) */}
        <aside className="hidden w-[320px] shrink-0 flex-col border-l border-border md:flex">
          <div className="shrink-0 border-b border-border/60 px-3 py-2 text-[11px] font-medium tracking-wider text-fg-2 uppercase">
            Inspector
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {selectedEvent !== null || eventNotFound ? (
              <EventInspector event={selectedEvent} notFound={eventNotFound} />
            ) : (
              <SpanInspector span={selectedSpan} notFound={spanNotFound} />
            )}
          </div>
        </aside>
      </div>

      {/* Inspector as bottom sheet (mobile) */}
      {!isDesktop && (
        <Sheet
          open={selectedEvent !== null || eventNotFound || selectedSpan !== null || spanNotFound}
          onOpenChange={(open) => {
            if (!open) {
              selectSpan(null);
              selectEvent(null);
            }
          }}
        >
          <SheetContent side="bottom" className="overflow-y-auto p-0">
            <SheetTitle className="sr-only">Inspector</SheetTitle>
            {selectedEvent !== null || eventNotFound ? (
              <EventInspector event={selectedEvent} notFound={eventNotFound} />
            ) : (
              <SpanInspector span={selectedSpan} notFound={spanNotFound} />
            )}
          </SheetContent>
        </Sheet>
      )}
    </div>
  );
}
