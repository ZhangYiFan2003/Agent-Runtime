import { describe, expect, it } from "vitest";
import { formatDurationMs, formatEventTime, formatRelativeTime, truncateId } from "./format";

describe("formatDurationMs", () => {
  it("formats ms / seconds / minutes / hours", () => {
    expect(formatDurationMs(null)).toBe("—");
    expect(formatDurationMs(-5)).toBe("—");
    expect(formatDurationMs(Number.NaN)).toBe("—");
    expect(formatDurationMs(180)).toBe("180 ms");
    expect(formatDurationMs(1800)).toBe("1.8 s");
    expect(formatDurationMs(12_400)).toBe("12 s");
    expect(formatDurationMs(84_000)).toBe("1m 24s");
    expect(formatDurationMs(3_720_000)).toBe("1h 02m");
  });
});

describe("formatRelativeTime", () => {
  // Fixed anchor: 2026-09-18T12:00:00 local
  const now = new Date(2026, 8, 18, 12, 0, 0).getTime();

  it("formats recent times as relative", () => {
    expect(formatRelativeTime(new Date(2026, 8, 18, 11, 59, 57).toISOString(), now)).toBe(
      "3s ago",
    );
    expect(formatRelativeTime(new Date(2026, 8, 18, 11, 58, 0).toISOString(), now)).toBe(
      "2m ago",
    );
  });

  it("formats same-day times as clock", () => {
    expect(formatRelativeTime(new Date(2026, 8, 18, 10, 32, 0).toISOString(), now)).toBe(
      "10:32",
    );
  });

  it("formats older times as date", () => {
    expect(formatRelativeTime(new Date(2026, 8, 17, 22, 0, 0).toISOString(), now)).toBe(
      "Sep 17",
    );
  });

  it("handles garbage input", () => {
    expect(formatRelativeTime("not-a-date", now)).toBe("—");
  });
});

describe("formatEventTime", () => {
  it("formats HH:MM:SS.mmm and tolerates garbage", () => {
    const d = new Date(2026, 8, 19, 14, 32, 7, 183);
    expect(formatEventTime(d.toISOString())).toBe("14:32:07.183");
    expect(formatEventTime("not-a-date")).toBe("—");
  });
});

describe("truncateId", () => {
  it("truncates long ids and keeps short ones", () => {
    expect(truncateId("run_a8312c9d4e5f6071", 14)).toBe("run_a8312c9d4e…");
    expect(truncateId("run_x", 14)).toBe("run_x");
  });
});
