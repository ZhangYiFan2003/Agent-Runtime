import { apiGet, apiGetBlob, ApiError } from "./client";
import { runArtifactsSchema } from "./dto/artifact";
import { childrenResponseSchema, runViewSchema } from "./dto/run";
import { traceResponseSchema } from "./dto/trace";
import { runMetricsSchema } from "./dto/metrics";
import { adaptChildRun, adaptRunView, type ChildRun, type RunView } from "./adapters/run";
import { adaptSpan, adaptTrace, type Span, type Trace } from "./adapters/trace";
import { adaptRunMetrics, type RunMetrics } from "./adapters/metrics";
import type { ConnectionConfig } from "../lib/connection";
import { adaptArtifact, type Artifact } from "./adapters/artifact";
import { claimProvenanceSchema, runClaimsSchema } from "./dto/claim";
import { adaptClaim, adaptEvidence, type Claim, type Evidence } from "./adapters/claim";

export interface RunChildren {
  parentRunId: string;
  children: ChildRun[];
}

export interface RunTrace {
  trace: Trace;
  spans: Span[];
}

export interface RunArtifacts {
  runId: string;
  artifacts: Artifact[];
}

export interface RunClaims {
  runId: string;
  claims: Claim[];
}

export interface ClaimProvenance {
  claim: Claim;
  evidence: Evidence[];
}

export async function fetchRunClaims(
  config: ConnectionConfig,
  runId: string,
  fetchImpl?: typeof fetch,
): Promise<RunClaims> {
  const raw: unknown = await apiGet(`/v1/runs/${encodeURIComponent(runId)}/claims`, {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    fetchImpl,
  });
  const dto = runClaimsSchema.parse(raw);
  return { runId: dto.run_id, claims: dto.claims.map(adaptClaim) };
}

export async function fetchClaimProvenance(
  config: ConnectionConfig,
  claimId: string,
  fetchImpl?: typeof fetch,
): Promise<ClaimProvenance> {
  const raw: unknown = await apiGet(
    `/v1/claims/${encodeURIComponent(claimId)}/provenance`,
    { baseUrl: config.baseUrl, apiKey: config.apiKey, fetchImpl },
  );
  const dto = claimProvenanceSchema.parse(raw);
  return { claim: adaptClaim(dto.claim), evidence: dto.evidence.map(adaptEvidence) };
}

export async function fetchRunArtifacts(
  config: ConnectionConfig,
  runId: string,
  fetchImpl?: typeof fetch,
): Promise<RunArtifacts> {
  const raw: unknown = await apiGet(`/v1/runs/${encodeURIComponent(runId)}/artifacts`, {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    fetchImpl,
  });
  const dto = runArtifactsSchema.parse(raw);
  return { runId: dto.run_id, artifacts: dto.artifacts.map(adaptArtifact) };
}

export function fetchArtifactContent(
  config: ConnectionConfig,
  artifactId: string,
  fetchImpl?: typeof fetch,
): Promise<Blob> {
  return apiGetBlob(`/v1/artifacts/${encodeURIComponent(artifactId)}/content`, {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    fetchImpl,
  });
}

/** Fetches and parses `GET /v1/runs/{id}` → adapted RunView. Throws ApiError (404 = no such run). */
export async function fetchRun(
  config: ConnectionConfig,
  runId: string,
  fetchImpl?: typeof fetch,
): Promise<RunView> {
  const raw: unknown = await apiGet(`/v1/runs/${encodeURIComponent(runId)}`, {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    fetchImpl,
  });
  return adaptRunView(runViewSchema.parse(raw));
}

/** Fetches and parses `GET /v1/runs/{id}/children`. Throws ApiError. */
export async function fetchRunChildren(
  config: ConnectionConfig,
  runId: string,
  fetchImpl?: typeof fetch,
): Promise<RunChildren> {
  const raw: unknown = await apiGet(`/v1/runs/${encodeURIComponent(runId)}/children`, {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    fetchImpl,
  });
  const dto = childrenResponseSchema.parse(raw);
  return { parentRunId: dto.parent_run_id, children: dto.children.map(adaptChildRun) };
}

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/**
 * Fetches and parses `GET /v1/runs/{id}/trace`.
 * Returns null when the run has no trace yet (404) — a normal state for a
 * just-started run, not an error. Other failures throw ApiError.
 */
export async function fetchRunTrace(
  config: ConnectionConfig,
  runId: string,
  fetchImpl?: typeof fetch,
): Promise<RunTrace | null> {
  let raw: unknown;
  try {
    raw = await apiGet(`/v1/runs/${encodeURIComponent(runId)}/trace`, {
      baseUrl: config.baseUrl,
      apiKey: config.apiKey,
      fetchImpl,
    });
  } catch (error) {
    if (isNotFound(error)) return null;
    throw error;
  }
  const dto = traceResponseSchema.parse(raw);
  return { trace: adaptTrace(dto.trace), spans: dto.spans.map(adaptSpan) };
}

/**
 * Fetches and parses `GET /v1/runs/{id}/metrics`.
 * Returns null when the run has no trace yet (404), same as fetchRunTrace.
 */
export async function fetchRunMetrics(
  config: ConnectionConfig,
  runId: string,
  fetchImpl?: typeof fetch,
): Promise<RunMetrics | null> {
  let raw: unknown;
  try {
    raw = await apiGet(`/v1/runs/${encodeURIComponent(runId)}/metrics`, {
      baseUrl: config.baseUrl,
      apiKey: config.apiKey,
      fetchImpl,
    });
  } catch (error) {
    if (isNotFound(error)) return null;
    throw error;
  }
  return adaptRunMetrics(runMetricsSchema.parse(raw));
}
