import { useState } from "react";
import { SendHorizontal } from "lucide-react";
import type { SendOutcome } from "../../queries/use-workbench";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { Textarea } from "../ui/textarea";

/**
 * Turn composer. Enter sends, Shift+Enter inserts a newline. The draft is
 * component-local state only — never persisted. On an explicit HTTP failure
 * the draft is restored; on a lost connection the pending echo stays in the
 * feed instead (the run may still be executing).
 */
export function Composer({
  disabled,
  disabledReason,
  submitting,
  autoFocus = false,
  hero = false,
  onSend,
}: {
  disabled: boolean;
  disabledReason: string | null;
  submitting: boolean;
  autoFocus?: boolean;
  /** Larger empty-state variant. */
  hero?: boolean;
  onSend: (text: string) => Promise<SendOutcome>;
}) {
  const [draft, setDraft] = useState("");
  const trimmed = draft.trim();
  const canSend = !disabled && trimmed !== "";

  const send = async () => {
    if (!canSend) return;
    const text = trimmed;
    setDraft("");
    const outcome = await onSend(text);
    if (outcome === "failed") setDraft(text);
  };

  return (
    <div className={cn(hero ? "w-full" : "")}>
      <Textarea
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            void send();
          }
        }}
        disabled={disabled}
        autoFocus={autoFocus}
        rows={hero ? 3 : 2}
        aria-label="Message"
        placeholder={
          disabled && disabledReason !== null
            ? disabledReason
            : "What do you want Axiom to do?"
        }
        className={cn("max-h-40 resize-none overflow-y-auto", hero && "min-h-24")}
      />
      <div className="mt-1.5 flex items-center justify-between gap-3">
        <span className="min-w-0 truncate font-mono text-[10px] text-fg-2">
          {submitting ? "Submitting turn…" : (disabledReason ?? "Enter to send · Shift+Enter for newline")}
        </span>
        <Button
          variant="primary"
          size="sm"
          disabled={!canSend}
          onClick={() => void send()}
          aria-label="Send message"
        >
          <SendHorizontal />
          {submitting ? "Sending…" : "Send"}
        </Button>
      </div>
    </div>
  );
}
