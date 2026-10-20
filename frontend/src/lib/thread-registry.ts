import { normalizeRuntimeBaseUrl } from "./connection";

/**
 * Local recent-thread registry (localStorage).
 *
 * The Runtime has no `GET /v1/threads` list endpoint, so the Workbench keeps
 * its own list of thread IDs the user has opened on this device. This is
 * metadata ONLY — never prompts, conversation text, tool output, or anything
 * beyond IDs and timestamps. Removing an entry never deletes the runtime
 * thread.
 */

export interface ThreadRegistryEntry {
  threadId: string;
  /** Epoch ms when the thread was first registered locally. */
  createdAt: number;
  /** Epoch ms of last selection in the Workbench. */
  lastOpenedAt: number;
  label?: string;
}

export const THREAD_REGISTRY_LIMIT = 50;

const STORAGE_KEY = "axiom.workbench.threads";
const LAST_THREAD_KEY = "axiom.workbench.last-thread";

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function defaultStorage(): StorageLike | null {
  try {
    if (typeof window !== "undefined" && window.localStorage) return window.localStorage;
  } catch {
    /* localStorage unavailable (privacy mode etc.) */
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Pure core (directly unit-testable)                                  */
/* ------------------------------------------------------------------ */

function isValidEntry(value: unknown): value is ThreadRegistryEntry {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.threadId === "string" &&
    record.threadId !== "" &&
    typeof record.createdAt === "number" &&
    typeof record.lastOpenedAt === "number"
  );
}

/** Most recently opened first. */
function sortEntries(entries: ThreadRegistryEntry[]): ThreadRegistryEntry[] {
  return [...entries].sort((a, b) => b.lastOpenedAt - a.lastOpenedAt);
}

/** Sorted, de-duplicated, and capped — evicts the least recently opened. */
export function normalizeEntries(entries: ThreadRegistryEntry[]): ThreadRegistryEntry[] {
  const byId = new Map<string, ThreadRegistryEntry>();
  for (const entry of entries) {
    if (isValidEntry(entry)) byId.set(entry.threadId, entry);
  }
  return sortEntries([...byId.values()]).slice(0, THREAD_REGISTRY_LIMIT);
}

export function applyAdd(
  entries: ThreadRegistryEntry[],
  threadId: string,
  now: number,
): ThreadRegistryEntry[] {
  const existing = entries.find((entry) => entry.threadId === threadId);
  const next: ThreadRegistryEntry = {
    threadId,
    createdAt: existing?.createdAt ?? now,
    lastOpenedAt: now,
    ...(existing?.label !== undefined ? { label: existing.label } : {}),
  };
  return normalizeEntries([next, ...entries.filter((entry) => entry.threadId !== threadId)]);
}

export function applyTouch(
  entries: ThreadRegistryEntry[],
  threadId: string,
  now: number,
): ThreadRegistryEntry[] {
  if (!entries.some((entry) => entry.threadId === threadId)) return entries;
  return normalizeEntries(
    entries.map((entry) => (entry.threadId === threadId ? { ...entry, lastOpenedAt: now } : entry)),
  );
}

export function applyRemove(
  entries: ThreadRegistryEntry[],
  threadId: string,
): ThreadRegistryEntry[] {
  return entries.filter((entry) => entry.threadId !== threadId);
}

/* ------------------------------------------------------------------ */
/* Storage-backed registry                                             */
/* ------------------------------------------------------------------ */

export interface ThreadRegistry {
  list(): ThreadRegistryEntry[];
  add(threadId: string): ThreadRegistryEntry[];
  touch(threadId: string): ThreadRegistryEntry[];
  remove(threadId: string): ThreadRegistryEntry[];
}

export function createThreadRegistry(
  storage: StorageLike | null,
  now: () => number = Date.now,
  scope: string = normalizeRuntimeBaseUrl(""),
): ThreadRegistry {
  const key = `${STORAGE_KEY}:${encodeURIComponent(scope)}`;
  // Unscoped records have no trustworthy Runtime owner. Discard once rather
  // than silently adopting them into the first Runtime opened after upgrade.
  try {
    storage?.removeItem(STORAGE_KEY);
    storage?.removeItem(LAST_THREAD_KEY);
  } catch { /* unavailable storage */ }
  const read = (): ThreadRegistryEntry[] => {
    if (storage === null) return [];
    try {
      const raw = storage.getItem(key);
      if (raw === null) return [];
      const parsed: unknown = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return normalizeEntries(parsed.filter(isValidEntry));
    } catch {
      return [];
    }
  };
  const write = (entries: ThreadRegistryEntry[]): ThreadRegistryEntry[] => {
    if (storage !== null) {
      try {
        storage.setItem(key, JSON.stringify(entries));
      } catch {
        /* quota / privacy mode — in-memory result still returned */
      }
    }
    return entries;
  };

  return {
    list: read,
    add: (threadId) => write(applyAdd(read(), threadId, now())),
    touch: (threadId) => write(applyTouch(read(), threadId, now())),
    remove: (threadId) => write(applyRemove(read(), threadId)),
  };
}

const registries = new Map<string, ThreadRegistry>();
export function getThreadRegistry(scope: string): ThreadRegistry {
  let registry = registries.get(scope);
  if (!registry) {
    registry = createThreadRegistry(defaultStorage(), Date.now, scope);
    registries.set(scope, registry);
  }
  return registry;
}

/* ------------------------------------------------------------------ */
/* Last-selected thread (returning from Run Detail restores it)        */
/* ------------------------------------------------------------------ */

export function loadLastThreadId(scope: string, storage: StorageLike | null = defaultStorage()): string | null {
  if (storage === null) return null;
  try {
    const value = storage.getItem(`${LAST_THREAD_KEY}:${encodeURIComponent(scope)}`);
    return typeof value === "string" && value !== "" ? value : null;
  } catch {
    return null;
  }
}

export function saveLastThreadId(
  scope: string,
  threadId: string | null,
  storage: StorageLike | null = defaultStorage(),
): void {
  if (storage === null) return;
  try {
    const key = `${LAST_THREAD_KEY}:${encodeURIComponent(scope)}`;
    if (threadId === null) storage.removeItem(key);
    else storage.setItem(key, threadId);
  } catch {
    /* ignore */
  }
}
