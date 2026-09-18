import type { ReactNode } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "../../lib/utils";
import { Input } from "../ui/input";
import {
  ALL_FILTER,
  SORT_OPTIONS,
  type RunsFilter,
  type RunsSortKey,
} from "../../lib/runs-view";

export interface RunsFilterOptions {
  statuses: string[];
  strategies: string[];
}

function Select({
  value,
  onChange,
  children,
  ariaLabel,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <div className={cn("relative", className)}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={ariaLabel}
        className="h-8 w-full cursor-pointer appearance-none rounded border border-border bg-bg-1 pr-7 pl-2.5 text-xs text-fg-0 focus-visible:border-accent focus-visible:ring-1 focus-visible:ring-accent focus-visible:outline-none"
      >
        {children}
      </select>
      <ChevronDown className="pointer-events-none absolute top-1/2 right-2 size-3 -translate-y-1/2 text-fg-2" />
    </div>
  );
}

interface RunsFilterControlsProps {
  filter: RunsFilter;
  onFilterChange: (patch: Partial<RunsFilter>) => void;
  options: RunsFilterOptions;
  sort: RunsSortKey;
  onSortChange: (sort: RunsSortKey) => void;
}

/** Shared filter controls — rendered inline on desktop, inside a Sheet on mobile. */
export function RunsFilterControls({
  filter,
  onFilterChange,
  options,
  sort,
  onSortChange,
}: RunsFilterControlsProps) {
  return (
    <div className="flex flex-col gap-2">
      <Input
        value={filter.search}
        onChange={(e) => onFilterChange({ search: e.target.value })}
        placeholder="Search runs…"
        aria-label="Search runs"
        autoComplete="off"
        spellCheck={false}
        className="font-mono text-xs"
      />
      <div className="flex flex-col gap-2 md:flex-row">
        <Select
          value={filter.status}
          onChange={(status) => onFilterChange({ status })}
          ariaLabel="Filter by status"
          className="flex-1"
        >
          <option value={ALL_FILTER}>All statuses</option>
          {options.statuses.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </Select>
        <Select
          value={filter.strategy}
          onChange={(strategy) => onFilterChange({ strategy })}
          ariaLabel="Filter by strategy"
          className="flex-1"
        >
          <option value={ALL_FILTER}>All strategies</option>
          {options.strategies.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </Select>
        <Select
          value={sort}
          onChange={(value) => onSortChange(value as RunsSortKey)}
          ariaLabel="Sort runs"
          className="flex-1"
        >
          {SORT_OPTIONS.map(({ key, label }) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </Select>
      </div>
    </div>
  );
}
