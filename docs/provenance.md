# Claim Provenance

Axiom's optional provenance layer connects a material Agent conclusion to durable Runtime sources.
It answers **which existing execution records support this Claim**; it does not decide whether the
Claim is true, semantically complete, or persuasive.

## Model and persistence

```text
Claim (clm_...)
  └─ ClaimEvidence
       └─ EvidenceRecord (evd_...)
            └─ existing Runtime source
```

Claims, Evidence records, and links are one atomic bundle. The `record_claim` Tool invocation ID is
the idempotency key, so durable Tool replay returns the original Claim rather than creating a
duplicate. Metadata lives in the same SQLite or PostgreSQL authority as Runs. Evidence stores only
bounded locators, summaries, and digests; it does not copy Tool results, Artifact bytes, or full
Trace attributes.

Supported v1 source types are:

- `TOOL_EXECUTION`: durable invocation ID, Tool name/status, completion time, and Artifact IDs.
- `ARTIFACT`: logical Artifact ID and immutable Blob SHA-256 metadata. Reuse ancestry is resolved to
  a bounded chain without copying bytes.
- `CODE_LOCATION`: workspace-relative path, bounded line range/excerpt, and SHA-256 of the captured
  file bytes. A later file change is reported as `STALE_CODE_SNAPSHOT`.
- `TRACE_SPAN`: Span identity, type, name, status, timestamps, and duration. Arbitrary Span
  attributes are not copied into Evidence.

Runtime Events and retrieval chunks are not v1 Evidence sources because they do not currently have
the same stable, independently resolved Runtime lookup boundary.

## Scope and integrity

A Claim may reference sources from its own Run or descendant Runs in the same Thread. The Runtime
walks persisted `parent_run_id` lineage; an unrelated Run, a different Thread, a missing source, an
out-of-workspace Code path, or a sensitive path fails closed before the bundle is committed.

Read-time integrity is structural:

- `VALID`
- `MISSING_SOURCE`
- `INVALID_SCOPE`
- `STALE_CODE_SNAPSHOT`
- `MISSING_BLOB`

`VALID` means the referenced source still resolves within the allowed lineage and its structural
identity matches. It is not a semantic truth score.

## Recording and citations

Provenance is disabled by default. Set `AXIOM_PROVENANCE_ENABLED=true` to register `record_claim`.
When prompt guidance is enabled, the durable ReAct, Plan, and Multi-Agent paths receive one short
instruction to record material evidence-backed conclusions and include the returned marker:

```text
[claim:clm_<opaque-id>]
```

The Web Console turns markers in persisted assistant messages into links to the Run Inspector's
Claims tab. It does not infer Claims from prose or fabricate evidence IDs.

Authenticated read endpoints are:

- `GET /v1/runs/{run_id}/claims`
- `GET /v1/claims/{claim_id}`
- `GET /v1/claims/{claim_id}/provenance`

There is intentionally no public Claim mutation endpoint; the durable Tool path preserves Run,
Thread, Turn, invocation, policy, and replay semantics.

## Completion contracts

Two optional deterministic checks are available:

- `claims_have_evidence` with positive `min_claims` and `min_evidence_per_claim` values.
- `claim_citations_resolve`, optionally with `require_at_least_one: true`.

They verify durable existence, lineage, and integrity only. Existing CompletionVerifier checks and
Runs without provenance remain unchanged.

## Limits

This release has no semantic judge, truth verifier, Claim-to-Claim graph, graph visualization,
automatic citation generation, Runtime Event Evidence, retrieval-chunk Evidence, or provenance
write API. Artifact retention still governs whether referenced Blob metadata remains resolvable.
