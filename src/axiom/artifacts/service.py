from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from axiom.artifacts.blob import ArtifactBlobStore
from axiom.artifacts.metadata import ArtifactMetadataStore
from axiom.artifacts.models import (
    ArtifactRecord,
    ArtifactReuseEntry,
    ReuseDescriptor,
)


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactService:
    def __init__(
        self,
        metadata: ArtifactMetadataStore,
        blobs: ArtifactBlobStore,
        *,
        max_file_bytes: int,
        max_metadata_bytes: int,
    ) -> None:
        self.metadata = metadata
        self.blobs = blobs
        self.max_file_bytes = max_file_bytes
        self.max_metadata_bytes = max_metadata_bytes

    def publish_file(
        self,
        path: str | Path,
        *,
        run_id: str,
        thread_id: str,
        invocation_id: str,
        tool_name: str,
        name: str | None = None,
        media_type: str | None = None,
        source_path: str | None = None,
        kind: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        output_slot: str = "default",
    ) -> tuple[ArtifactRecord, bool]:
        existing = self.metadata.get_artifact_by_operation(invocation_id, output_slot)
        if existing is not None:
            return existing, True
        artifact_metadata = self._metadata(metadata)
        source = Path(path)
        written = self.blobs.put_file(source, max_bytes=self.max_file_bytes)
        self.metadata.upsert_blob(written.blob)
        artifact = ArtifactRecord(
            artifact_id=f"art_{uuid4().hex}",
            blob_sha256=written.blob.sha256,
            run_id=run_id,
            thread_id=thread_id,
            invocation_id=invocation_id,
            output_slot=output_slot,
            tool_name=tool_name,
            name=_artifact_name(name or source.name),
            media_type=(
                media_type
                or mimetypes.guess_type(source.name)[0]
                or "application/octet-stream"
            ),
            size_bytes=written.blob.size_bytes,
            source_path=source_path,
            kind=kind,
            metadata=artifact_metadata,
        )
        return self.metadata.create_artifact(artifact), written.reused

    def publish_bytes(
        self,
        content: bytes,
        *,
        run_id: str,
        thread_id: str,
        invocation_id: str,
        tool_name: str,
        name: str,
        media_type: str = "application/octet-stream",
        kind: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        output_slot: str = "default",
    ) -> tuple[ArtifactRecord, bool]:
        existing = self.metadata.get_artifact_by_operation(invocation_id, output_slot)
        if existing is not None:
            return existing, True
        artifact_metadata = self._metadata(metadata)
        written = self.blobs.put_bytes(content, max_bytes=self.max_file_bytes)
        self.metadata.upsert_blob(written.blob)
        artifact = ArtifactRecord(
            artifact_id=f"art_{uuid4().hex}",
            blob_sha256=written.blob.sha256,
            run_id=run_id,
            thread_id=thread_id,
            invocation_id=invocation_id,
            output_slot=output_slot,
            tool_name=tool_name,
            name=_artifact_name(name),
            media_type=media_type,
            size_bytes=written.blob.size_bytes,
            kind=kind,
            metadata=artifact_metadata,
        )
        return self.metadata.create_artifact(artifact), written.reused

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self.metadata.get_artifact(artifact_id)

    def list_run(self, run_id: str) -> list[ArtifactRecord]:
        return self.metadata.list_run_artifacts(run_id)

    def iter_content(self, artifact_id: str) -> Iterator[bytes]:
        artifact = self.metadata.get_artifact(artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError("artifact not found")
        blob = self.metadata.get_blob(artifact.blob_sha256)
        if blob is None or not self.blobs.exists(blob.storage_key):
            raise ArtifactNotFoundError("artifact blob not found")
        return self.blobs.iter_bytes(blob.storage_key)

    def register_reuse(self, descriptor: ReuseDescriptor, artifact_id: str) -> ArtifactReuseEntry:
        artifact = self.metadata.get_artifact(artifact_id)
        if artifact is None:
            raise ArtifactNotFoundError("source artifact not found")
        blob = self.metadata.get_blob(artifact.blob_sha256)
        if blob is None or not self.blobs.exists(blob.storage_key):
            raise ArtifactNotFoundError("source artifact blob not found")
        entry = ArtifactReuseEntry(
            reuse_key=build_reuse_key(descriptor),
            artifact_id=artifact_id,
            namespace=descriptor.namespace,
            producer_version=descriptor.producer_version,
        )
        return self.metadata.put_reuse_entry(entry)

    def reuse(
        self,
        descriptor: ReuseDescriptor,
        *,
        run_id: str,
        thread_id: str,
        invocation_id: str,
        tool_name: str,
        name: str | None = None,
        output_slot: str = "default",
    ) -> ArtifactRecord | None:
        existing = self.metadata.get_artifact_by_operation(invocation_id, output_slot)
        if existing is not None:
            return existing
        entry = self.metadata.get_reuse_entry(build_reuse_key(descriptor))
        if entry is None:
            return None
        source = self.metadata.get_artifact(entry.artifact_id)
        if source is None:
            return None
        blob = self.metadata.get_blob(source.blob_sha256)
        if blob is None or not self.blobs.exists(blob.storage_key):
            return None
        artifact = ArtifactRecord(
            artifact_id=f"art_{uuid4().hex}",
            blob_sha256=source.blob_sha256,
            run_id=run_id,
            thread_id=thread_id,
            invocation_id=invocation_id,
            output_slot=output_slot,
            tool_name=tool_name,
            name=_artifact_name(name or source.name),
            media_type=source.media_type,
            size_bytes=source.size_bytes,
            kind=source.kind,
            reused_from_artifact_id=source.artifact_id,
            metadata=source.metadata,
        )
        return self.metadata.create_artifact(artifact)

    def health(self) -> dict[str, str]:
        try:
            self.blobs.healthcheck()
        except Exception:
            return {
                "status": "unavailable",
                "metadata_backend": self.metadata.backend,
                "blob_backend": self.blobs.backend,
            }
        return {
            "status": "ok",
            "metadata_backend": self.metadata.backend,
            "blob_backend": self.blobs.backend,
        }

    def _metadata(self, value: Mapping[str, Any] | None) -> dict[str, Any]:
        data = dict(value or {})
        encoded = canonical_json(data)
        if len(encoded) > self.max_metadata_bytes:
            raise ValueError(
                f"artifact metadata exceeds configured limit of {self.max_metadata_bytes} bytes"
            )
        return json.loads(encoded)


def build_reuse_key(descriptor: ReuseDescriptor) -> str:
    payload = {
        "schema_version": descriptor.schema_version,
        "namespace": descriptor.namespace,
        "producer": descriptor.producer,
        "producer_version": descriptor.producer_version,
        "normalized_parameters": descriptor.normalized_parameters,
        "input_content_hashes": list(descriptor.input_content_hashes),
        "environment_digest": descriptor.environment_digest,
    }
    return f"sha256:{hashlib.sha256(canonical_json(payload)).hexdigest()}"


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _artifact_name(value: str) -> str:
    name = Path(value).name.replace("\x00", "").strip()
    return name[:255] or "artifact"
