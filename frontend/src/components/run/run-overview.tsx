import type { RunView } from "../../api/adapters/run";
import type { RunMetrics } from "../../api/adapters/metrics";
import { buildOverviewSections } from "../../lib/run-detail-view";
import { DefinitionList } from "./definition-list";

/** Overview tab — compact definition rows, no KPI cards, no charts. */
export function RunOverview({ run, metrics }: { run: RunView; metrics: RunMetrics | null }) {
  return <DefinitionList sections={buildOverviewSections(run, metrics)} />;
}
