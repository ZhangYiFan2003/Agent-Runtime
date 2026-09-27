from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from axiom.artifacts.models import ArtifactBlob, ArtifactRecord, ArtifactReuseEntry


class ArtifactMetadataStore(Protocol):
    backend: str

    def upsert_blob(self, blob: ArtifactBlob) -> ArtifactBlob: ...

    def get_blob(self, sha256: str) -> ArtifactBlob | None: ...

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None: ...

    def get_artifact_by_operation(
        self, invocation_id: str, output_slot: str
    ) -> ArtifactRecord | None: ...

    def create_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord: ...

    def list_run_artifacts(self, run_id: str) -> list[ArtifactRecord]: ...

    def get_reuse_entry(self, reuse_key: str) -> ArtifactReuseEntry | None: ...

    def put_reuse_entry(self, entry: ArtifactReuseEntry) -> ArtifactReuseEntry: ...


class MemoryArtifactMetadataStore:
    backend = "memory"

    def __init__(self) -> None:
        self.blobs: dict[str, ArtifactBlob] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.operations: dict[tuple[str, str], str] = {}
        self.reuse_entries: dict[str, ArtifactReuseEntry] = {}

    def upsert_blob(self, blob: ArtifactBlob) -> ArtifactBlob:
        return self.blobs.setdefault(blob.sha256, blob)

    def get_blob(self, sha256: str) -> ArtifactBlob | None:
        return self.blobs.get(sha256)

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        return self.artifacts.get(artifact_id)

    def get_artifact_by_operation(
        self, invocation_id: str, output_slot: str
    ) -> ArtifactRecord | None:
        artifact_id = self.operations.get((invocation_id, output_slot))
        return self.artifacts.get(artifact_id) if artifact_id else None

    def create_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        existing = self.get_artifact_by_operation(artifact.invocation_id, artifact.output_slot)
        if existing is not None:
            return existing
        self.artifacts[artifact.artifact_id] = artifact
        self.operations[(artifact.invocation_id, artifact.output_slot)] = artifact.artifact_id
        return artifact

    def list_run_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        return sorted(
            (artifact for artifact in self.artifacts.values() if artifact.run_id == run_id),
            key=lambda artifact: (artifact.created_at, artifact.artifact_id),
        )

    def get_reuse_entry(self, reuse_key: str) -> ArtifactReuseEntry | None:
        return self.reuse_entries.get(reuse_key)

    def put_reuse_entry(self, entry: ArtifactReuseEntry) -> ArtifactReuseEntry:
        return self.reuse_entries.setdefault(entry.reuse_key, entry)


