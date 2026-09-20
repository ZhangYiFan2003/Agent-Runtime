import { describe, expect, it } from "vitest";
import { parseSseFrames } from "./parser";

describe("parseSseFrames", () => {
  it("parses the runtime replay frame format", () => {
    const text =
      'id: 1\nevent: llm.started\ndata: {"model": "deepseek"}\n\n' +
      'id: 2\nevent: tool.completed\ndata: {"tool_name": "search_code"}\n\n';
    expect(parseSseFrames(text)).toEqual([
      { id: "1", event: "llm.started", data: '{"model": "deepseek"}' },
      { id: "2", event: "tool.completed", data: '{"tool_name": "search_code"}' },
    ]);
  });

  it("joins multi-line data with newlines", () => {
    const frames = parseSseFrames("data: line one\ndata: line two\n\n");
    expect(frames).toEqual([{ data: "line one\nline two" }]);
  });

  it("tolerates CRLF endings", () => {
    const frames = parseSseFrames("id: 7\r\nevent: run.started\r\ndata: {\"a\": 1}\r\n\r\n");
    expect(frames).toEqual([{ id: "7", event: "run.started", data: '{"a": 1}' }]);
  });

  it("ignores comment / keepalive lines and unknown fields", () => {
    const frames = parseSseFrames(
      ": keepalive\nretry: 3000\nevent: ping\ndata: {}\n\n: keepalive\n\n",
    );
    expect(frames).toEqual([{ event: "ping", data: "{}" }]);
  });

  it("flushes a trailing frame without a final blank line", () => {
    expect(parseSseFrames("id: 9\ndata: {\"last\": true}")).toEqual([
      { id: "9", data: '{"last": true}' },
    ]);
  });

  it("returns no frames for empty input", () => {
    expect(parseSseFrames("")).toEqual([]);
    expect(parseSseFrames("\n\n\n")).toEqual([]);
  });

  it("tolerates mixed LF and CRLF in one body", () => {
    const frames = parseSseFrames("id: 1\r\ndata: {\"a\": 1}\n\ndata: {\"b\": 2}\r\n\r\n");
    expect(frames).toEqual([
      { id: "1", data: '{"a": 1}' },
      { data: '{"b": 2}' },
    ]);
  });
});
