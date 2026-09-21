import { useEffect, useRef, useState } from "react";

/** Distance from the bottom (px) that still counts as "watching the tail". */
export const TAIL_THRESHOLD_PX = 48;

export interface FollowPin {
  scrollRef: React.RefObject<HTMLDivElement | null>;
  showJump: boolean;
  handleScroll: () => void;
  jumpToLatest: () => void;
}

/**
 * Follow-latest scrolling for live feeds (Run Detail event feed, Workbench
 * feed). While the user is near the bottom the list stays pinned to the
 * newest entry; scrolling up unpins and surfaces a "Jump to latest" affordance.
 * `changeKey` should identify the newest entry (scrolls when it changes).
 */
export function useFollowPin(changeKey: unknown): FollowPin {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const [showJump, setShowJump] = useState(false);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (el === null) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < TAIL_THRESHOLD_PX;
    pinnedRef.current = nearBottom;
    setShowJump(!nearBottom);
  };

  useEffect(() => {
    const el = scrollRef.current;
    if (el !== null && pinnedRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [changeKey]);

  const jumpToLatest = () => {
    const el = scrollRef.current;
    if (el === null) return;
    el.scrollTop = el.scrollHeight;
    pinnedRef.current = true;
    setShowJump(false);
  };

  return { scrollRef, showJump, handleScroll, jumpToLatest };
}
