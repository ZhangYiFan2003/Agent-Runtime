import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { controlErrorMessage } from "./control-errors";

describe("controlErrorMessage", () => {
  it("maps invalid_run_transition to a conflict note", () => {
    const result = controlErrorMessage(
      new ApiError({ status: 409, code: "invalid_run_transition", message: "raw" }),
    );
    expect(result.kind).toBe("conflict");
    expect(result.message).toBe("Run state changed before this operation completed.");
  });

  it("maps checkpoint_conflict to a conflict note", () => {
    const result = controlErrorMessage(
      new ApiError({ status: 409, code: "checkpoint_conflict", message: "raw" }),
    );
    expect(result.kind).toBe("conflict");
    expect(result.message).toBe("Run was updated concurrently. Refreshed to the latest state.");
  });

  it("maps interrupt_already_resolved to a conflict note", () => {
    const result = controlErrorMessage(
      new ApiError({ status: 409, code: "interrupt_already_resolved", message: "raw" }),
    );
    expect(result.kind).toBe("conflict");
    expect(result.message).toBe("This approval request has already been resolved.");
  });

  it("maps operation_in_progress to a conflict note", () => {
    const result = controlErrorMessage(
      new ApiError({ status: 409, code: "operation_in_progress", message: "raw" }),
    );
    expect(result.kind).toBe("conflict");
    expect(result.message).toBe("Another control operation is already in progress.");
  });

  it("maps idempotency_key_conflict to an explicit idempotency error", () => {
    const result = controlErrorMessage(
      new ApiError({ status: 409, code: "idempotency_key_conflict", message: "raw" }),
    );
    expect(result.kind).toBe("idempotency");
    expect(result.message).toContain("Idempotency key conflict");
  });

  it("passes any other error through as fatal with the server message", () => {
    const result = controlErrorMessage(new ApiError({ status: 500, message: "boom" }));
    expect(result).toEqual({ kind: "fatal", message: "boom" });
  });

  it("treats an unknown 409 code as fatal", () => {
    const result = controlErrorMessage(
      new ApiError({ status: 409, code: "future_conflict", message: "raw" }),
    );
    expect(result).toEqual({ kind: "fatal", message: "raw" });
  });
});
