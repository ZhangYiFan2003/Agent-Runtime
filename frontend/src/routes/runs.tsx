import { useMemo, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { ListTree, RefreshCw, SlidersHorizontal } from "lucide-react";
import { useRuns } from "../queries/use-runs";
import { ApiError } from "../api/client";
import {
  ALL_FILTER,
  deriveFilterOptions,
  filterRuns,
  runsEmptyState,
  sortRuns,
  type RunsFilter,
  type RunsSortKey,
} from "../lib/runs-view";
import { Button } from "../components/ui/button";
import { ErrorState } from "../components/ui/error-state";
import { Input } from "../components/ui/input";
import { Separator } from "../components/ui/separator";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "../components/ui/sheet";
import { Skeleton } from "../components/ui/skeleton";
import { RunsFilterControls } from "../components/run/runs-filter-controls";
import { RunsMobileList, RunsTable } from "../components/run/runs-list";

function TableSkeleton() {
  return (
    <div className="flex flex-col" aria-hidden>
      {Array.from({ length: 8 }, (_, i) => (
        <div key={i} className="flex h-10 items-center gap-6 border-b border-border/60 px-4 last:border-b-0">
          <Skeleton className="h-3 w-20" />
          <Skeleton className="h-3 w-28" />
          <Skeleton className="h-3 w-16" />
          <Skeleton className="h-3 w-14" />
          <Skeleton className="h-3 w-24" />
          <Skeleton className="ml-auto h-3 w-12" />
        </div>
      ))}
    </div>
  );
}

function EmptyState({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-16 text-center">
      <ListTree className="size-4 text-fg-2" />
      <div className="text-sm font-medium text-fg-0">{title}</div>
      <div className="font-mono text-xs text-fg-2">{hint}</div>
    </div>
  );
}

export function RunsPage() {
  const navigate = useNavigate();
  const runsQuery = useRuns();
  const [filter, setFilter] = useState<RunsFilter>({
    search: "",
    status: ALL_FILTER,
    strategy: ALL_FILTER,
  });
  const [sort, setSort] = useState<RunsSortKey>("newest");
  const [filtersOpen, setFiltersOpen] = useState(false);

  const allRuns = useMemo(() => runsQuery.data ?? [], [runsQuery.data]);
  const options = useMemo(() => deriveFilterOptions(allRuns), [allRuns]);
  const visibleRuns = useMemo(
    () => sortRuns(filterRuns(allRuns, filter), sort),
    [allRuns, filter, sort],
  );
  const empty = runsEmptyState(allRuns.length, visibleRuns.length);

  const patchFilter = (patch: Partial<RunsFilter>) => setFilter((f) => ({ ...f, ...patch }));
  const openRun = (runId: string) =>
    void navigate({ to: "/runs/$runId", params: { runId } });

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex h-13 shrink-0 items-center justify-between border-b border-border px-4 md:px-6">
        <div className="flex items-baseline gap-3">
          <h1 className="text-sm font-semibold text-fg-0">Runs</h1>
          {runsQuery.isSuccess && (
            <span className="font-mono text-[11px] text-fg-2">
              {visibleRuns.length}
              {visibleRuns.length !== allRuns.length ? ` / ${allRuns.length}` : ""}
            </span>
          )}
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => void runsQuery.refetch()}
          disabled={runsQuery.isFetching}
          aria-label="Refresh runs"
        >
          <RefreshCw className={runsQuery.isFetching ? "animate-spin" : ""} />
          {runsQuery.isFetching ? "Refreshing…" : "Refresh"}
        </Button>
      </div>

      {/* Filter bar: desktop inline, mobile = search + sheet */}
      <div className="shrink-0 border-b border-border p-3 md:px-6 md:py-2.5">
        <div className="hidden md:block">
          <RunsFilterControls
            filter={filter}
            onFilterChange={patchFilter}
            options={options}
            sort={sort}
            onSortChange={setSort}
          />
        </div>
        <div className="flex gap-2 md:hidden">
          <Input
            value={filter.search}
            onChange={(e) => patchFilter({ search: e.target.value })}
            placeholder="Search runs…"
            aria-label="Search runs"
            autoComplete="off"
            spellCheck={false}
            className="h-8 flex-1 font-mono text-xs"
          />
          <Sheet open={filtersOpen} onOpenChange={setFiltersOpen}>
            <SheetTrigger asChild>
              <Button variant="default" size="default" aria-label="Open filters" className="h-auto">
                <SlidersHorizontal />
              </Button>
            </SheetTrigger>
            <SheetContent side="right" className="flex w-72 flex-col gap-4">
              <SheetTitle className="text-sm font-semibold text-fg-0">Filters</SheetTitle>
              <Separator />
              <RunsFilterControls
                filter={filter}
                onFilterChange={patchFilter}
                options={options}
                sort={sort}
                onSortChange={setSort}
              />
              <Button
                variant="ghost"
                size="sm"
                className="mt-auto"
                onClick={() =>
                  setFilter({ search: "", status: ALL_FILTER, strategy: ALL_FILTER })
                }
              >
                Clear filters
              </Button>
            </SheetContent>
          </Sheet>
        </div>
      </div>

      {/* Content */}
      <div className="min-h-0 flex-1 overflow-y-auto pb-16 md:pb-0">
        {runsQuery.isPending && <TableSkeleton />}

        {runsQuery.isError && (
          <div className="flex flex-col items-start gap-3 px-6 py-10">
            <ErrorState
              title={
                runsQuery.error instanceof ApiError && runsQuery.error.status > 0
                  ? `Failed to load runs · HTTP ${runsQuery.error.status}`
                  : "Failed to load runs"
              }
              message={
                runsQuery.error instanceof ApiError
                  ? runsQuery.error.code
                    ? `${runsQuery.error.code}: ${runsQuery.error.message}`
                    : runsQuery.error.message
                  : "Unexpected error"
              }
              className="w-full max-w-md"
            />
            <Button variant="default" size="sm" onClick={() => void runsQuery.refetch()}>
              Retry
            </Button>
          </div>
        )}

        {runsQuery.isSuccess && empty && <EmptyState title={empty.title} hint={empty.hint} />}

        {runsQuery.isSuccess && !empty && (
          <>
            <div className="hidden md:block">
              <RunsTable runs={visibleRuns} onOpenRun={openRun} />
            </div>
            <div className="md:hidden">
              <RunsMobileList runs={visibleRuns} onOpenRun={openRun} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}
