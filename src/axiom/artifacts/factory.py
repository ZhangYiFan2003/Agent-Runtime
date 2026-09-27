from __future__ import annotations

from pathlib import Path

from axiom.artifacts.blob import LocalArtifactBlobStore, S3ArtifactBlobStore
from axiom.artifacts.metadata import ArtifactMetadataStore
from axiom.artifacts.service import ArtifactService
from axiom.config import ArtifactConfig


def build_artifact_service(
    *,
    config: ArtifactConfig,
    metadata: ArtifactMetadataStore,
    data_dir: str | Path,
) -> ArtifactService | None:
    if not config.enabled:
        return None
    backend = config.backend.strip().lower()
    if backend == "local":
        root = (
            Path(config.local_path).expanduser()
            if config.local_path
            else Path(data_dir) / "artifacts"
        )
        blobs = LocalArtifactBlobStore(root)
    elif backend == "s3":
        blobs = S3ArtifactBlobStore(
            endpoint=config.s3.endpoint,
            bucket=config.s3.bucket,
            access_key=config.s3.access_key,
            secret_key=config.s3.secret_key,
            secure=config.s3.secure,
            region=config.s3.region or None,
        )
    else:  # load_config rejects this; keep direct construction explicit.
        raise ValueError(f"unsupported artifact backend: {backend}")
    return ArtifactService(
        metadata=metadata,
        blobs=blobs,
        max_file_bytes=config.max_file_bytes,
        max_metadata_bytes=config.max_metadata_bytes,
    )
