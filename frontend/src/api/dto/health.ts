import { z } from "zod";

/**
 * DTO for `GET /health` (unauthenticated).
 * Mirrors the backend response exactly (snake_case). Unknown fields are
 * preserved via catchall so the schema survives backend additions.
 */
export const capacitySnapshotSchema = z
  .object({
    queued_runs: z.number().int().nullable().optional(),
    active_runs: z.number().int().nullable().optional(),
    max_queued_runs: z.number().int().nullable().optional(),
    max_active_runs: z.number().int().nullable().optional(),
    admission_rejections: z.number().int().nullable().optional(),
    failure_queued_runs: z.number().int().nullable().optional(),
  })
  .catchall(z.unknown());

export const healthSchema = z
  .object({
    status: z.string(),
    workers: z.number().int(),
    database: z.string(),
    storage_backend: z.string(),
    capacity: capacitySnapshotSchema.nullable().optional(),
  })
  .catchall(z.unknown());

export type HealthDto = z.infer<typeof healthSchema>;
