import { ApiError } from "../api/client";

/**
 * Map a control-mutation `ApiError` onto UI copy. Pure and unit-testable.
 *
 * - "conflict" (409 state races): a concise developer message; the caller
 *   should ALSO silently refetch the run family — state changed under us.
 * - "idempotency" (409 idempotency_key_conflict): a client bug; surface it,
 *   never auto-retry with a fresh key.
 * - "fatal": anything else, surfaced with the server message verbatim.
 */

export type ControlErrorKind = "conflict" | "idempotency" | "fatal";

export interface ControlErrorMessage {
  kind: ControlErrorKind;
  message: string;
}

export const CONFLICT_ERROR_CODES = new Set([
  "invalid_run_transition",
  "checkpoint_conflict",
  "interrupt_already_resolved",
  "operation_in_progress",
]);

const CONFLICT_MESSAGES: Record<string, string> = {
  invalid_run_transition: "Run state changed before this operation completed.",
  checkpoint_conflict: "Run was updated concurrently. Refreshed to the latest state.",
  interrupt_already_resolved: "This approval request has already been resolved.",
  operation_in_progress: "Another control operation is already in progress.",
};

export function controlErrorMessage(error: ApiError): ControlErrorMessage {
  if (error.status === 409) {
    if (error.code === "idempotency_key_conflict") {
      return {
        kind: "idempotency",
        message:
          "Idempotency key conflict — the same key was reused with a different request.",
      };
    }
    if (error.code !== undefined && CONFLICT_MESSAGES[error.code] !== undefined) {
      return { kind: "conflict", message: CONFLICT_MESSAGES[error.code] };
    }
  }
  return { kind: "fatal", message: error.message };
}
