import { beforeEach, describe, expect, it } from "vitest";
import {
  clearConnection,
  getConnection,
  getConnectionSnapshot,
  subscribeConnection,
  normalizeRuntimeBaseUrl,
  saveConnection,
} from "./connection";

// Node test environment has no window/sessionStorage: exercises the
// in-memory path. The sessionStorage path is thin JSON (de)serialization
// guarded by try/catch and is covered implicitly in the browser.
describe("connection config", () => {
  beforeEach(() => {
    clearConnection();
  });

  it("defaults to same-origin with no API key", () => {
    expect(getConnection()).toEqual({ baseUrl: "", apiKey: "" });
  });

  it("round-trips save/get/clear in memory", () => {
    saveConnection({ baseUrl: "http://127.0.0.1:8080", apiKey: "sk-test" });
    expect(getConnection()).toEqual({ baseUrl: "http://127.0.0.1:8080", apiKey: "sk-test" });
    clearConnection();
    expect(getConnection()).toEqual({ baseUrl: "", apiKey: "" });
  });
  it("emits a fresh generation for same-URL credential replacement and clear", () => {
    let emissions = 0;
    const unsubscribe = subscribeConnection(() => { emissions += 1; });
    saveConnection({ baseUrl: "http://runtime-a/", apiKey: "first" });
    const first = getConnectionSnapshot();
    expect(first.scope).toBe("http://runtime-a");
    expect(getConnectionSnapshot()).toBe(first);
    saveConnection({ baseUrl: "http://runtime-a", apiKey: "replacement" });
    const next = getConnectionSnapshot();
    expect(next.scope).toBe(first.scope);
    expect(next.generation).toBe(first.generation + 1);
    clearConnection();
    expect(getConnectionSnapshot().generation).toBe(next.generation + 1);
    expect(emissions).toBe(3);
    unsubscribe();
  });
  it("reuses the pending-submission URL scope, without credentials or query fragments", () => {
    expect(normalizeRuntimeBaseUrl("http://runtime-a/path///?ignored=yes#fragment"))
      .toBe("http://runtime-a/path");
  });
});
