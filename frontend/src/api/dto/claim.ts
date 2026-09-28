import { z } from "zod";

export const claimSchema = z
  .object({
    claim_id: z.string(),
    run_id: z.string(),
    thread_id: z.string(),
    turn_id: z.string(),
    text: z.string(),
    claim_kind: z.string().nullable(),
    evidence_count: z.number().int().nonnegative(),
    citation_marker: z.string(),
    integrity: z.string(),
    created_at: z.string(),
  })
  .catchall(z.unknown());

export const runClaimsSchema = z
  .object({
    run_id: z.string(),
    claims: z.array(claimSchema),
  })
  .catchall(z.unknown());

export const evidenceSchema = z
  .object({
    evidence_id: z.string(),
    source_type: z.string(),
    source_id: z.string(),
    source_run_id: z.string(),
    relation: z.string(),
    summary: z.string(),
    locator: z.record(z.string(), z.unknown()),
    source_digest: z.string().nullable(),
    integrity: z.string(),
    created_at: z.string(),
  })
  .catchall(z.unknown());

export const claimProvenanceSchema = z
  .object({
    claim: claimSchema,
    evidence: z.array(evidenceSchema),
  })
  .catchall(z.unknown());

export type ClaimDto = z.infer<typeof claimSchema>;
export type EvidenceDto = z.infer<typeof evidenceSchema>;
