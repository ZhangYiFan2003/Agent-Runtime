import {
  runtimeEventEnvelopeSchema,
  type RuntimeEventEnvelopeDto,
} from "../dto/event";

/**
 * camelCase view model of one persisted runtime event. `payload` holds every
 * top-level envelope field that is not a known envelope key — i.e. the event
 * payload itself, since the backend spreads payload fields at the top level.
 */
export interface RuntimeEvent {
  eventId: number;
  threadId: string;
  turnId: string | null;
  runId: string | null;
  parentRunId: string | null;
  parentStepId: string | null;
  assignmentId: string | null;
  eventType: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

const ENVELOPE_KEYS = new Set([
  "event_id",
  "thread_id",
  "turn_id",
  "run_id",
  "parent_run_id",
  "parent_step_id",
  "assignment_id",
  "event_type",
  "timestamp",
]);

export function adaptRuntimeEvent(dto: RuntimeEventEnvelopeDto): RuntimeEvent {
  const payload: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(dto)) {
    if (!ENVELOPE_KEYS.has(key)) payload[key] = value;
  }
  return {
    eventId: dto.event_id,
    threadId: dto.thread_id,
    turnId: dto.turn_id,
    runId: dto.run_id,
    parentRunId: dto.parent_run_id,
    parentStepId: dto.parent_step_id,
    assignmentId: dto.assignment_id,
    eventType: dto.event_type,
    timestamp: dto.timestamp,
    payload,
  };
}

/** Validate one raw `data:` JSON value; returns null when malformed. */
export function parseEventEnvelope(raw: unknown): RuntimeEventEnvelopeDto | null {
  const result = runtimeEventEnvelopeSchema.safeParse(raw);
  return result.success ? result.data : null;
}
