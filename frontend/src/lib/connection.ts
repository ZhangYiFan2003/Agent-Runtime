import { useSyncExternalStore } from "react";

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

/** The Step-2 pending-submission identity: address only, never credentials. */
export function normalizeRuntimeBaseUrl(baseUrl: string): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "http://same-origin";
  const url = new URL(baseUrl.trim() || origin, origin);
  return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
}

export interface ConnectionSnapshot {
  config: ConnectionConfig;
  scope: string;
  /** Local, non-persisted identity for credential replacement and clear. */
  generation: number;
}

let current: ConnectionConfig = loadPersisted();
let snapshot: ConnectionSnapshot = {
  config: current, scope: normalizeRuntimeBaseUrl(current.baseUrl), generation: 0,
};
const listeners = new Set<() => void>();

export function getConnectionSnapshot(): ConnectionSnapshot { return snapshot; }
export function subscribeConnection(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
export function useConnection(): ConnectionSnapshot {
  return useSyncExternalStore(subscribeConnection, getConnectionSnapshot, getConnectionSnapshot);
}

function replace(config: ConnectionConfig): void {
  const scope = normalizeRuntimeBaseUrl(config.baseUrl);
  current = { ...config };
  snapshot = {
    config: current, scope,
    generation: snapshot.generation + 1,
  };
  for (const listener of listeners) listener();
}

export function getConnection(): ConnectionConfig {
  return current;
}

export function saveConnection(config: ConnectionConfig): void {
  replace(config);
  const store = storage();
  if (!store) return;
  try {
    store.setItem(STORAGE_KEY, JSON.stringify(current));
  } catch {
    /* quota / privacy mode — memory copy still works */
  }
}

export function clearConnection(): void {
  replace(DEFAULTS);
  const store = storage();
  if (!store) return;
  try {
    store.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}
