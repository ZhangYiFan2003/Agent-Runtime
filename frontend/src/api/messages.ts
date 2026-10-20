import { z } from "zod";
import { ApiError, apiGet } from "./client";
import type { ConnectionConfig } from "../lib/connection";

const messageSchema = z.object({
  id: z.number().int().positive(), thread_id: z.string().min(1),
  run_id: z.string().min(1), turn_id: z.string().min(1),
  role: z.enum(["user", "assistant"]), text: z.string(), created_at: z.string(),
});
const pageSchema = z.object({
  messages: z.array(messageSchema), older_cursor: z.string().min(1).nullable(),
  after_cursor: z.string().min(1), has_more: z.boolean(),
  current_run: z.object({ run_id: z.string().min(1), status: z.string().min(1) }).nullable(),
});

export type ThreadMessage = z.infer<typeof messageSchema>;
export type ThreadMessagesPage = z.infer<typeof pageSchema>;

/** SQL-filtered durable conversation, independent of the debug replay window. */
export async function fetchThreadMessages(
  config: ConnectionConfig,
  threadId: string,
  params: { before?: string; after?: string; limit?: number } = {},
  options: { signal?: AbortSignal; fetchImpl?: typeof fetch } = {},
): Promise<ThreadMessagesPage> {
  const query = new URLSearchParams({ limit: String(params.limit ?? 50) });
  if (params.before !== undefined) query.set("before", params.before);
  if (params.after !== undefined) query.set("after", params.after);
  const raw = await apiGet<unknown>(`/v1/threads/${encodeURIComponent(threadId)}/messages?${query}`, {
    ...config, ...options,
  });
  const parsed = pageSchema.safeParse(raw);
  if (!parsed.success || parsed.data.messages.some((message, index, messages) =>
    message.thread_id !== threadId || (index > 0 && message.id <= messages[index - 1].id)) ||
    (params.after === undefined && parsed.data.has_more !== (parsed.data.older_cursor !== null)) ||
    (params.after !== undefined && parsed.data.messages.length === 0 && (parsed.data.after_cursor !== params.after || parsed.data.has_more))) {
    throw new ApiError({ status: 0, code: "INVALID_MESSAGES_RESPONSE", message: "Invalid conversation page received from Runtime." });
  }
  return parsed.data;
}
