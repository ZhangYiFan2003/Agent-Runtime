import type { DefSection } from "../../lib/run-detail-view";

/** Compact definition-row sections — the shared rendering for the Overview
 *  and Metrics tabs (values pre-formatted by the view-model layer). */
export function DefinitionList({ sections }: { sections: DefSection[] }) {
  return (
    <div className="flex flex-col">
      {sections.map((section) => (
        <section key={section.title} className="border-b border-border/60 last:border-b-0">
          <h3 className="px-4 pt-3 pb-1 text-[11px] font-medium tracking-wider text-fg-2 uppercase">
            {section.title}
          </h3>
          <dl className="px-4 pb-3">
            {section.rows.map((row) => (
              <div key={row.label} className="flex items-baseline justify-between gap-4 py-0.5">
                <dt className="shrink-0 text-13 text-fg-2">{row.label}</dt>
                <dd className="min-w-0 text-right font-mono text-xs break-all text-fg-0">
                  {row.value}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
    </div>
  );
}
