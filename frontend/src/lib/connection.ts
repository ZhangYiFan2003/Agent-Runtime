/**
 * Runtime connection configuration.
 *
 * The API key is intentionally kept out of `.env` / `VITE_*` variables
 * (Vite inlines those into the browser bundle). It lives in runtime memory
 * and, when the user asks for persistence across reloads, sessionStorage —
 * never localStorage, never the repository.
 */

export interface ConnectionConfig {
  /** Empty string means "same origin" (dev-server proxy to the Runtime). */
  baseUrl: string;
  apiKey: string;
}

const STORAGE_KEY = "axiom.runtime-connection";

const DEFAULTS: ConnectionConfig = { baseUrl: "", apiKey: "" };

function storage(): Storage | null {
  try {
    if (typeof window !== "undefined" && window.sessionStorage) {
      return window.sessionStorage;
    }
  } catch {
    /* sessionStorage unavailable (privacy mode etc.) — fall back to memory */
  }
  return null;
}

function loadPersisted(): ConnectionConfig {
  const store = storage();
  if (!store) return { ...DEFAULTS };
  try {
    const raw = store.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return { ...DEFAULTS };
    const record = parsed as Record<string, unknown>;
    return {
      baseUrl: typeof record.baseUrl === "string" ? record.baseUrl : "",
      apiKey: typeof record.apiKey === "string" ? record.apiKey : "",
    };
  } catch {
    return { ...DEFAULTS };
  }
}

let current: ConnectionConfig = loadPersisted();

export function getConnection(): ConnectionConfig {
  return current;
}

export function saveConnection(config: ConnectionConfig): void {
  current = { ...config };
  const store = storage();
  if (!store) return;
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    /* quota / privacy mode — memory copy still works */
  }
}

export function clearConnection(): void {
  current = { ...DEFAULTS };
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}
