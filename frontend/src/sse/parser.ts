/**
 * Pure SSE (Server-Sent Events) frame parser.
 *
 * The Runtime's events endpoint returns `text/event-stream` bodies that close
 * after the stored frames ("replay mode"): `id: {int}\nevent: {type}\ndata:
 * {json}\n\n` with LF endings (CRLF tolerated). This parser handles the
 * general case: blank-line-delimited frames, multi-line `data:`, comment
 * lines (`:` prefix), ignored unknown fields, and a trailing unterminated
 * frame.
 */

export interface SseFrame {
  id?: string;
  event?: string;
  /** Raw data payload (multi-line data joined with "\n"). */
  data: string;
}

/**
 * Split a raw SSE body into frames. A frame is terminated by one or more
 * blank lines; a final frame without a trailing blank line is still emitted.
 * Comment lines (starting with ":") and unknown fields are ignored.
 */
export function parseSseFrames(text: string): SseFrame[] {
  const frames: SseFrame[] = [];
  // Normalize CRLF (and lone CR) to LF, then split on blank lines. A split
  // pattern consuming the newline before the blank line avoids emitting
  // phantom empty frames.
  const blocks = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").split(/\n\n+/);

  for (const block of blocks) {
    if (block === "") continue;
    const dataLines: string[] = [];
    let id: string | undefined;
    let event: string | undefined;
    let sawField = false;

    for (const line of block.split("\n")) {
      if (line === "" || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      // A single leading space after the colon is part of the syntax.
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);

      if (field === "data") {
        dataLines.push(value);
        sawField = true;
      } else if (field === "id") {
        id = value;
        sawField = true;
      } else if (field === "event") {
        event = value;
        sawField = true;
      }
      // Unknown fields (retry, etc.) are skipped.
    }

    if (!sawField) continue;
    frames.push({ ...(id !== undefined ? { id } : {}), ...(event !== undefined ? { event } : {}), data: dataLines.join("\n") });
  }

  return frames;
}
