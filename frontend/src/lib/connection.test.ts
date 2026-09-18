import { beforeEach, describe, expect, it } from "vitest";
import {
  clearConnection,
  getConnection,
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
});
