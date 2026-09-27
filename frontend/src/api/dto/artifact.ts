import { z } from "zod";

export const artifactSchema = z
  .object({
    artifact_id: z.string(),
    sha256: z.string(),
    run_id: z.string(),
    thread_id: z.string(),
    invocation_id: z.string(),
    tool_name: z.string(),
    name: z.string(),
    media_type: z.string(),
    size_bytes: z.number().int().nonnegative(),
    kind: z.string().nullable(),
    reused_from_artifact_id: z.string().nullable(),
    reused: z.boolean(),
    created_at: z.string(),
    metadata: z.record(z.string(), z.unknown()),
  })
  .catchall(z.unknown());

export const runArtifactsSchema = z
  .object({
    run_id: z.string(),
    artifacts: z.array(artifactSchema),
  })
  .catchall(z.unknown());

export type ArtifactDto = z.infer<typeof artifactSchema>;
