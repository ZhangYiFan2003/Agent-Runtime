from __future__ import annotations

import asyncio
import hashlib
import json
from io import BytesIO

import pytest

from axiom.artifacts import (
    ArtifactService,
    ArtifactTooLargeError,
    LocalArtifactBlobStore,
    MemoryArtifactMetadataStore,
    ReuseDescriptor,
    S3ArtifactBlobStore,
    SQLiteArtifactMetadataStore,
    blob_storage_key,
    build_reuse_key,
)
from axiom.artifacts.tool import artifact_tools, publish_artifact
from axiom.config import AxiomConfig, config_to_public_dict, load_config
from axiom.policy import DefaultPermissionPolicy, PermissionAction
from axiom.runtime.api import RuntimeApiServer
from axiom.runtime.checkpoints import SQLiteCheckpointStore
from axiom.runtime.durable import DurableAgentRuntime
from axiom.runtime.models import Checkpoint, ToolExecutionRecord, ToolExecutionStatus
from axiom.tools import ToolRegistry
from axiom.tools.base import ToolContext


def _service(tmp_path, *, metadata=None, max_file_bytes=1024) -> ArtifactService:
    return ArtifactService(
        metadata=metadata or MemoryArtifactMetadataStore(),
        blobs=LocalArtifactBlobStore(tmp_path / "blobs"),
        max_file_bytes=max_file_bytes,
        max_metadata_bytes=1024,
    )


def _publish(service: ArtifactService, content: bytes, invocation: str, run: str = "run-1"):
    return service.publish_bytes(
        content,
        run_id=run,
        thread_id="thread-1",
        invocation_id=invocation,
        tool_name="producer",
        name="result.txt",
        media_type="text/plain",
    )


def test_blob_storage_key_is_content_addressed() -> None:
    digest = "a" * 64
    assert blob_storage_key(digest) == f"blobs/sha256/aa/{digest}"
    with pytest.raises(ValueError, match="SHA-256"):
        blob_storage_key("not-a-digest")


def test_local_blob_store_deduplicates_bytes(tmp_path) -> None:
    store = LocalArtifactBlobStore(tmp_path)
    first = store.put_bytes(b"same", max_bytes=10)
    second = store.put_bytes(b"same", max_bytes=10)

    assert first.blob.sha256 == hashlib.sha256(b"same").hexdigest()
    assert not first.reused
    assert second.reused
    assert b"".join(store.iter_bytes(first.blob.storage_key)) == b"same"


