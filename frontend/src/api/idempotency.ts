import { ApiError } from "./client";

/**
 * Idempotency-key minting and mutation retry policy.
 *
 * One user intention = one key: the caller mints a key per click and reuses
 * that exact key across network-level retries of the same click. After any
 * definitive HTTP response arrives the intention is complete; the next click
 * mints a new key. Keys live only for the in-flight request lifetime — no
 * framework, no persistence.
 */

/* ------------------------------------------------------------------ */
/* Key minting                                                         */
/* ------------------------------------------------------------------ */

function randomHex(bytes: number): string {
  const alphabet = "0123456789abcdef";
  let out = "";
  const cryptoObj: Crypto | undefined =
    typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  if (cryptoObj?.getRandomValues) {
    const buf = new Uint8Array(bytes);
    cryptoObj.getRandomValues(buf);
    for (const b of buf) out += alphabet[b >> 4] + alphabet[b & 0x0f];
  } else {
    for (let i = 0; i < bytes * 2; i += 1) out += alphabet[Math.floor(Math.random() * 16)];
  }
  return out;
}

/** Fresh idempotency key (UUIDv4 shape). Never reused across intentions. */
export function mintIdempotencyKey(): string {
  const cryptoObj: Crypto | undefined =
    typeof globalThis !== "undefined" ? globalThis.crypto : undefined;
  if (typeof cryptoObj?.randomUUID === "function") return cryptoObj.randomUUID();
  // Fallback for non-secure contexts without randomUUID — still unique enough
  // for a per-click key (122 bits of randomness).
  return `${randomHex(4)}-${randomHex(2)}-4${randomHex(1).slice(1)}-${randomHex(2)}-${randomHex(6)}`;
}

/* ------------------------------------------------------------------ */
/* Mutation retry policy                                               */
/* ------------------------------------------------------------------ */

export interface ControlMutationOptions {
  /** Total attempts including the first. Transport failures only. Default 2. */
  maxAttempts?: number;
  /** Delay between transport-failure retries. Default 300 ms. */
  retryDelayMs?: number;
  /** Injectable sleeper for tests. */
  sleep?: (ms: number) => Promise<void>;
}

export const CONTROL_MUTATION_MAX_ATTEMPTS = 2;
export const CONTROL_MUTATION_RETRY_DELAY_MS = 300;

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Execute one control mutation with the retry policy from FRONTEND_PLAN J.2:
 * auto-retry ONLY on transport failure (`ApiError` with `status === 0`), at
 * most `maxAttempts` times, with a small delay between attempts. Never
 * retries after a received HTTP status (4xx/5xx) — the backend has durable
 * identity (run_id + key), so replaying a key the server already saw is
 * safe, but reacting to a real response is the caller's job.
 *
 * The `attempt` callback receives the SAME caller-minted key on every
 * attempt; this helper never mints keys itself.
 */
export async function executeControlMutation<T>(
  attempt: () => Promise<T>,
  options: ControlMutationOptions = {},
): Promise<T> {
  const maxAttempts = options.maxAttempts ?? CONTROL_MUTATION_MAX_ATTEMPTS;
  const retryDelayMs = options.retryDelayMs ?? CONTROL_MUTATION_RETRY_DELAY_MS;
  const sleep = options.sleep ?? defaultSleep;

  let lastError: unknown = null;
  for (let i = 0; i < maxAttempts; i += 1) {
    try {
      return await attempt();
    } catch (error) {
      lastError = error;
      const transportFailure = error instanceof ApiError && error.status === 0;
      if (!transportFailure || i === maxAttempts - 1) throw error;
      await sleep(retryDelayMs);
    }
  }
  throw lastError instanceof Error ? lastError : new Error("Control mutation failed");
}
