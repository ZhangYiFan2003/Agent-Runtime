import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "./button";

/** Generic text copy button (output blocks, attribute JSON). */
export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* clipboard unavailable — nothing to copy visually */
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1200);
  }

  return (
    <Button variant="ghost" size="sm" onClick={() => void copy()} aria-label={label}>
      {copied ? <Check className="text-accent" /> : <Copy />}
      {copied ? "Copied" : label}
    </Button>
  );
}
