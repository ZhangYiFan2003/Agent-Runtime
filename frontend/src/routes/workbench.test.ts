import { createElement, type ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { useWorkbench, type Workbench } from "../queries/use-workbench";
import { TooltipProvider } from "../components/ui/tooltip";
import { WorkbenchPage } from "./workbench";

vi.mock("../queries/use-workbench", () => ({ useWorkbench: vi.fn() }));
vi.mock("../components/workbench/context-panel", () => ({ RunContextPanel: () => null }));
vi.mock("@tanstack/react-router", () => ({
  Link: ({ to, children, className }: { to: string; children: ReactNode; className?: string }) =>
    createElement("a", { href: to, className }, children),
}));

function workbench(threadId: string | null): Workbench {
  return {
    threads: [], threadId, events: [], replayStatus: "connecting", replayError: null,
    items: [], run: null, activeRunId: null, runQueryPending: false,
    submission: {
      phase: "idle", threadId: null, prompt: null, errorStatus: null,
      errorMessage: null, responseText: null, confirmedRunId: null,
      confirmedTurnId: null, confirmedStatus: null, rejectedDraft: null,
    },
    composer: { disabled: false, reason: null }, connected: true,
    createError: "unauthorized", creatingThread: false,
    send: async () => "failed", selectThread: vi.fn(),
    createAndSelectThread: async () => undefined, removeThread: vi.fn(),
    dismissSubmissionNotice: vi.fn(), retrySubmission: async () => undefined,
    abandonSubmission: vi.fn(), missingRunId: null, retryRunLookup: vi.fn(),
    history: {
      messages: [], currentRun: null, loading: false, error: null,
      hasOlder: false, loadingOlder: false, olderError: null,
      loadOlder: async () => undefined, retry: vi.fn(),
    },
  };
}

describe("Workbench creation failures", () => {
  it.each([null, "thread_existing"])("shows the cause and Settings outside the closed mobile rail for %s", (threadId) => {
    vi.mocked(useWorkbench).mockReturnValue(workbench(threadId));
    const html = renderToStaticMarkup(createElement(TooltipProvider, {
      children: createElement(WorkbenchPage),
    }));
    const railEnd = html.indexOf("</aside>");
    expect(railEnd).toBeGreaterThan(0);
    expect(html.slice(0, railEnd)).not.toContain('role="alert"');
    const visibleContent = html.slice(railEnd);
    expect(visibleContent).toContain('role="alert"');
    expect(visibleContent).toContain("Could not create a thread: unauthorized");
    expect(visibleContent).toContain('href="/settings"');
    expect(html.match(/Could not create a thread/g)).toHaveLength(1);
    expect(visibleContent).toContain('aria-label="Message"');
  });
});