class SQLiteArtifactMetadataStore:
    backend = "sqlite"

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def upsert_blob(self, blob: ArtifactBlob) -> ArtifactBlob:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into artifact_blobs(
                    sha256, storage_key, size_bytes, storage_backend, created_at
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    blob.sha256,
                    blob.storage_key,
                    blob.size_bytes,
                    blob.storage_backend,
                    blob.created_at,
                ),
            )
            row = conn.execute(
                "select sha256, storage_key, size_bytes, storage_backend, created_at "
                "from artifact_blobs where sha256 = ?",
                (blob.sha256,),
            ).fetchone()
        return _blob_from_row(row)

    def get_blob(self, sha256: str) -> ArtifactBlob | None:
        with self._connect() as conn:
            row = conn.execute(
                "select sha256, storage_key, size_bytes, storage_backend, created_at "
                "from artifact_blobs where sha256 = ?",
                (sha256,),
            ).fetchone()
        return _blob_from_row(row) if row else None

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"{_SQLITE_ARTIFACT_SELECT} where artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        return _artifact_from_row(row) if row else None

    def get_artifact_by_operation(
        self, invocation_id: str, output_slot: str
    ) -> ArtifactRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"{_SQLITE_ARTIFACT_SELECT} where invocation_id = ? and output_slot = ?",
                (invocation_id, output_slot),
            ).fetchone()
        return _artifact_from_row(row) if row else None

    def create_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into artifacts(
                    artifact_id, blob_sha256, run_id, thread_id, invocation_id, output_slot,
                    tool_name, name, media_type, size_bytes, source_path, kind,
                    reused_from_artifact_id, metadata_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _artifact_values(artifact),
            )
            row = conn.execute(
                f"{_SQLITE_ARTIFACT_SELECT} where invocation_id = ? and output_slot = ?",
                (artifact.invocation_id, artifact.output_slot),
            ).fetchone()
        return _artifact_from_row(row)

    def list_run_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                f"{_SQLITE_ARTIFACT_SELECT} where run_id = ? order by created_at, artifact_id",
                (run_id,),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def get_reuse_entry(self, reuse_key: str) -> ArtifactReuseEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                "select reuse_key, artifact_id, namespace, producer_version, created_at "
                "from artifact_reuse_entries where reuse_key = ?",
                (reuse_key,),
            ).fetchone()
        return _reuse_from_row(row) if row else None

    def put_reuse_entry(self, entry: ArtifactReuseEntry) -> ArtifactReuseEntry:
        with self._connect() as conn:
            conn.execute(
                """
                insert or ignore into artifact_reuse_entries(
                    reuse_key, artifact_id, namespace, producer_version, created_at
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    entry.reuse_key,
                    entry.artifact_id,
                    entry.namespace,
                    entry.producer_version,
                    entry.created_at,
                ),
            )
            row = conn.execute(
                "select reuse_key, artifact_id, namespace, producer_version, created_at "
                "from artifact_reuse_entries where reuse_key = ?",
                (entry.reuse_key,),
            ).fetchone()
        return _reuse_from_row(row)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            for statement in sqlite_artifact_schema():
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma foreign_keys = on")
        return conn


class PostgresArtifactMetadataStore:
    backend = "postgres"

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def upsert_blob(self, blob: ArtifactBlob) -> ArtifactBlob:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                insert into artifact_blobs(
                    sha256, storage_key, size_bytes, storage_backend, created_at
                ) values (%s, %s, %s, %s, %s)
                on conflict(sha256) do update set sha256 = excluded.sha256
                returning sha256, storage_key, size_bytes, storage_backend, created_at
                """,
                (
                    blob.sha256,
                    blob.storage_key,
                    blob.size_bytes,
                    blob.storage_backend,
                    blob.created_at,
                ),
            ).fetchone()
        return _blob_from_row(row)

    def get_blob(self, sha256: str) -> ArtifactBlob | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select sha256, storage_key, size_bytes, storage_backend, created_at "
                "from artifact_blobs where sha256 = %s",
                (sha256,),
            ).fetchone()
        return _blob_from_row(row) if row else None

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        return self._one("artifact_id = %s", (artifact_id,))

    def get_artifact_by_operation(
        self, invocation_id: str, output_slot: str
    ) -> ArtifactRecord | None:
        return self._one(
            "invocation_id = %s and output_slot = %s",
            (invocation_id, output_slot),
        )

    def create_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                insert into artifacts(
                    artifact_id, blob_sha256, run_id, thread_id, invocation_id, output_slot,
                    tool_name, name, media_type, size_bytes, source_path, kind,
                    reused_from_artifact_id, metadata_json, created_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s
                )
                on conflict(invocation_id, output_slot) do update
                    set invocation_id = excluded.invocation_id
                returning artifact_id, blob_sha256, run_id, thread_id, invocation_id,
                    output_slot, tool_name, name, media_type, size_bytes, source_path, kind,
                    reused_from_artifact_id, metadata_json, created_at
                """,
                _artifact_values(artifact),
            ).fetchone()
        return _artifact_from_row(row)

    def list_run_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"{_POSTGRES_ARTIFACT_SELECT} where run_id = %s "
                "order by created_at, artifact_id",
                (run_id,),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def get_reuse_entry(self, reuse_key: str) -> ArtifactReuseEntry | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select reuse_key, artifact_id, namespace, producer_version, created_at "
                "from artifact_reuse_entries where reuse_key = %s",
                (reuse_key,),
            ).fetchone()
        return _reuse_from_row(row) if row else None

    def put_reuse_entry(self, entry: ArtifactReuseEntry) -> ArtifactReuseEntry:
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                insert into artifact_reuse_entries(
                    reuse_key, artifact_id, namespace, producer_version, created_at
                ) values (%s, %s, %s, %s, %s)
                on conflict(reuse_key) do update set reuse_key = excluded.reuse_key
                returning reuse_key, artifact_id, namespace, producer_version, created_at
                """,
                (
                    entry.reuse_key,
                    entry.artifact_id,
                    entry.namespace,
                    entry.producer_version,
                    entry.created_at,
                ),
            ).fetchone()
        return _reuse_from_row(row)

    def _one(self, where: str, values: tuple[object, ...]) -> ArtifactRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(f"{_POSTGRES_ARTIFACT_SELECT} where {where}", values).fetchone()
        return _artifact_from_row(row) if row else None


