import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchThreadMessages, type ThreadMessage, type ThreadMessagesPage } from "../api/messages";
import { useConnection } from "../lib/connection";
import { queryKeys } from "./keys";
import { ApiError } from "../api/client";

export interface ThreadMessagesData {
  messages: ThreadMessage[];
  olderCursor: string | null;
  afterCursor: string;
  currentRun: ThreadMessagesPage["current_run"];
  hasNewer?: boolean;
}

/** Historical and live pages may resolve in either order; only their own boundary changes. */
export function mergeMessagePage(
  current: ThreadMessagesData | undefined,
  page: ThreadMessagesPage,
  direction: "initial" | "older" | "after",
): ThreadMessagesData {
  const messages = new Map((current?.messages ?? []).map((message) => [message.id, message]));
  for (const message of page.messages) messages.set(message.id, message);
  return {
    messages: [...messages.values()].sort((a, b) => a.id - b.id),
    olderCursor: direction === "after" && current ? current.olderCursor : page.older_cursor,
    afterCursor: direction === "older" && current ? current.afterCursor : page.after_cursor,
    currentRun: direction === "older" && current ? current.currentRun : page.current_run,
    hasNewer: direction === "older" && current ? current.hasNewer : direction === "after" && page.has_more,
  };
}

export function useThreadMessages(threadId: string | null) {
  const connection = useConnection();
  const queryClient = useQueryClient();
  const key = queryKeys.forConnection(connection).messages(threadId ?? "");
  const [older, setOlder] = useState<{ key: string; loading: boolean; error: string | null }>({ key: "", loading: false, error: null });
  const scopeKey = JSON.stringify(key);
  const olderController = useRef<AbortController | null>(null);
  useEffect(() => () => { olderController.current?.abort(); }, [scopeKey]);
  const query = useQuery<ThreadMessagesData, Error>({
    queryKey: key,
    enabled: threadId !== null,
    retry: false,
    refetchInterval: (query) => query.state.error instanceof ApiError &&
      ((query.state.error.status >= 400 && query.state.error.status < 500) || query.state.error.code === "INVALID_MESSAGES_RESPONSE")
      ? false : query.state.data?.hasNewer ? 1 : 1500,
    queryFn: async ({ signal }): Promise<ThreadMessagesData> => {
      const previous = queryClient.getQueryData<ThreadMessagesData>(key);
      const page = await fetchThreadMessages(connection.config, threadId as string,
        previous ? { after: previous.afterCursor } : {}, { signal });
      signal.throwIfAborted();
      return mergeMessagePage(queryClient.getQueryData<ThreadMessagesData>(key), page, previous ? "after" : "initial");
    },
  });
  const loadOlder = useCallback(async () => {
    const current = queryClient.getQueryData<ThreadMessagesData>(key);
    if (threadId === null || !current?.olderCursor || (older.key === scopeKey && older.loading)) return;
    const controller = new AbortController();
    olderController.current = controller;
    setOlder({ key: scopeKey, loading: true, error: null });
    try {
      const page = await fetchThreadMessages(connection.config, threadId, { before: current.olderCursor }, { signal: controller.signal });
      controller.signal.throwIfAborted();
      queryClient.setQueryData<ThreadMessagesData>(key, (latest) => mergeMessagePage(latest, page, "older"));
      setOlder({ key: scopeKey, loading: false, error: null });
    } catch (error) {
      if (!controller.signal.aborted) setOlder({ key: scopeKey, loading: false, error: error instanceof Error ? error.message : "Unable to load earlier messages" });
    }
  }, [queryClient, scopeKey, threadId, connection.config, older.key, older.loading]);
  return {
    messages: query.data?.messages ?? [], currentRun: query.data?.currentRun ?? null,
    loading: threadId !== null && query.isPending,
    error: query.error instanceof Error ? query.error.message : null,
    hasOlder: query.data?.olderCursor != null,
    loadingOlder: older.key === scopeKey && older.loading,
    olderError: older.key === scopeKey ? older.error : null,
    loadOlder,
    retry: () => void query.refetch(),
  };
}
