import { useState } from "react";
import { FolderSearch, Plus, X } from "lucide-react";
import type { ThreadRegistryEntry } from "../../lib/thread-registry";
import { formatRelativeTime, truncateId } from "../../lib/format";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "../ui/dialog";
import { Input } from "../ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

/**
 * Local thread list (registry-backed, metadata only). "Remove" deletes only
 * the local entry — the runtime thread is untouched, and there is no list
 * endpoint on the runtime, so this rail is the only history.
 */
export function ThreadRail({
  threads,
  selectedThreadId,
  creating,
  createError,
  onCreate,
  onSelect,
  onRemove,
  onOpenById,
}: {
  threads: ThreadRegistryEntry[];
  selectedThreadId: string | null;
  creating: boolean;
  createError: string | null;
  onCreate: () => void;
  onSelect: (threadId: string) => void;
  onRemove: (threadId: string) => void;
  onOpenById: (threadId: string) => void;
}) {
  const [openDialog, setOpenDialog] = useState(false);
  const [idDraft, setIdDraft] = useState("");

  const submitOpenById = () => {
    const id = idDraft.trim();
    if (id === "") return;
    onOpenById(id);
    setIdDraft("");
    setOpenDialog(false);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b border-border/60 px-3 py-2">
        <span className="text-[11px] font-medium tracking-wider text-fg-2 uppercase">Threads</span>
        <Button variant="default" size="sm" onClick={onCreate} disabled={creating}>
          <Plus />
          {creating ? "Creating…" : "New"}
        </Button>
      </div>

      {createError !== null && (
        <div role="alert" className="shrink-0 border-b border-border/60 px-3 py-1.5 text-xs text-danger">
          {createError}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {threads.length === 0 ? (
          <div className="px-4 py-8 text-center">
            <div className="text-xs text-fg-2">No threads on this device yet.</div>
            <div className="mt-1 font-mono text-[11px] text-fg-2">
              The runtime has no thread list — IDs you open are kept locally.
            </div>
          </div>
        ) : (
          <ul className="divide-y divide-border/60">
            {threads.map((entry) => {
              const selected = entry.threadId === selectedThreadId;
              return (
                <li key={entry.threadId} className="group relative">
                  <div
                    className={cn(
                      "flex w-full items-center gap-1 pr-1",
                      selected ? "bg-bg-3" : "hover:bg-bg-2",
                    )}
                  >
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          onClick={() => onSelect(entry.threadId)}
                          aria-current={selected}
                          className={cn(
                            "min-w-0 flex-1 px-3 py-2 text-left focus-visible:outline-none",
                            selected && "shadow-[inset_2px_0_0_0_var(--color-accent)]",
                          )}
                        >
                          <div
                            className={cn(
                              "truncate font-mono text-xs",
                              selected ? "text-fg-0" : "text-fg-1",
                            )}
                          >
                            {truncateId(entry.threadId, 22)}
                          </div>
                          <div className="mt-0.5 font-mono text-[10px] text-fg-2">
                            opened {formatRelativeTime(new Date(entry.lastOpenedAt).toISOString())}
                          </div>
                        </button>
                      </TooltipTrigger>
                      <TooltipContent side="right" className="font-mono text-[11px]">
                        {entry.threadId}
                      </TooltipContent>
                    </Tooltip>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          onClick={() => onRemove(entry.threadId)}
                          aria-label={`Remove ${entry.threadId} from this list`}
                          className="shrink-0 rounded p-1 text-fg-2 opacity-0 transition-opacity group-hover:opacity-100 hover:bg-bg-1 hover:text-fg-0 focus-visible:opacity-100"
                        >
                          <X className="size-3" />
                        </button>
                      </TooltipTrigger>
                      <TooltipContent side="right">
                        Remove from this list — the runtime thread is not deleted
                      </TooltipContent>
                    </Tooltip>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="shrink-0 border-t border-border/60 p-2">
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-start"
          onClick={() => setOpenDialog(true)}
        >
          <FolderSearch />
          Open by ID…
        </Button>
      </div>

      <Dialog open={openDialog} onOpenChange={setOpenDialog}>
        <DialogContent>
          <DialogTitle>Open thread by ID</DialogTitle>
          <DialogDescription className="mt-1.5 text-13 text-fg-1">
            The runtime has no thread list endpoint — enter a thread ID directly.
          </DialogDescription>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              submitOpenById();
            }}
          >
            <Input
              value={idDraft}
              onChange={(event) => setIdDraft(event.target.value)}
              placeholder="thread_…"
              aria-label="Thread ID"
              className="mt-3 font-mono"
              autoFocus
            />
            <div className="mt-4 flex justify-end gap-2">
              <Button
                variant="default"
                size="sm"
                type="button"
                onClick={() => setOpenDialog(false)}
              >
                Cancel
              </Button>
              <Button variant="primary" size="sm" type="submit" disabled={idDraft.trim() === ""}>
                Open
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
