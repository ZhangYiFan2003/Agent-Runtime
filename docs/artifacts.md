# Artifact Store

Axiom's optional Artifact Store keeps large or binary Run outputs outside Checkpoints, Events, and
ToolExecution result text. It is append/read only in this release.

## Data model

```text
ArtifactService
├── ArtifactMetadataStore
│   ├── SQLite
│   └── PostgreSQL
└── ArtifactBlobStore
    ├── Local filesystem
    └── S3-compatible / MinIO
```

A **Blob** is immutable bytes identified by SHA-256. Its physical key is derived only from the
content digest: `blobs/sha256/<first-two-hex>/<full-sha256>`. A filename, Run ID, timestamp, or
client-supplied hash never determines blob identity.

An **ArtifactRecord** is a logical reference from one Run and Tool invocation to a Blob. It has an
opaque `art_<uuid>` ID plus producer identity (`run_id`, `thread_id`, `invocation_id`, and
`tool_name`), display metadata, and an optional `reused_from_artifact_id`. Two Runs publishing the
same bytes therefore create two provenance-bearing Artifact records backed by one physical Blob.

`ToolExecutionRecord.artifact_ids` stores only the logical references. Artifact bytes are never
embedded in ToolExecution, Checkpoint, Event, or PostgreSQL JSON state.

## Publishing

When `artifacts.enabled` is true, Axiom registers `publish_artifact`. The Tool accepts an existing
workspace `path` plus optional `name` and `media_type`. It uses the existing PathGuard and Permission
Policy, so paths outside the workspace and sensitive names such as credentials, keys, tokens, and
environment files are denied before execution. It streams and hashes the file in bounded chunks,
enforces `artifacts.max_file_bytes`, and returns only a bounded ID/name/size/hash summary.

The write order is Blob, Blob metadata, then Artifact metadata. A blob upload followed by a database
failure can leave a safe unreferenced blob; an Artifact is not committed before its Blob exists.
Artifact publication fails closed when required persistence is unavailable. Other Tools and Run
scheduling do not depend on a successful Artifact operation.

## Reuse semantics

Three independent mechanisms must not be conflated:

- **ToolExecution idempotency:** the same invocation reuses its persisted successful Tool result and
  `artifact_ids`; the Tool is not executed again.
- **Blob deduplication:** different invocations with equal bytes share one SHA-256 Blob while retaining
  distinct Artifact records.
- **Computation reuse:** an explicitly deterministic producer may register a versioned reuse key and
  later create a new current-Run Artifact referencing the source Blob.

The generic reuse key is SHA-256 over canonical UTF-8 JSON containing a schema version, namespace,
producer, explicit producer version, normalized parameters, input content hashes, and a deliberately
scoped environment digest. It never hashes the whole process environment. A hit is accepted only
when the source Artifact and Blob still exist. Concurrent misses may compute twice; there is no
distributed single-flight lock.

No production producer currently opts into computation reuse. `publish_artifact` provides real Blob
deduplication, while existing domain-specific caches such as Code Intelligence
`embedding_input_hash` remain independent. Shell, LLM, Web, and MCP calls are not automatically
cached, and read-only does not imply deterministic.

## Backends and API

Local mode uses SQLite metadata and `<data_dir>/artifacts` for Blob bytes. Distributed Compose mode
uses PostgreSQL as metadata authority and MinIO as shared Blob storage. The S3 SDK is an optional
`artifact-s3` dependency; local users do not need it.

Authenticated read endpoints are:

- `GET /v1/runs/{run_id}/artifacts`
- `GET /v1/artifacts/{artifact_id}`
- `GET /v1/artifacts/{artifact_id}/content`

Content is streamed in bounded chunks through the Runtime API with a safe attachment filename. The
browser never receives object-store credentials or a public MinIO URL.

An Artifact can also be referenced by a durable Claim Evidence record. The Evidence stores the
logical Artifact ID, Blob digest, and bounded producer metadata—not the bytes. Read-time provenance
integrity confirms that the Artifact and Blob metadata still resolve. See
[`provenance.md`](provenance.md).

## Retention and limitations

Artifact retention currently follows deployment storage lifetime. Back up both PostgreSQL metadata
and MinIO `artifact_data`; restoring only one side can create missing or unreferenced data.

This release has no automatic orphan GC, per-Run retention policy, delete API, Range downloads,
presigned URLs, public upload UI, preview framework, or distributed computation single-flight.
Automatic orphan/blob GC is deferred; a future collector must treat Artifact IDs referenced by
Claim Evidence as retention roots or explicitly preserve their missing-source history.
