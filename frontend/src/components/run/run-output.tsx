import type { RunView } from "../../api/adapters/run";
import { outputState } from "../../lib/run-detail-view";
import { CopyButton } from "../ui/copy-button";

/** Output tab — final output for completed runs, error detail for failed
 *  runs, neutral empty state otherwise. Whitespace preserved, mono. */
export function RunOutput({ run }: { run: RunView }) {
  const state = outputState(run);

  if (state.kind === "empty") {
    return (
      <div className="px-6 py-16 text-center">
        <div className="text-sm font-medium text-fg-0">No output available</div>
        <div className="mt-1 font-mono text-xs text-fg-2">
          Output appears when the run completes.
        </div>
      </div>
    );
  }

  if (state.kind === "error") {
    const text = `${state.errorType}: ${state.message}${state.step !== null ? `\nstep: ${state.step}` : ""}`;
    return (
      <div className="flex flex-col">
        <div className="flex items-center justify-between border-b border-border/60 px-4 py-1.5">
          <span className="text-[11px] font-medium tracking-wider text-danger uppercase">
            Error · {state.errorType}
          </span>
          <CopyButton text={text} />
        </div>
        <pre className="px-4 py-3 font-mono text-xs break-words whitespace-pre-wrap text-fg-1">
          {state.message}
          {state.step !== null && <span className="block pt-2 text-fg-2">step: {state.step}</span>}
        </pre>
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-1.5">
        <span className="text-[11px] font-medium tracking-wider text-fg-2 uppercase">Output</span>
        <CopyButton text={state.text} />
      </div>
      <pre className="px-4 py-3 font-mono text-xs break-words whitespace-pre-wrap text-fg-1">
        {state.text}
      </pre>
    </div>
  );
}
