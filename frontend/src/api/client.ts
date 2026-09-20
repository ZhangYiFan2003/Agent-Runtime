/**
 * Unified HTTP client for the Axiom Runtime `/v1` + `/health` API.
 *
 * Responsibilities: base URL resolution, bearer auth, JSON handling,
 * timeout/abort, and normalization of the backend's two error shapes
 * (structured `{error: {code, message}}` and plain `{error: "..."}`)
 * into a single `ApiError`. Everything above this layer sees exactly one
 * error type.
 */

export interface ApiErrorShape {
  status: number;
  code?: string;
  message: string;
  details?: unknown;
}

export class ApiError extends Error implements ApiErrorShape {
  readonly status: number;
  readonly code?: string;
  readonly details?: unknown;

  constructor(shape: ApiErrorShape) {
    super(shape.message);
    this.name = "ApiError";
    this.status = shape.status;
    this.code = shape.code;
    this.details = shape.details;
  }
}

interface StructuredErrorBody {
  error: {
    code?: unknown;
    message?: unknown;
    details?: unknown;
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Normalize any parsed error response body into an `ApiErrorShape`.
 * Exported for unit tests; used by the client on non-2xx responses.
 */
export function normalizeErrorBody(status: number, body: unknown): ApiErrorShape {
  const fallback = (message: string): ApiErrorShape => ({ status, message });

  if (typeof body === "string" && body.trim() !== "") {
    return fallback(body);
  }
  if (isRecord(body) && "error" in body) {
    const error = (body as unknown as StructuredErrorBody).error;
    if (isRecord(error)) {
      const code = typeof error.code === "string" ? error.code : undefined;
      const message =
        typeof error.message === "string" && error.message !== ""
          ? error.message
          : code ?? `Request failed with status ${status}`;
      const shape: ApiErrorShape = { status, code, message };
      if ("details" in error) shape.details = error.details;
      return shape;
    }
    if (typeof error === "string" && error !== "") {
      return fallback(error);
    }
  }
  return fallback(`Request failed with status ${status}`);
}

export interface ApiClientOptions {
  /** Base URL, e.g. "" (same origin) or "http://127.0.0.1:8080". No trailing slash. */
  baseUrl: string;
  /** Bearer token; empty means no Authorization header (/health is unauthenticated). */
  apiKey: string;
  /** Total timeout per request in ms. */
  timeoutMs?: number;
  /** AbortSignal for caller-side cancellation, composed with the timeout. */
  signal?: AbortSignal;
  /** Injectable fetch for tests. Defaults to global fetch. */
  fetchImpl?: typeof fetch;
}

const DEFAULT_TIMEOUT_MS = 10_000;

function joinUrl(baseUrl: string, path: string): string {
  const base = baseUrl.replace(/\/+$/, "");
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${base}${suffix}`;
}

export async function apiGet<T>(path: string, options: ApiClientOptions): Promise<T> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const timeout = AbortSignal.timeout(timeoutMs);
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;

  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.apiKey) headers.Authorization = `Bearer ${options.apiKey}`;

  let response: Response;
  try {
    response = await fetchImpl(joinUrl(options.baseUrl, path), {
      method: "GET",
      headers,
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError({ status: 0, message: "Request timed out or was cancelled" });
    }
    throw new ApiError({
      status: 0,
      message: error instanceof Error ? error.message : "Network request failed",
    });
  }

  const text = await response.text();
  let body: unknown = null;
  if (text !== "") {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!response.ok) {
    const shape = normalizeErrorBody(response.status, body);
    throw new ApiError(shape);
  }
  return body as T;
}

export interface ApiPostOptions extends ApiClientOptions {
  /** JSON-serializable body; omitted (no Content-Type) when undefined. */
  body?: unknown;
  /** Extra request headers, e.g. Idempotency-Key. Values never leave this call. */
  headers?: Record<string, string>;
}

/**
 * POST with a JSON body and JSON response, sharing apiGet's timeout, abort
 * and error normalization. A transport failure surfaces as `ApiError` with
 * `status === 0`; any received HTTP status (including 5xx) is returned as a
 * normal non-ok ApiError and must NOT be auto-retried by callers.
 */
export async function apiPost<T>(path: string, options: ApiPostOptions): Promise<T> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const timeout = AbortSignal.timeout(timeoutMs);
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;

  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.headers ?? {}),
  };
  if (options.apiKey) headers.Authorization = `Bearer ${options.apiKey}`;
  const hasBody = options.body !== undefined;
  if (hasBody) headers["Content-Type"] = "application/json";

  let response: Response;
  try {
    response = await fetchImpl(joinUrl(options.baseUrl, path), {
      method: "POST",
      headers,
      body: hasBody ? JSON.stringify(options.body) : undefined,
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError({ status: 0, message: "Request timed out or was cancelled" });
    }
    throw new ApiError({
      status: 0,
      message: error instanceof Error ? error.message : "Network request failed",
    });
  }

  const text = await response.text();
  let body: unknown = null;
  if (text !== "") {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!response.ok) {
    const shape = normalizeErrorBody(response.status, body);
    throw new ApiError(shape);
  }
  return body as T;
}
