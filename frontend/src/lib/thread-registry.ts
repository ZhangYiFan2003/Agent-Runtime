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
): ThreadRegistry {
  const read = (): ThreadRegistryEntry[] => {
    if (storage === null) return [];
    try {
      const raw = storage.getItem(STORAGE_KEY);
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
        storage.setItem(STORAGE_KEY, JSON.stringify(entries));
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

/** App-wide registry on window.localStorage. */
export const threadRegistry = createThreadRegistry(defaultStorage());

/* ------------------------------------------------------------------ */
/* Last-selected thread (returning from Run Detail restores it)        */
/* ------------------------------------------------------------------ */

export function loadLastThreadId(storage: StorageLike | null = defaultStorage()): string | null {
  if (storage === null) return null;
  try {
    const value = storage.getItem(LAST_THREAD_KEY);
    return typeof value === "string" && value !== "" ? value : null;
  } catch {
    return null;
  }
}

export function saveLastThreadId(
  threadId: string | null,
  storage: StorageLike | null = defaultStorage(),
): void {
  if (storage === null) return;
  try {
    if (threadId === null) storage.removeItem(LAST_THREAD_KEY);
    else storage.setItem(LAST_THREAD_KEY, threadId);
  } catch {
    /* ignore */
  }
}
