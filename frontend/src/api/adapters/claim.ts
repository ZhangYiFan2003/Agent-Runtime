import type { ClaimDto, EvidenceDto } from "../dto/claim";

export interface Claim {
  claimId: string;
  runId: string;
  threadId: string;
  turnId: string;
  text: string;
  claimKind: string | null;
  evidenceCount: number;
  citationMarker: string;
  integrity: string;
  createdAt: string;
}

export interface Evidence {
  evidenceId: string;
  sourceType: string;
  sourceId: string;
  sourceRunId: string;
  relation: string;
  summary: string;
  locator: Record<string, unknown>;
  sourceDigest: string | null;
  integrity: string;
  createdAt: string;
}

export function adaptClaim(dto: ClaimDto): Claim {
  return {
    claimId: dto.claim_id,
    runId: dto.run_id,
    threadId: dto.thread_id,
    turnId: dto.turn_id,
    text: dto.text,
    claimKind: dto.claim_kind,
    evidenceCount: dto.evidence_count,
    citationMarker: dto.citation_marker,
    integrity: dto.integrity,
    createdAt: dto.created_at,
  };
}

export function adaptEvidence(dto: EvidenceDto): Evidence {
  return {
    evidenceId: dto.evidence_id,
    sourceType: dto.source_type,
    sourceId: dto.source_id,
    sourceRunId: dto.source_run_id,
    relation: dto.relation,
    summary: dto.summary,
    locator: dto.locator,
    sourceDigest: dto.source_digest,
    integrity: dto.integrity,
    createdAt: dto.created_at,
  };
}
