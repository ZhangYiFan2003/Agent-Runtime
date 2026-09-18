import type { ChildRun } from "../../api/adapters/run";
import { formatRelativeTime, truncateId } from "../../lib/format";
import { CopyId } from "./copy-id";
import { StatusPill } from "./status-pill";

interface RunChildrenTableProps {
  children: ChildRun[];
  onOpenRun: (runId: string) => void;
}

/** Children tab for a parent run — dense table, row click opens the child. */
export function RunChildrenTable({ children, onOpenRun }: RunChildrenTableProps) {
  if (children.length === 0) {
    return (
      <div className="px-6 py-16 text-center">
        <div className="text-sm font-medium text-fg-0">No child runs</div>
        <div className="mt-1 font-mono text-xs text-fg-2">
          Plan / multi-agent strategies spawn child runs.
        </div>
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left">
        <thead>
          <tr className="border-b border-border">
            {["Status", "Child run", "Role", "Assignment", "Attempt", "Interrupt", "Updated"].map(
              (h) => (
                <th
                  key={h}
                  scope="col"
                  className="px-3 py-2 text-[11px] font-medium tracking-wider text-fg-2 uppercase first:pl-4 last:pr-4"
                >
                  {h}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {children.map((child) => (
            <tr
              key={child.childRunId}
              tabIndex={0}
              role="link"
              aria-label={`Child run ${child.childRunId}, status ${child.status}`}
              onClick={() => onOpenRun(child.childRunId)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onOpenRun(child.childRunId);
                }
              }}
              className="h-10 cursor-pointer border-b border-border/60 transition-colors last:border-b-0 hover:bg-bg-2 focus-visible:bg-bg-3 focus-visible:outline-none"
            >
              <td className="px-3 first:pl-4">
                <StatusPill status={child.status} statusGroup={child.statusGroup} />
              </td>
              <td className="px-3">
                <div className="flex flex-col leading-tight">
                  <CopyId id={child.childRunId} />
                  <span className="font-mono text-[11px] text-fg-2">{child.runKind}</span>
                </div>
              </td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">
                {child.workerRole ?? "—"}
              </td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-1">
                {child.assignmentId !== null ? truncateId(child.assignmentId, 16) : "—"}
              </td>
              <td className="px-3 font-mono text-xs text-fg-1">{child.attempt ?? "—"}</td>
              <td className="px-3 font-mono text-xs whitespace-nowrap">
                {child.interrupt !== null ? (
                  <span className="text-warn">{child.interrupt.interruptType}</span>
                ) : (
                  <span className="text-fg-2">—</span>
                )}
              </td>
              <td className="px-3 font-mono text-xs whitespace-nowrap text-fg-2 last:pr-4">
                {formatRelativeTime(child.updatedAt)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
