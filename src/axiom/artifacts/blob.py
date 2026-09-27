from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from axiom.artifacts.models import ArtifactBlob, BlobWriteResult

CHUNK_SIZE = 1024 * 1024


class ArtifactTooLargeError(ValueError):
    pass


class ArtifactBlobStore(Protocol):
    backend: str

    def put_file(self, path: str | Path, *, max_bytes: int) -> BlobWriteResult: ...

    def put_bytes(self, content: bytes, *, max_bytes: int) -> BlobWriteResult: ...

    def iter_bytes(self, storage_key: str, *, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]: ...

    def exists(self, storage_key: str) -> bool: ...

    def healthcheck(self) -> None: ...


def blob_storage_key(sha256: str) -> str:
    if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
        raise ValueError("invalid SHA-256 digest")
    return f"blobs/sha256/{sha256[:2]}/{sha256}"


class LocalArtifactBlobStore:
    backend = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_file(self, path: str | Path, *, max_bytes: int) -> BlobWriteResult:
        source = Path(path)
        temporary_dir = self.root / ".tmp"
        temporary_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        temp_path: Path | None = None
        try:
            with source.open("rb") as input_handle, tempfile.NamedTemporaryFile(
                dir=temporary_dir,
                delete=False,
            ) as output_handle:
                temp_path = Path(output_handle.name)
                while chunk := input_handle.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ArtifactTooLargeError(
                            f"artifact exceeds configured limit of {max_bytes} bytes"
                        )
                    digest.update(chunk)
                    output_handle.write(chunk)
            sha256 = digest.hexdigest()
            storage_key = blob_storage_key(sha256)
            target = self._path(storage_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            reused = target.exists()
            if reused:
                temp_path.unlink(missing_ok=True)
            else:
                os.replace(temp_path, target)
            return BlobWriteResult(
                ArtifactBlob(sha256, storage_key, size, self.backend),
                reused=reused,
            )
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def put_bytes(self, content: bytes, *, max_bytes: int) -> BlobWriteResult:
        if len(content) > max_bytes:
            raise ArtifactTooLargeError(
                f"artifact exceeds configured limit of {max_bytes} bytes"
            )
        sha256 = hashlib.sha256(content).hexdigest()
        storage_key = blob_storage_key(sha256)
        target = self._path(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        reused = target.exists()
        if not reused:
            fd, raw_path = tempfile.mkstemp(dir=target.parent, prefix=".artifact-")
            temp_path = Path(raw_path)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)
        return BlobWriteResult(
            ArtifactBlob(sha256, storage_key, len(content), self.backend),
            reused=reused,
        )

    def iter_bytes(self, storage_key: str, *, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        with self._path(storage_key).open("rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk

    def exists(self, storage_key: str) -> bool:
        return self._path(storage_key).is_file()

    def healthcheck(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise RuntimeError("local artifact blob directory is unavailable")

    def _path(self, storage_key: str) -> Path:
        expected = blob_storage_key(storage_key.rsplit("/", 1)[-1])
        if storage_key != expected:
            raise ValueError("artifact storage key is invalid")
        return self.root / Path(storage_key)


class S3ArtifactBlobStore:
    backend = "s3"

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        secure: bool,
        region: str | None = None,
        client: object | None = None,
    ) -> None:
        self.bucket = bucket
        if client is not None:
            self.client = client
            return
        try:
            from minio import Minio
        except ImportError as exc:
            raise RuntimeError(
                "S3 artifact storage requires the 'artifact-s3' optional dependency"
            ) from exc
        parsed = urlsplit(endpoint if "://" in endpoint else f"http://{endpoint}")
        if not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("artifact S3 endpoint must be a host[:port] URL without a path")
        self.client = Minio(
            parsed.netloc,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure if parsed.scheme == "http" else True,
            region=region or None,
        )

    def put_file(self, path: str | Path, *, max_bytes: int) -> BlobWriteResult:
        source = Path(path)
        sha256, size = _hash_file(source, max_bytes=max_bytes)
        storage_key = blob_storage_key(sha256)
        reused = self.exists(storage_key)
        if not reused:
            self.client.fput_object(self.bucket, storage_key, str(source))
        return BlobWriteResult(
            ArtifactBlob(sha256, storage_key, size, self.backend),
            reused=reused,
        )

    def put_bytes(self, content: bytes, *, max_bytes: int) -> BlobWriteResult:
        import io

        if len(content) > max_bytes:
            raise ArtifactTooLargeError(
                f"artifact exceeds configured limit of {max_bytes} bytes"
            )
        sha256 = hashlib.sha256(content).hexdigest()
        storage_key = blob_storage_key(sha256)
        reused = self.exists(storage_key)
        if not reused:
            self.client.put_object(self.bucket, storage_key, io.BytesIO(content), len(content))
        return BlobWriteResult(
            ArtifactBlob(sha256, storage_key, len(content), self.backend),
            reused=reused,
        )

    def iter_bytes(self, storage_key: str, *, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        response = self.client.get_object(self.bucket, storage_key)
        try:
            yield from response.stream(chunk_size)
        finally:
            response.close()
            response.release_conn()

    def exists(self, storage_key: str) -> bool:
        try:
            self.client.stat_object(self.bucket, storage_key)
            return True
        except Exception as exc:  # MinIO's S3Error is optional with the SDK
            code = getattr(exc, "code", "")
            if code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound"}:
                return False
            raise

    def healthcheck(self) -> None:
        if not self.client.bucket_exists(self.bucket):
            raise RuntimeError("artifact bucket is unavailable")


def _hash_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            size += len(chunk)
            if size > max_bytes:
                raise ArtifactTooLargeError(
                    f"artifact exceeds configured limit of {max_bytes} bytes"
                )
            digest.update(chunk)
    return digest.hexdigest(), size
