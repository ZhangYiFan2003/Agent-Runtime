from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def artifact_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ArtifactBlob:
    sha256: str
    storage_key: str
    size_bytes: int
    storage_backend: str
    created_at: str = field(default_factory=artifact_now)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    blob_sha256: str
    run_id: str
    thread_id: str
    invocation_id: str
    tool_name: str
    name: str
    media_type: str
    size_bytes: int
    source_path: str | None = None
    kind: str | None = None
    reused_from_artifact_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output_slot: str = "default"
    created_at: str = field(default_factory=artifact_now)

    @property
    def reused(self) -> bool:
        return self.reused_from_artifact_id is not None

    def public_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.blob_sha256,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "invocation_id": self.invocation_id,
            "tool_name": self.tool_name,
            "name": self.name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "reused_from_artifact_id": self.reused_from_artifact_id,
            "reused": self.reused,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ArtifactReuseEntry:
    reuse_key: str
    artifact_id: str
    namespace: str
    producer_version: str
    created_at: str = field(default_factory=artifact_now)


@dataclass(frozen=True, slots=True)
class BlobWriteResult:
    blob: ArtifactBlob
    reused: bool


@dataclass(frozen=True, slots=True)
class ReuseDescriptor:
    namespace: str
    producer: str
    producer_version: str
    normalized_parameters: dict[str, Any]
    input_content_hashes: tuple[str, ...] = ()
    environment_digest: str = ""
    schema_version: int = 1
