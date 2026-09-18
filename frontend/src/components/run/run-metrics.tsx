import type { RunMetrics } from "../../api/adapters/metrics";
import { groupMetrics } from "../../lib/run-detail-view";
import { DefinitionList } from "./definition-list";

/** Metrics tab — grouped definition lists from the real RunMetrics fields. */
export function RunMetricsPanel({ metrics }: { metrics: RunMetrics }) {
  const sections = groupMetrics(metrics).map((group) => ({
    title: group.title,
    rows: group.entries,
  }));
  return <DefinitionList sections={sections} />;
}
