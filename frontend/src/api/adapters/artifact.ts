import type { ArtifactDto } from "../dto/artifact";

export interface Artifact {
  artifactId: string;
  sha256: string;
  runId: string;
  threadId: string;
  invocationId: string;
  toolName: string;
  name: string;
  mediaType: string;
  sizeBytes: number;
  kind: string | null;
  reusedFromArtifactId: string | null;
  reused: boolean;
  createdAt: string;
  metadata: Record<string, unknown>;
}

export function adaptArtifact(dto: ArtifactDto): Artifact {
  return {
    artifactId: dto.artifact_id,
    sha256: dto.sha256,
    runId: dto.run_id,
    threadId: dto.thread_id,
    invocationId: dto.invocation_id,
    toolName: dto.tool_name,
    name: dto.name,
    mediaType: dto.media_type,
    sizeBytes: dto.size_bytes,
    kind: dto.kind,
    reusedFromArtifactId: dto.reused_from_artifact_id,
    reused: dto.reused,
    createdAt: dto.created_at,
    metadata: dto.metadata,
  };
}