def test_local_blob_store_enforces_streaming_size_limit(tmp_path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")
    with pytest.raises(ArtifactTooLargeError, match="configured limit"):
        LocalArtifactBlobStore(tmp_path / "blobs").put_file(source, max_bytes=4)


def test_two_runs_get_distinct_records_for_one_blob(tmp_path) -> None:
    service = _service(tmp_path)
    first, first_reused = _publish(service, b"same", "inv-1", "run-1")
    second, second_reused = _publish(service, b"same", "inv-2", "run-2")

    assert first.artifact_id != second.artifact_id
    assert first.blob_sha256 == second.blob_sha256
    assert not first_reused
    assert second_reused


def test_publish_operation_is_idempotent(tmp_path) -> None:
    service = _service(tmp_path)
    first, _ = _publish(service, b"first", "inv-1")
    replay, _ = _publish(service, b"different", "inv-1")
    assert replay.artifact_id == first.artifact_id
    assert replay.blob_sha256 == first.blob_sha256


def test_artifact_metadata_is_bounded(tmp_path) -> None:
    service = ArtifactService(
        MemoryArtifactMetadataStore(),
        LocalArtifactBlobStore(tmp_path),
        max_file_bytes=1024,
        max_metadata_bytes=8,
    )
    with pytest.raises(ValueError, match="metadata exceeds"):
        service.publish_bytes(
            b"x",
            run_id="run",
            thread_id="thread",
            invocation_id="inv",
            tool_name="tool",
            name="x",
            metadata={"long": "value"},
        )


def test_reuse_key_is_stable_for_parameter_order() -> None:
    left = ReuseDescriptor("code", "tool", "1", {"b": 2, "a": 1})
    right = ReuseDescriptor("code", "tool", "1", {"a": 1, "b": 2})
    assert build_reuse_key(left) == build_reuse_key(right)


def test_reuse_key_invalidates_on_producer_version() -> None:
    left = ReuseDescriptor("code", "tool", "1", {"a": 1})
    right = ReuseDescriptor("code", "tool", "2", {"a": 1})
    assert build_reuse_key(left) != build_reuse_key(right)


def test_explicit_reuse_creates_a_new_logical_artifact(tmp_path) -> None:
    service = _service(tmp_path)
    source, _ = _publish(service, b"cached", "inv-1", "run-1")
    descriptor = ReuseDescriptor("report", "producer", "v1", {"format": "txt"})
    service.register_reuse(descriptor, source.artifact_id)

    reused = service.reuse(
        descriptor,
        run_id="run-2",
        thread_id="thread-1",
        invocation_id="inv-2",
        tool_name="producer",
    )

    assert reused is not None
    assert reused.artifact_id != source.artifact_id
    assert reused.reused_from_artifact_id == source.artifact_id
    assert reused.blob_sha256 == source.blob_sha256


def test_reuse_misses_when_blob_is_unavailable(tmp_path) -> None:
    service = _service(tmp_path)
    source, _ = _publish(service, b"cached", "inv-1")
    descriptor = ReuseDescriptor("report", "producer", "v1", {})
    service.register_reuse(descriptor, source.artifact_id)
    blob = service.metadata.get_blob(source.blob_sha256)
    assert blob is not None
    (tmp_path / "blobs" / blob.storage_key).unlink()
    assert (
        service.reuse(
            descriptor,
            run_id="run-2",
            thread_id="thread-1",
            invocation_id="inv-2",
            tool_name="producer",
        )
        is None
    )


def test_sqlite_metadata_survives_reconstruction(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    first = _service(tmp_path, metadata=SQLiteArtifactMetadataStore(database))
    artifact, _ = _publish(first, b"durable", "inv-1")
    second = _service(tmp_path, metadata=SQLiteArtifactMetadataStore(database))
    assert second.get(artifact.artifact_id) == artifact
    assert second.list_run("run-1") == [artifact]


def test_publish_tool_requires_durable_context(tmp_path) -> None:
    result = asyncio.run(
        publish_artifact(
            {"path": "result.txt"},
            ToolContext(cwd=str(tmp_path), config=AxiomConfig()),
        )
    )
    assert result.is_error
    assert "not enabled" in result.content


def test_publish_tool_publishes_workspace_file(tmp_path) -> None:
    (tmp_path / "result.txt").write_text("artifact", encoding="utf-8")
    service = _service(tmp_path / "store")
    result = asyncio.run(
        publish_artifact(
            {"path": "result.txt"},
            ToolContext(
                cwd=str(tmp_path),
                workspace=str(tmp_path),
                config=AxiomConfig(),
                artifact_service=service,
                run_id="run-1",
                thread_id="thread-1",
                invocation_id="inv-1",
            ),
        )
    )
    assert not result.is_error
    assert len(result.metadata["artifact_ids"]) == 1
    assert service.list_run("run-1")[0].name == "result.txt"


def test_publish_tool_rejects_path_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes workspace"):
        asyncio.run(
            publish_artifact(
                {"path": str(outside)},
                ToolContext(
                    cwd=str(workspace),
                    workspace=str(workspace),
                    config=AxiomConfig(),
                    artifact_service=_service(tmp_path / "store"),
                    run_id="run-1",
                    thread_id="thread-1",
                    invocation_id="inv-1",
                ),
            )
        )


def test_tool_execution_artifact_links_round_trip_in_sqlite(tmp_path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "runtime.db")
    record = ToolExecutionRecord(
        invocation_id="inv",
        run_id="run",
        tool_call_id="call",
        tool_name="publish_artifact",
        arguments_hash="hash",
        status=ToolExecutionStatus.SUCCEEDED,
        artifact_ids=["art_1"],
    )
    asyncio.run(store.save_tool_execution(record))
    loaded = asyncio.run(store.load_tool_execution("inv"))
    assert loaded is not None
    assert loaded.artifact_ids == ["art_1"]


def test_old_tool_execution_defaults_to_no_artifacts() -> None:
    record = ToolExecutionRecord.from_dict(
        {
            "invocation_id": "inv",
            "run_id": "run",
            "tool_name": "shell",
            "arguments_hash": "hash",
            "status": "SUCCEEDED",
        }
    )
    assert record.artifact_ids == []


def test_publish_tool_sensitive_path_is_denied_by_existing_policy(tmp_path) -> None:
    tool = artifact_tools()[0]
    context = ToolContext(cwd=str(tmp_path), workspace=str(tmp_path), config=AxiomConfig())
    request = tool.permission_request(
        {"path": ".env"},
        context,
        invocation_id="inv",
    )
    decision = asyncio.run(DefaultPermissionPolicy(tmp_path).evaluate(request))
    assert decision.action == PermissionAction.DENY
    assert decision.matched_rule == "filesystem.sensitive_path"


def test_durable_run_persists_artifact_link_without_embedding_content(tmp_path) -> None:
    class ArtifactClient:
        provider_name = "test"
        model_name = "test"
        max_context_window = 10_000

        async def chat(self, messages, _tools, *, system_prompt):
            del system_prompt
            if not any(message.role == "tool" for message in messages):
                yield {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": 0,
                        "id": "call-artifact",
                        "function": {
                            "name": "publish_artifact",
                            "arguments": json.dumps({"path": "result.txt"}),
                        },
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            yield {"type": "text_delta", "text": "published"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    (tmp_path / "result.txt").write_text("durable artifact", encoding="utf-8")
    config = AxiomConfig()
    config.policy.hitl_mode = "never"
    config.features.audit_log = False
    registry = ToolRegistry()
    registry.register_all(artifact_tools())
    store = SQLiteCheckpointStore(tmp_path / "runtime.db")
    service = _service(tmp_path / "artifact-store")
    runtime = DurableAgentRuntime(
        llm_client=ArtifactClient(),
        tool_registry=registry,
        system_prompt="test",
        cwd=str(tmp_path),
        config=config,
        store=store,
        artifact_service=service,
    )

    state = asyncio.run(
        runtime.start(
            thread_id="thread-1",
            turn_id="turn-1",
            run_id="run-1",
            input="publish",
        )
    )
    record = asyncio.run(store.load_tool_execution("run-1:call-artifact"))
    assert state.output_text == "published"
    assert record is not None and len(record.artifact_ids) == 1
    assert record.result is not None and "durable artifact" not in record.result
    assert service.get(record.artifact_ids[0]) is not None


class _FakeRequest:
    def __init__(self, path: str, *, authorized: bool = True) -> None:
        self.command = "GET"
        self.path = path
        self.headers = {"content-length": "0"}
        if authorized:
            self.headers["x-api-key"] = "test-key"
        self.rfile = BytesIO()
        self.wfile = BytesIO()
        self.status = 0
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name.lower()] = value

    def end_headers(self) -> None:
        return


def _artifact_api(tmp_path):
    config = AxiomConfig()
    config.artifacts.enabled = True
    config.artifacts.local_path = str(tmp_path / "blobs")
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-key",
        workers=0,
        data_dir=tmp_path / "runtime",
    )
    thread_id = server.repository.create_thread()
    state = Checkpoint.create(
        thread_id=thread_id,
        turn_id="turn-1",
        run_id="run-1",
        input="test",
    )
    asyncio.run(server.checkpoint_store.save(state))
    assert server.artifact_service is not None
    artifact, _ = _publish(server.artifact_service, b"download", "inv-1")
    return server, artifact


def test_artifact_api_lists_and_reads_metadata(tmp_path) -> None:
    server, artifact = _artifact_api(tmp_path)
    listing = _FakeRequest("/v1/runs/run-1/artifacts")
    server._handle(listing)  # type: ignore[arg-type]
    payload = json.loads(listing.wfile.getvalue())
    assert listing.status == 200
    assert payload["artifacts"][0]["artifact_id"] == artifact.artifact_id

    detail = _FakeRequest(f"/v1/artifacts/{artifact.artifact_id}")
    server._handle(detail)  # type: ignore[arg-type]
    assert json.loads(detail.wfile.getvalue())["sha256"] == artifact.blob_sha256


def test_artifact_content_download_is_authenticated(tmp_path) -> None:
    server, artifact = _artifact_api(tmp_path)
    unauthorized = _FakeRequest(f"/v1/artifacts/{artifact.artifact_id}/content", authorized=False)
    server._handle(unauthorized)  # type: ignore[arg-type]
    assert unauthorized.status == 401

    request = _FakeRequest(f"/v1/artifacts/{artifact.artifact_id}/content")
    server._handle(request)  # type: ignore[arg-type]
    assert request.status == 200
    assert request.wfile.getvalue() == b"download"
    assert request.response_headers["content-type"] == "text/plain"


def test_artifact_config_environment_and_redaction(tmp_path) -> None:
    config = load_config(
        project_root=tmp_path,
        env={
            "AXIOM_ARTIFACTS_ENABLED": "true",
            "AXIOM_ARTIFACTS_BACKEND": "s3",
            "AXIOM_ARTIFACT_S3_ENDPOINT": "http://storage:9000",
            "AXIOM_ARTIFACT_S3_BUCKET": "artifacts",
            "AXIOM_ARTIFACT_S3_ACCESS_KEY": "example-access",
            "AXIOM_ARTIFACT_S3_SECRET_KEY": "example-secret",
        },
    )
    assert config.artifacts.enabled
    assert config.artifacts.s3.bucket == "artifacts"
    assert config_to_public_dict(config)["artifacts"]["s3"]["secret_key"] == "***"


def test_enabled_s3_artifacts_require_credentials(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires bucket and credentials"):
        load_config(
            project_root=tmp_path,
            overrides={"artifacts": {"enabled": True, "backend": "s3"}},
            env={},
        )


class _ObjectResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def stream(self, chunk_size: int):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self) -> None:
        return

    def release_conn(self) -> None:
        return


class _FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def stat_object(self, _bucket: str, key: str) -> object:
        if key not in self.objects:
            error = RuntimeError("missing")
            error.code = "NoSuchKey"  # type: ignore[attr-defined]
            raise error
        return object()

    def put_object(self, _bucket: str, key: str, stream, length: int) -> None:
        self.objects[key] = stream.read(length)

    def get_object(self, _bucket: str, key: str) -> _ObjectResponse:
        return _ObjectResponse(self.objects[key])

    def bucket_exists(self, _bucket: str) -> bool:
        return True


def test_s3_blob_store_uses_content_addressed_keys() -> None:
    client = _FakeS3()
    store = S3ArtifactBlobStore(
        endpoint="http://storage:9000",
        bucket="artifacts",
        access_key="example",
        secret_key="example",
        secure=False,
        client=client,
    )
    first = store.put_bytes(b"remote", max_bytes=100)
    second = store.put_bytes(b"remote", max_bytes=100)
    assert not first.reused
    assert second.reused
    assert b"".join(store.iter_bytes(first.blob.storage_key, chunk_size=2)) == b"remote"