def sqlite_artifact_schema() -> tuple[str, ...]:
    return (
        """
        create table if not exists artifact_blobs (
            sha256 text primary key,
            storage_key text not null unique,
            size_bytes integer not null,
            storage_backend text not null,
            created_at text not null
        )
        """,
        """
        create table if not exists artifacts (
            artifact_id text primary key,
            blob_sha256 text not null references artifact_blobs(sha256),
            run_id text not null,
            thread_id text not null,
            invocation_id text not null,
            output_slot text not null default 'default',
            tool_name text not null,
            name text not null,
            media_type text not null,
            size_bytes integer not null,
            source_path text,
            kind text,
            reused_from_artifact_id text references artifacts(artifact_id),
            metadata_json text not null default '{}',
            created_at text not null,
            unique(invocation_id, output_slot)
        )
        """,
        "create index if not exists idx_artifacts_run on artifacts(run_id, created_at)",
        "create index if not exists idx_artifacts_blob on artifacts(blob_sha256)",
        """
        create table if not exists artifact_reuse_entries (
            reuse_key text primary key,
            artifact_id text not null references artifacts(artifact_id),
            namespace text not null,
            producer_version text not null,
            created_at text not null
        )
        """,
    )


def postgres_artifact_schema() -> tuple[str, ...]:
    return (
        """
        create table if not exists artifact_blobs (
            sha256 text primary key,
            storage_key text not null unique,
            size_bytes bigint not null,
            storage_backend text not null,
            created_at timestamptz not null
        )
        """,
        """
        create table if not exists artifacts (
            artifact_id text primary key,
            blob_sha256 text not null references artifact_blobs(sha256),
            run_id text not null,
            thread_id text not null,
            invocation_id text not null,
            output_slot text not null default 'default',
            tool_name text not null,
            name text not null,
            media_type text not null,
            size_bytes bigint not null,
            source_path text,
            kind text,
            reused_from_artifact_id text references artifacts(artifact_id),
            metadata_json jsonb not null default '{}'::jsonb,
            created_at timestamptz not null,
            unique(invocation_id, output_slot)
        )
        """,
        "create index if not exists idx_artifacts_run on artifacts(run_id, created_at)",
        "create index if not exists idx_artifacts_invocation on artifacts(invocation_id)",
        "create index if not exists idx_artifacts_blob on artifacts(blob_sha256)",
        """
        create table if not exists artifact_reuse_entries (
            reuse_key text primary key,
            artifact_id text not null references artifacts(artifact_id),
            namespace text not null,
            producer_version text not null,
            created_at timestamptz not null
        )
        """,
    )


_SQLITE_ARTIFACT_SELECT = """
select artifact_id, blob_sha256, run_id, thread_id, invocation_id, output_slot,
       tool_name, name, media_type, size_bytes, source_path, kind,
       reused_from_artifact_id, metadata_json, created_at
from artifacts
"""
_POSTGRES_ARTIFACT_SELECT = _SQLITE_ARTIFACT_SELECT


def _artifact_values(artifact: ArtifactRecord) -> tuple[object, ...]:
    return (
        artifact.artifact_id,
        artifact.blob_sha256,
        artifact.run_id,
        artifact.thread_id,
        artifact.invocation_id,
        artifact.output_slot,
        artifact.tool_name,
        artifact.name,
        artifact.media_type,
        artifact.size_bytes,
        artifact.source_path,
        artifact.kind,
        artifact.reused_from_artifact_id,
        json.dumps(artifact.metadata, ensure_ascii=False, separators=(",", ":")),
        artifact.created_at,
    )


def _artifact_from_row(row: Any) -> ArtifactRecord:
    metadata = row[13]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return ArtifactRecord(
        artifact_id=str(row[0]),
        blob_sha256=str(row[1]),
        run_id=str(row[2]),
        thread_id=str(row[3]),
        invocation_id=str(row[4]),
        output_slot=str(row[5]),
        tool_name=str(row[6]),
        name=str(row[7]),
        media_type=str(row[8]),
        size_bytes=int(row[9]),
        source_path=str(row[10]) if row[10] is not None else None,
        kind=str(row[11]) if row[11] is not None else None,
        reused_from_artifact_id=str(row[12]) if row[12] is not None else None,
        metadata=deepcopy(dict(metadata or {})),
        created_at=_timestamp(row[14]),
    )


def _blob_from_row(row: Any) -> ArtifactBlob:
    return ArtifactBlob(
        sha256=str(row[0]),
        storage_key=str(row[1]),
        size_bytes=int(row[2]),
        storage_backend=str(row[3]),
        created_at=_timestamp(row[4]),
    )


def _reuse_from_row(row: Any) -> ArtifactReuseEntry:
    return ArtifactReuseEntry(
        reuse_key=str(row[0]),
        artifact_id=str(row[1]),
        namespace=str(row[2]),
        producer_version=str(row[3]),
        created_at=_timestamp(row[4]),
    )


def _timestamp(value: Any) -> str:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else str(value)
