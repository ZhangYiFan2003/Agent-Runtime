import { afterEach, describe, expect, it, vi } from "vitest";
import { InfiniteQueryObserver, QueryClient } from "@tanstack/react-query";
import { getConnectionSnapshot, saveConnection } from "../lib/connection";
import { makeRunViewDto } from "../test-fixtures/run";
import { flattenRunPages, runsQueryOptions } from "./use-runs";
import { queryKeys } from "./keys";

const filter = { search: "", status: "all", strategy: "all" };
const response = (ids: string[], next: string | null = null) => new Response(JSON.stringify({
  runs: ids.map((run_id) => makeRunViewDto({ run_id })), next_cursor: next, has_more: next !== null,
}), { status: 200, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

describe("Run pages and Runtime isolation", () => {
  it("requests only the first page, loads more explicitly and deduplicates overlaps", async () => {
    saveConnection({ baseUrl: "http://runtime-a", apiKey: "fake-a" });
    const urls: URL[] = [];
    vi.stubGlobal("fetch", async (input: string) => {
      const url = new URL(input);
      urls.push(url);
      return url.searchParams.has("cursor") ? response(["run-b", "run-c"]) : response(["run-a", "run-b"], "next");
    });
    const client = new QueryClient();
    const observer = new InfiniteQueryObserver(client, runsQueryOptions(getConnectionSnapshot(), filter, "newest"));
    const unsubscribe = observer.subscribe(() => {});
    await observer.refetch();
    expect(urls).toHaveLength(1);
    expect(urls[0].searchParams.get("limit")).toBe("50");
    expect(observer.getCurrentResult().hasNextPage).toBe(true);
    await observer.fetchNextPage();
    expect(urls).toHaveLength(2);
    expect(urls[1].searchParams.get("cursor")).toBe("next");
    const data = observer.getCurrentResult().data!;
    expect(flattenRunPages(data.pages).map((run) => run.runId)).toEqual(["run-a", "run-b", "run-c"]);
    expect(observer.getCurrentResult().hasNextPage).toBe(false);
    unsubscribe();
    client.clear();
  });

  it("aborts A's old page and ignores its late response through A → B → A and same-URL replacement", async () => {
    saveConnection({ baseUrl: "http://runtime-a", apiKey: "fake-a-old" });
    const firstA = getConnectionSnapshot();
    let release!: (value: Response) => void;
    let oldSignal!: AbortSignal;
    let aRequests = 0;
    vi.stubGlobal("fetch", async (input: string, init: RequestInit) => {
      if (String(input).startsWith("http://runtime-b")) return response(["only-b"]);
      if (++aRequests === 1) {
        oldSignal = init.signal!;
        return new Promise<Response>((resolve) => { release = resolve; });
      }
      return response([new Headers(init.headers).get("Authorization") === "Bearer fake-a-new" ? "new-credentials-a" : "only-a"]);
    });
    const client = new QueryClient();
    const observer = new InfiniteQueryObserver(client, runsQueryOptions(firstA, filter, "newest"));
    const unsubscribe = observer.subscribe(() => {});
    saveConnection({ baseUrl: "http://runtime-b", apiKey: "fake-b" });
    observer.setOptions(runsQueryOptions(getConnectionSnapshot(), filter, "newest"));
    expect(observer.getCurrentResult().data).toBeUndefined();
    await observer.refetch();
    expect(oldSignal.aborted).toBe(true);
    release(response(["late-a"]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(flattenRunPages(observer.getCurrentResult().data!.pages).map((run) => run.runId)).toEqual(["only-b"]);
    saveConnection({ baseUrl: "http://runtime-a", apiKey: "fake-a-old" });
    const returningA = getConnectionSnapshot();
    observer.setOptions(runsQueryOptions(returningA, filter, "newest"));
    expect(observer.getCurrentResult().data).toBeUndefined();
    await observer.refetch();
    expect(flattenRunPages(observer.getCurrentResult().data!.pages).map((run) => run.runId)).toEqual(["only-a"]);
    saveConnection({ baseUrl: "http://runtime-a", apiKey: "fake-a-new" });
    const replacement = getConnectionSnapshot();
    expect(replacement.scope).toBe(returningA.scope);
    expect(replacement.generation).not.toBe(returningA.generation);
    observer.setOptions(runsQueryOptions(replacement, filter, "newest"));
    expect(observer.getCurrentResult().data).toBeUndefined();
    await observer.refetch();
    expect(flattenRunPages(observer.getCurrentResult().data!.pages).map((run) => run.runId)).toEqual(["new-credentials-a"]);
    expect(JSON.stringify(queryKeys.connection(replacement))).not.toContain("fake-a-new");
    unsubscribe();
    client.clear();
  });

  it("starts a new server-filtered list with no inherited cursor when filters change", async () => {
    saveConnection({ baseUrl: "http://runtime-a", apiKey: "fake-a" });
    const urls: URL[] = [];
    vi.stubGlobal("fetch", async (input: string) => {
      const url = new URL(input); urls.push(url);
      return response([url.searchParams.get("status") === "FAILED" ? "matching-old-run" : "newer-run"], "next");
    });
    const client = new QueryClient();
    const snapshot = getConnectionSnapshot();
    const observer = new InfiniteQueryObserver(client, runsQueryOptions(snapshot, filter, "newest"));
    const unsubscribe = observer.subscribe(() => {});
    await observer.refetch();
    await observer.fetchNextPage();
    observer.setOptions(runsQueryOptions(snapshot, { search: " thread ", status: "FAILED", strategy: "multi_agent" }, "oldest"));
    await observer.refetch();
    expect(Object.fromEntries(urls.at(-1)!.searchParams)).toEqual({
      limit: "50", sort: "oldest", search: "thread", status: "FAILED", strategy: "multi_agent",
    });
    expect(flattenRunPages(observer.getCurrentResult().data!.pages).map((run) => run.runId)).toEqual(["matching-old-run"]);
    unsubscribe();
    client.clear();
  });
});
