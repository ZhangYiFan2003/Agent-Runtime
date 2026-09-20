import { apiPost } from "./client";
import type { ConnectionConfig } from "../lib/connection";

/**
 * Run control mutations: resume / approve / reject (one resume endpoint with
 * a decision), cancel, interrupt, requeue. Every call carries a caller-minted
 * `Idempotency-Key` (never logged). Success bodies are parsed leniently — the
 * RunView refresh comes from query invalidation, not from these responses
 * (interrupt returns `public_dict`, requeue returns 202 with a queued
 * result, resume may return 202 while waiting/queued).
 */

export interface ControlCallOptions {
  idempotencyKey: string;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export type ApprovalDecision = "approve" | "reject";

export interface ResumeBody {
  decision?: ApprovalDecision;
  invocationId?: string;
}

function controlPost(
  config: ConnectionConfig,
  path: string,
  body: unknown,
  options: ControlCallOptions,
): Promise<unknown> {
  return apiPost(path, {
    baseUrl: config.baseUrl,
    apiKey: config.apiKey,
    body,
    headers: { "Idempotency-Key": options.idempotencyKey },
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

function resumeBody({ decision, invocationId }: ResumeBody): Record<string, string> {
  const body: Record<string, string> = {};
  if (decision !== undefined) body.decision = decision;
  if (invocationId !== undefined) body.invocation_id = invocationId;
  return body;
}

/**
 * `POST /v1/runs/{run_id}/resume`. Without a decision this is a plain resume;
 * with one it resolves an approval. Approvals on child runs must target the
 * CHILD run id, never the parent by proxy. 200 and 202 both mean accepted.
 */
export async function resumeRun(
  config: ConnectionConfig,
  runId: string,
  body: ResumeBody,
  options: ControlCallOptions,
): Promise<unknown> {
  return controlPost(
    config,
    `/v1/runs/${encodeURIComponent(runId)}/resume`,
    resumeBody(body),
    options,
  );
}

/** `POST /v1/runs/{run_id}/cancel` — state-idempotent, cascades to non-terminal children. */
export async function cancelRun(
  config: ConnectionConfig,
  runId: string,
  options: ControlCallOptions,
): Promise<unknown> {
  return controlPost(config, `/v1/runs/${encodeURIComponent(runId)}/cancel`, {}, options);
}

/** `POST /v1/runs/{run_id}/interrupt` — body `{reason?}`; server defaults when omitted. */
export async function interruptRun(
  config: ConnectionConfig,
  runId: string,
  body: { reason?: string },
  options: ControlCallOptions,
): Promise<unknown> {
  const payload: Record<string, string> = {};
  if (body.reason !== undefined && body.reason !== "") payload.reason = body.reason;
  return controlPost(config, `/v1/runs/${encodeURIComponent(runId)}/interrupt`, payload, options);
}

/**
 * `POST /v1/runs/{run_id}/requeue` — redelivers a delivery-exhausted FAILED
 * run; returns 202. Shares the resume/cancel idempotency machinery.
 */
export async function requeueRun(
  config: ConnectionConfig,
  runId: string,
  options: ControlCallOptions,
): Promise<unknown> {
  return controlPost(config, `/v1/runs/${encodeURIComponent(runId)}/requeue`, {}, options);
}
