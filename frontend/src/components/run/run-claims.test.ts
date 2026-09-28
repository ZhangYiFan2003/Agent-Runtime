import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { Claim } from "../../api/adapters/claim";
import { RunClaimsPanel } from "./run-claims";

const claim: Claim = {
  claimId: "clm_abc",
  runId: "run_1",
  threadId: "thread_1",
  turnId: "turn_1",
  text: "The artifact was produced successfully.",
  claimKind: "result",
  evidenceCount: 2,
  citationMarker: "[claim:clm_abc]",
  integrity: "VALID",
  createdAt: "2026-09-27T10:00:00+00:00",
};

function render(claims: Claim[]) {
  return renderToStaticMarkup(
    createElement(RunClaimsPanel, {
      claims,
      selectedClaimId: null,
      provenance: undefined,
      provenancePending: false,
      provenanceError: false,
      onSelectClaim: () => undefined,
      onOpenSpan: () => undefined,
    }),
  );
}

describe("RunClaimsPanel", () => {
  it("renders the durable citation and evidence count", () => {
    const html = render([claim]);
    expect(html).toContain("The artifact was produced successfully.");
    expect(html).toContain("[claim:clm_abc]");
    expect(html).toContain("2 evidence");
  });

  it("renders an explicit empty state", () => {
    expect(render([])).toContain("No claims");
  });
});
