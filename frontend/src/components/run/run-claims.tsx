import { useState } from "react";
import type { Claim, Evidence } from "../../api/adapters/claim";
import type { ClaimProvenance } from "../../api/run";
import { fetchArtifactContent } from "../../api/run";
import { getConnection } from "../../lib/connection";
import { formatRelativeTime, truncateId } from "../../lib/format";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import { CopyId } from "./copy-id";

function integrityTone(integrity: string): string {
  return integrity === "VALID" ? "text-accent" : "text-warn";
}

function locatorString(evidence: Evidence, key: string): string | null {
  const value = evidence.locator[key];
  return typeof value === "string" && value !== "" ? value : null;
}

function CodeEvidence({ evidence }: { evidence: Evidence }) {
  const path = locatorString(evidence, "path");
  const excerpt = locatorString(evidence, "excerpt");
  const start = evidence.locator.start_line;
  const end = evidence.locator.end_line;
  return (
    <div className="mt-2">
      {path !== null && (
        <div className="font-mono text-[11px] text-fg-2">
          {path}
          {typeof start === "number" && typeof end === "number" ? `:${start}-${end}` : ""}
        </div>
      )}
      {excerpt !== null && (
        <pre className="mt-1 max-h-64 overflow-auto rounded border border-border/60 bg-bg-0 p-2 font-mono text-[11px] whitespace-pre text-fg-1">
          {excerpt}
        </pre>
      )}
    </div>
  );
}

function EvidenceRow({
  evidence,
  onOpenSpan,
}: {
  evidence: Evidence;
  onOpenSpan: (spanId: string) => void;
}) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const artifactId = locatorString(evidence, "artifact_id");
  const artifactName = locatorString(evidence, "name") ?? "artifact";
  const spanId = locatorString(evidence, "span_id");

  const download = async () => {
    if (artifactId === null) return;
    setDownloading(true);
    setError(null);
    try {
      const blob = await fetchArtifactContent(getConnection(), artifactId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifactName;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="border-b border-border/60 px-4 py-3 last:border-b-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono text-[10px] tracking-wider text-fg-2 uppercase">
          {evidence.sourceType.replaceAll("_", " ")}
        </span>
        <span className={cn("font-mono text-[10px]", integrityTone(evidence.integrity))}>
          {evidence.integrity.toLowerCase().replaceAll("_", " ")}
        </span>
        <span className="font-mono text-[10px] text-fg-2">{evidence.relation}</span>
      </div>
      <div className="mt-1 text-13 text-fg-0">{evidence.summary}</div>
      <div className="mt-1 flex min-w-0 items-center gap-2 font-mono text-[11px] text-fg-2">
        <span className="truncate" title={evidence.sourceId}>
          {truncateId(evidence.sourceId, 22)}
        </span>
        <CopyId id={evidence.sourceId} head={0} />
      </div>
      {evidence.sourceDigest !== null && (
        <div className="mt-1 font-mono text-[10px] text-fg-2" title={evidence.sourceDigest}>
          sha256 {truncateId(evidence.sourceDigest, 16)}
        </div>
      )}
      {evidence.sourceType === "CODE_LOCATION" && <CodeEvidence evidence={evidence} />}
      <div className="mt-2 flex items-center gap-2">
        {evidence.sourceType === "TRACE_SPAN" && spanId !== null && (
          <Button size="sm" variant="default" onClick={() => onOpenSpan(spanId)}>
            Open span
          </Button>
        )}
        {evidence.sourceType === "ARTIFACT" && artifactId !== null && (
          <Button size="sm" variant="default" disabled={downloading} onClick={() => void download()}>
            {downloading ? "Downloading" : "Download artifact"}
          </Button>
        )}
      </div>
      {error !== null && <div className="mt-1 text-xs text-danger">{error}</div>}
    </div>
  );
}

function ClaimRow({
  claim,
  selected,
  onSelect,
}: {
  claim: Claim;
  selected: boolean;
  onSelect: (claimId: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(claim.claimId)}
      className={cn(
        "w-full border-b border-border/60 px-4 py-3 text-left last:border-b-0 hover:bg-bg-2/50",
        selected && "bg-bg-2",
      )}
    >
      <div className="text-13 leading-5 break-words text-fg-0">{claim.text}</div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px] text-fg-2">
        <span>{claim.evidenceCount} evidence</span>
        <span className={integrityTone(claim.integrity)}>{claim.integrity.toLowerCase()}</span>
        {claim.claimKind !== null && <span>{claim.claimKind}</span>}
        <span>{formatRelativeTime(claim.createdAt)}</span>
      </div>
      <div className="mt-1 font-mono text-[11px] text-info">{claim.citationMarker}</div>
    </button>
  );
}

export function RunClaimsPanel({
  claims,
  selectedClaimId,
  provenance,
  provenancePending,
  provenanceError,
  onSelectClaim,
  onOpenSpan,
}: {
  claims: Claim[];
  selectedClaimId: string | null;
  provenance: ClaimProvenance | undefined;
  provenancePending: boolean;
  provenanceError: boolean;
  onSelectClaim: (claimId: string) => void;
  onOpenSpan: (spanId: string) => void;
}) {
  if (claims.length === 0) {
    return (
      <div className="px-6 py-16 text-center">
        <div className="text-sm font-medium text-fg-0">No claims</div>
        <div className="mt-1 font-mono text-xs text-fg-2">
          This run did not record evidence-backed claims.
        </div>
      </div>
    );
  }
  return (
    <div className="grid min-h-full md:grid-cols-[minmax(240px,0.8fr)_minmax(320px,1.2fr)]">
      <div className="border-border md:border-r">
        {claims.map((claim) => (
          <ClaimRow
            key={claim.claimId}
            claim={claim}
            selected={claim.claimId === selectedClaimId}
            onSelect={onSelectClaim}
          />
        ))}
      </div>
      <div>
        {selectedClaimId === null ? (
          <div className="px-6 py-16 text-center font-mono text-xs text-fg-2">
            Select a claim to inspect its evidence.
          </div>
        ) : provenancePending ? (
          <div className="px-6 py-16 text-center font-mono text-xs text-fg-2">Loading evidence…</div>
        ) : provenanceError || provenance === undefined ? (
          <div className="px-6 py-16 text-center text-xs text-danger">Evidence could not be loaded.</div>
        ) : (
          <div>
            <div className="border-b border-border px-4 py-3">
              <div className="text-sm leading-6 break-words text-fg-0">{provenance.claim.text}</div>
              <div className="mt-1 flex items-center gap-2 font-mono text-[11px] text-fg-2">
                <span>{provenance.claim.citationMarker}</span>
                <CopyId id={provenance.claim.claimId} head={0} />
              </div>
            </div>
            {provenance.evidence.map((evidence) => (
              <EvidenceRow key={evidence.evidenceId} evidence={evidence} onOpenSpan={onOpenSpan} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
