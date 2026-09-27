import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Artifact } from "../../api/adapters/artifact";
import { TooltipProvider } from "../ui/tooltip";
import { RunArtifactsTable } from "./run-artifacts";

const artifact: Artifact = {
  artifactId: "art_123456789",
  sha256: "a".repeat(64),
  runId: "run_1",
  threadId: "thread_1",
  invocationId: "run_1:call_1",
  toolName: "publish_artifact",
  name: "report.txt",
  mediaType: "text/plain",
  sizeBytes: 1536,
  kind: "file",
  reusedFromArtifactId: null,
  reused: false,
  createdAt: "2026-09-27T09:00:00+00:00",
  metadata: {},
};

describe("RunArtifactsTable", () => {
  it("renders the compact artifact projection", () => {
    const html = renderToStaticMarkup(
      createElement(
        TooltipProvider,
        { children: createElement(RunArtifactsTable, { artifacts: [artifact] }) },
      ),
    );
    expect(html).toContain("report.txt");
    expect(html).toContain("text/plain");
    expect(html).toContain("1.5 KiB");
    expect(html).toContain("publish_artifact");
    expect(html).toContain("Download");
  });

  it("renders a direct empty state", () => {
    const html = renderToStaticMarkup(createElement(RunArtifactsTable, { artifacts: [] }));
    expect(html).toContain("No artifacts");
    expect(html).toContain("No artifacts produced by this run.");
  });
});
