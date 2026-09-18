import type { RunView } from "../../api/adapters/run";
import { deriveRunBanners } from "../../lib/run-detail-view";
import { cn } from "../../lib/utils";

/** Restrained waiting/recovery banner stack — warn hairline for approvals,
 *  info for everything else. Read-only: no approve/reject affordances. */
export function RunBanners({ run }: { run: RunView }) {
  const banners = deriveRunBanners(run);
  if (banners.length === 0) return null;
  return (
    <div className="shrink-0 border-b border-border">
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
