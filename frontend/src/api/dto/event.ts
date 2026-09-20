import { z } from "zod";

/**
 * DTO for one persisted runtime event envelope from
 * `GET /v1/threads/{thread_id}/events`.
 *
 * The event payload's own fields are spread at the top level of each
 * envelope (there is no nested `payload` key), alongside the stable envelope
 * keys below. `.catchall()` keeps unknown top-level fields — both future
 * envelope additions and arbitrary payload fields — so parsing never breaks
 * when the backend adds new event types.
 */
export const runtimeEventEnvelopeSchema = z
  .object({
    event_id: z.number().int(),
    thread_id: z.string(),
    turn_id: z.string().nullable(),
    run_id: z.string().nullable(),
    parent_run_id: z.string().nullable(),
    parent_step_id: z.string().nullable(),
    assignment_id: z.string().nullable(),
    event_type: z.string(),
    timestamp: z.string(),
  })
  .catchall(z.unknown());

export type RuntimeEventEnvelopeDto = z.infer<typeof runtimeEventEnvelopeSchema>;
