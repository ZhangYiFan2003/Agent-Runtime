import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { executeControlMutation, mintIdempotencyKey } from "./idempotency";

describe("mintIdempotencyKey", () => {
  it("mints unique UUID-shaped keys", () => {
    const a = mintIdempotencyKey();
    const b = mintIdempotencyKey();
    expect(a).not.toBe(b);
    expect(a).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  });
});

describe("executeControlMutation", () => {
  it("succeeds on the first attempt without sleeping", async () => {
    const sleep = vi.fn(async () => {});
    const attempt = vi.fn(async () => "ok");
    await expect(
      executeControlMutation(attempt, { sleep, retryDelayMs: 300 }),
    ).resolves.toBe("ok");
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it("retries a transport failure (status 0) with the same caller context", async () => {
    const sleep = vi.fn(async () => {});
    const seenKeys: string[] = [];
    let calls = 0;
    const attempt = async () => {
      calls += 1;
      seenKeys.push("caller-minted-key"); // the caller mints once, we reuse it
      if (calls === 1) throw new ApiError({ status: 0, message: "network down" });
      return "ok";
    };
    await expect(
      executeControlMutation(attempt, { sleep, retryDelayMs: 300 }),
    ).resolves.toBe("ok");
    expect(calls).toBe(2);
    expect(seenKeys).toEqual(["caller-minted-key", "caller-minted-key"]);
    expect(sleep).toHaveBeenCalledTimes(1);
    expect(sleep).toHaveBeenCalledWith(300);
  });

  it("does NOT retry after a received HTTP 500", async () => {
    const sleep = vi.fn(async () => {});
    const attempt = vi.fn(async () => {
      throw new ApiError({ status: 500, message: "server exploded" });
    });
    await expect(executeControlMutation(attempt, { sleep })).rejects.toMatchObject({
      status: 500,
    });
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it("does NOT retry a 409 idempotency_key_conflict", async () => {
    const sleep = vi.fn(async () => {});
    const attempt = vi.fn(async () => {
      throw new ApiError({ status: 409, code: "idempotency_key_conflict", message: "conflict" });
    });
    await expect(executeControlMutation(attempt, { sleep })).rejects.toMatchObject({
      status: 409,
      code: "idempotency_key_conflict",
    });
    expect(attempt).toHaveBeenCalledTimes(1);
    expect(sleep).not.toHaveBeenCalled();
  });

  it("gives up after maxAttempts transport failures", async () => {
    const sleep = vi.fn(async () => {});
    const attempt = vi.fn(async () => {
      throw new ApiError({ status: 0, message: "offline" });
    });
    await expect(
      executeControlMutation(attempt, { sleep, maxAttempts: 2, retryDelayMs: 300 }),
    ).rejects.toMatchObject({ status: 0 });
    expect(attempt).toHaveBeenCalledTimes(2);
    expect(sleep).toHaveBeenCalledTimes(1);
  });

  it("does not retry non-ApiError transport exceptions", async () => {
    const sleep = vi.fn(async () => {});
    const attempt = vi.fn(async () => {
      throw new TypeError("fetch failed");
    });
    await expect(executeControlMutation(attempt, { sleep })).rejects.toBeInstanceOf(TypeError);
    expect(attempt).toHaveBeenCalledTimes(1);
  });
});
