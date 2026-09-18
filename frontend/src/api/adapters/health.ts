import type { HealthDto } from "../dto/health";

/** Adapter: health DTO → compact connection view for the Settings page. */
export interface HealthView {
  status: string;
  workers: number;
  database: string;
  storageBackend: string;
  capacity: {
    activeRuns: number | null;
    queuedRuns: number | null;
    maxActiveRuns: number | null;
    maxQueuedRuns: number | null;
  } | null;
}

export function adaptHealth(dto: HealthDto): HealthView {
  return {
    status: dto.status,
    workers: dto.workers,
    database: dto.database,
    storageBackend: dto.storage_backend,
    capacity: dto.capacity
      ? {
          activeRuns: dto.capacity.active_runs ?? null,
          queuedRuns: dto.capacity.queued_runs ?? null,
          maxActiveRuns: dto.capacity.max_active_runs ?? null,
          maxQueuedRuns: dto.capacity.max_queued_runs ?? null,
        }
      : null,
  };
}
