import { describe, expect, it } from "vitest";
import {
  THREAD_REGISTRY_LIMIT,
  createThreadRegistry,
  loadLastThreadId,
  saveLastThreadId,
  type StorageLike,
} from "./thread-registry";

/** In-memory Storage stand-in (tests run in the node environment). */
function fakeStorage(): StorageLike & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => void data.set(key, value),
    removeItem: (key) => void data.delete(key),
  };
}

describe("thread registry", () => {
  it("adds, lists most-recently-opened first, touches, and removes", () => {
    let now = 1000;
    const registry = createThreadRegistry(fakeStorage(), () => now);

    registry.add("thread_a");
    now = 2000;
    registry.add("thread_b");

    let list = registry.list();
    expect(list.map((entry) => entry.threadId)).toEqual(["thread_b", "thread_a"]);
    expect(list[1].createdAt).toBe(1000);

    // touch bumps lastOpenedAt without changing createdAt
    now = 3000;
    registry.touch("thread_a");
    list = registry.list();
    expect(list.map((entry) => entry.threadId)).toEqual(["thread_a", "thread_b"]);
    expect(list[0].createdAt).toBe(1000);
    expect(list[0].lastOpenedAt).toBe(3000);

    // re-adding an existing id refreshes instead of duplicating
    now = 4000;
    registry.add("thread_b");
    expect(registry.list().map((entry) => entry.threadId)).toEqual(["thread_b", "thread_a"]);

    registry.remove("thread_b");
    expect(registry.list().map((entry) => entry.threadId)).toEqual(["thread_a"]);
    // removing an unknown id is a no-op
    registry.remove("thread_nope");
    expect(registry.list()).toHaveLength(1);
  });

  it("evicts the least recently opened beyond the 50-entry cap", () => {
    let now = 0;
    const registry = createThreadRegistry(fakeStorage(), () => now);
    for (let i = 0; i < THREAD_REGISTRY_LIMIT + 5; i += 1) {
      now = i;
      registry.add(`thread_${i}`);
    }
    const list = registry.list();
    expect(list).toHaveLength(THREAD_REGISTRY_LIMIT);
    // oldest five evicted; newest kept
    expect(list.some((entry) => entry.threadId === "thread_4")).toBe(false);
    expect(list[0].threadId).toBe(`thread_${THREAD_REGISTRY_LIMIT + 4}`);
  });
  it("isolates recent threads and the selected thread through A → B → A", () => {
    const store = fakeStorage();
    const a = createThreadRegistry(store, () => 1, "http://a");
    const b = createThreadRegistry(store, () => 1, "http://b");
    a.add("thread-a");
    saveLastThreadId("http://a", "thread-a", store);
    expect(b.list()).toEqual([]);
    expect(loadLastThreadId("http://b", store)).toBeNull();
    b.add("thread-b");
    saveLastThreadId("http://b", "thread-b", store);
    expect(a.list().map((entry) => entry.threadId)).toEqual(["thread-a"]);
    expect(loadLastThreadId("http://a", store)).toBe("thread-a");
    saveLastThreadId("http://b", null, store);
    expect(loadLastThreadId("http://a", store)).toBe("thread-a");
  });
  it("discards legacy records with no Runtime owner, without touching pending submissions", () => {
    const store = fakeStorage();
    store.setItem("axiom.workbench.threads", JSON.stringify([{ threadId: "legacy", createdAt: 1, lastOpenedAt: 1 }]));
    store.setItem("axiom.workbench.last-thread", "legacy");
    store.setItem("axiom.workbench.pending:runtime:thread", "unconfirmed");
    const a = createThreadRegistry(store, Date.now, "http://a");
    expect(a.list()).toEqual([]);
    expect(loadLastThreadId("http://a", store)).toBeNull();
    expect(store.getItem("axiom.workbench.threads")).toBeNull();
    expect(store.getItem("axiom.workbench.last-thread")).toBeNull();
    expect(store.getItem("axiom.workbench.pending:runtime:thread")).toBe("unconfirmed");
  });
});
