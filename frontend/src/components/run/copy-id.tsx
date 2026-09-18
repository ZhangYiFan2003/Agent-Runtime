import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { truncateId } from "../../lib/format";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

/**
 * Truncated mono ID with tooltip (full value) and click-to-copy.
 * Copies the complete ID, displays the truncated form.
 */
export function CopyId({ id, head = 14, className }: { id: string; head?: number; className?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(id);
    } catch {
      /* clipboard unavailable — tooltip still shows the full id */
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            void copy();
          }}
          className={`inline-flex cursor-pointer items-center gap-1 font-mono text-xs text-fg-0 hover:text-accent ${className ?? ""}`}
          aria-label={`${id} — click to copy`}
        >
          {copied ? <Check className="size-3 text-accent" /> : <Copy className="size-3 text-fg-2" />}
          <span>{truncateId(id, head)}</span>
        </button>
      </TooltipTrigger>
      <TooltipContent side="top" className="font-mono text-[11px]">
        {id}
      </TooltipContent>
    </Tooltip>
  );
}
