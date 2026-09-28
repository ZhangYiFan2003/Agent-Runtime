from __future__ import annotations

import asyncio
import json
from io import BytesIO

import pytest

from axiom.artifacts import (
    ArtifactService,
    LocalArtifactBlobStore,
    MemoryArtifactMetadataStore,
    ReuseDescriptor,
)
from axiom.config import AxiomConfig, load_config
from axiom.provenance import (
    EvidenceIntegrity,
    MemoryProvenanceStore,
    ProvenanceService,
    SQLiteProvenanceStore,
    parse_claim_citations,
    record_claim,
)
from axiom.runtime.api import RuntimeApiServer
from axiom.runtime.checkpoints import MemoryCheckpointStore
from axiom.runtime.completion import CompletionContract, CompletionVerifier
from axiom.runtime.models import Checkpoint, ToolExecutionRecord, ToolExecutionStatus
from axiom.runtime.observability import Span, SpanStatus, SpanType, Trace, tool_span_id
from axiom.runtime.observability_store import MemoryObservabilityStore
from axiom.tools.base import ToolContext


async def _save_run(
    store: MemoryCheckpointStore,
    run_id: str,
    *,
    thread_id: str = "thread-1",
    turn_id: str = "turn-1",
    parent_run_id: str | None = None,
) -> Checkpoint:
    state = Checkpoint.create(
        thread_id=thread_id,
        turn_id=turn_id,
        run_id=run_id,
        input="test",
        parent_run_id=parent_run_id,
    )
    await store.save(state)
    return state


def _service(tmp_path, *, store=None, observability=None, artifacts=None):
    runtime = MemoryCheckpointStore()
    asyncio.run(_save_run(runtime, "run-root"))
    service = ProvenanceService(
        store=store or MemoryProvenanceStore(),
        runtime_store=runtime,
        workspace=tmp_path,
        artifact_service=artifacts,
        observability_store=observability,
        sensitive_path_patterns=(".env", ".env.*", "*.key", "*token*"),
    )
    return service, runtime


def _artifacts(tmp_path) -> ArtifactService:
    return ArtifactService(
        metadata=MemoryArtifactMetadataStore(),
        blobs=LocalArtifactBlobStore(tmp_path / "blobs"),
        max_file_bytes=1024,
        max_metadata_bytes=1024,
    )


def test_records_code_claim_and_detects_stale_snapshot(tmp_path) -> None:
    source = tmp_path / "answer.py"
    source.write_text("first\nanswer = 42\nthird\n", encoding="utf-8")
    service, _ = _service(tmp_path)
    claim, evidence = asyncio.run(
        service.record_claim(
            invocation_id="run-root:claim-1",
            run_id="run-root",
            thread_id="thread-1",
            turn_id="turn-1",
            text="The configured answer is 42.",
            evidence_descriptors=[
                {"type": "code", "path": "answer.py", "start_line": 2, "end_line": 2}
            ],
        )
    )

    assert claim.citation_marker.startswith("[claim:clm_")
    assert evidence[0].locator["excerpt"] == "answer = 42"
    assert asyncio.run(service.evidence_integrity(claim, evidence[0])) == EvidenceIntegrity.VALID
    source.write_text("first\nanswer = 43\nthird\n", encoding="utf-8")
    assert (
        asyncio.run(service.evidence_integrity(claim, evidence[0]))
        == EvidenceIntegrity.STALE_CODE_SNAPSHOT
    )


def test_rejects_sensitive_code_path(tmp_path) -> None:
    (tmp_path / ".env").write_text("not-read", encoding="utf-8")
    service, _ = _service(tmp_path)
    with pytest.raises(ValueError, match="sensitive"):
        asyncio.run(
            service.record_claim(
                invocation_id="run-root:claim-1",
                run_id="run-root",
                thread_id="thread-1",
                turn_id="turn-1",
                text="secret",
                evidence_descriptors=[
                    {"type": "code", "path": ".env", "start_line": 1, "end_line": 1}
                ],
            )
        )


def test_invalid_bundle_is_atomic(tmp_path) -> None:
    provenance = MemoryProvenanceStore()
    service, _ = _service(tmp_path, store=provenance)
    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(
            service.record_claim(
                invocation_id="run-root:claim-1",
                run_id="run-root",
                thread_id="thread-1",
                turn_id="turn-1",
                text="unsupported",
                evidence_descriptors=[{"type": "tool", "invocation_id": "missing"}],
            )
        )
    assert provenance.list_run_claims("run-root") == []


def test_recording_is_idempotent_by_tool_invocation(tmp_path) -> None:
    (tmp_path / "source.txt").write_text("evidence\n", encoding="utf-8")
    service, _ = _service(tmp_path)
    arguments = dict(
        invocation_id="run-root:claim-1",
        run_id="run-root",
        thread_id="thread-1",
        turn_id="turn-1",
        text="one",
        evidence_descriptors=[
            {"type": "code", "path": "source.txt", "start_line": 1, "end_line": 1}
        ],
    )
    first, first_evidence = asyncio.run(service.record_claim(**arguments))
    replay, replay_evidence = asyncio.run(service.record_claim(**{**arguments, "text": "two"}))
    assert replay.claim_id == first.claim_id
    assert replay.text == "one"
    assert replay_evidence[0].evidence_id == first_evidence[0].evidence_id


def test_tool_evidence_allows_descendant_run_but_not_unrelated_run(tmp_path) -> None:
    service, runtime = _service(tmp_path)
    asyncio.run(_save_run(runtime, "run-child", parent_run_id="run-root"))
    asyncio.run(_save_run(runtime, "run-other"))
    for run_id in ("run-child", "run-other"):
        asyncio.run(
            runtime.save_tool_execution(
                ToolExecutionRecord(
                    invocation_id=f"{run_id}:call-1",
                    run_id=run_id,
                    tool_call_id="call-1",
                    tool_name="search_code",
                    arguments_hash="hash",
                    status=ToolExecutionStatus.SUCCEEDED,
                )
            )
        )
    claim, evidence = asyncio.run(
        service.record_claim(
            invocation_id="run-root:claim-child",
            run_id="run-root",
            thread_id="thread-1",
            turn_id="turn-1",
            text="child evidence",
            evidence_descriptors=[{"type": "tool", "invocation_id": "run-child:call-1"}],
        )
    )
    assert evidence[0].source_run_id == "run-child"
    assert asyncio.run(service.evidence_integrity(claim, evidence[0])) == EvidenceIntegrity.VALID
    with pytest.raises(ValueError, match="lineage"):
        asyncio.run(
            service.record_claim(
                invocation_id="run-root:claim-other",
                run_id="run-root",
                thread_id="thread-1",
                turn_id="turn-1",
                text="unrelated",
                evidence_descriptors=[{"type": "tool", "invocation_id": "run-other:call-1"}],
            )
        )


def test_trace_span_evidence_exposes_bounded_locator(tmp_path) -> None:
    observations = MemoryObservabilityStore()
    service, runtime = _service(tmp_path, observability=observations)
    trace = Trace(
        trace_id="trace-1",
        run_id="run-root",
        thread_id="thread-1",
        turn_id="turn-1",
        started_at="2026-01-01T00:00:00+00:00",
        status="COMPLETED",
        ended_at="2026-01-01T00:00:01+00:00",
    )
    span = Span(
        span_id=tool_span_id("run-root:call-1"),
        trace_id="trace-1",
        span_type=SpanType.TOOL,
        name="search_code",
        started_at=trace.started_at,
        ended_at=trace.ended_at,
        status=SpanStatus.SUCCEEDED,
        attributes={"secret": "must-not-copy"},
    )
    asyncio.run(observations.save_trace(trace))
    asyncio.run(observations.save_span(span))
    asyncio.run(
        runtime.save_tool_execution(
            ToolExecutionRecord(
                invocation_id="run-root:call-1",
                run_id="run-root",
                tool_call_id="call-1",
                tool_name="search_code",
                arguments_hash="hash",
                status=ToolExecutionStatus.SUCCEEDED,
            )
        )
    )
    claim, evidence = asyncio.run(
        service.record_claim(
            invocation_id="run-root:claim-span",
            run_id="run-root",
            thread_id="thread-1",
            turn_id="turn-1",
            text="span completed",
            evidence_descriptors=[{"type": "span", "tool_call_id": "call-1"}],
        )
    )
    assert evidence[0].locator["duration_ms"] == 1000.0
    assert "attributes" not in evidence[0].locator
    assert asyncio.run(service.evidence_integrity(claim, evidence[0])) == EvidenceIntegrity.VALID


def test_artifact_evidence_resolves_reuse_chain_and_missing_blob(tmp_path) -> None:
    artifacts = _artifacts(tmp_path)
    source, _ = artifacts.publish_bytes(
        b"same bytes",
        run_id="run-root",
        thread_id="thread-1",
        invocation_id="run-root:publish",
        tool_name="publish_artifact",
        name="report.txt",
    )
    descriptor = ReuseDescriptor("report", "producer", "v1", {"format": "txt"})
    artifacts.register_reuse(descriptor, source.artifact_id)
    reused = artifacts.reuse(
        descriptor,
        run_id="run-child",
        thread_id="thread-1",
        invocation_id="run-child:reuse",
        tool_name="producer",
    )
    assert reused is not None
    service, runtime = _service(tmp_path, artifacts=artifacts)
    asyncio.run(_save_run(runtime, "run-child", parent_run_id="run-root"))
    claim, evidence = asyncio.run(
        service.record_claim(
            invocation_id="run-root:claim-artifact",
            run_id="run-root",
            thread_id="thread-1",
            turn_id="turn-1",
            text="artifact reused",
            evidence_descriptors=[{"type": "artifact", "artifact_id": reused.artifact_id}],
        )
    )
    view = asyncio.run(service.provenance(claim.claim_id))
    assert view is not None
    assert view["evidence"][0]["locator"]["reuse_chain"][0]["artifact_id"] == source.artifact_id
    assert asyncio.run(service.evidence_integrity(claim, evidence[0])) == EvidenceIntegrity.VALID
    blob = artifacts.metadata.get_blob(reused.blob_sha256)
    assert blob is not None
    (tmp_path / "blobs" / blob.storage_key).unlink()
    assert (
        asyncio.run(service.evidence_integrity(claim, evidence[0]))
        == EvidenceIntegrity.MISSING_BLOB
    )


def test_sqlite_provenance_survives_reconstruction(tmp_path) -> None:
    database = tmp_path / "runtime.db"
    source = tmp_path / "source.txt"
    source.write_text("durable\n", encoding="utf-8")
    first, _ = _service(tmp_path, store=SQLiteProvenanceStore(database))
    claim, _ = asyncio.run(
        first.record_claim(
            invocation_id="run-root:claim-1",
            run_id="run-root",
            thread_id="thread-1",
            turn_id="turn-1",
            text="durable claim",
            evidence_descriptors=[
                {"type": "code", "path": "source.txt", "start_line": 1, "end_line": 1}
            ],
        )
    )
    reopened = SQLiteProvenanceStore(database)
    assert reopened.get_claim(claim.claim_id) == claim
    assert len(reopened.list_claim_evidence(claim.claim_id)) == 1


def test_citation_parser_deduplicates_and_ignores_malformed_markers() -> None:
    assert parse_claim_citations(
        "[claim:clm_abc] x [claim:clm_abc] [claim:bad] [claim:clm_123]"
    ) == ["clm_abc", "clm_123"]


def test_completion_checks_validate_claim_evidence_and_citations(tmp_path) -> None:
    (tmp_path / "source.txt").write_text("evidence\n", encoding="utf-8")
    service, runtime = _service(tmp_path)
    claim, _ = asyncio.run(
        service.record_claim(
            invocation_id="run-root:claim-1",
            run_id="run-root",
            thread_id="thread-1",
            turn_id="turn-1",
            text="supported",
            evidence_descriptors=[
                {"type": "code", "path": "source.txt", "start_line": 1, "end_line": 1}
            ],
        )
    )
    state = asyncio.run(runtime.load("run-root"))
    assert state is not None
    state.output_text = f"Result {claim.citation_marker}"
    contract = CompletionContract.from_dict(
        {
            "checks": [
                {"type": "claims_have_evidence"},
                {"type": "claim_citations_resolve", "require_at_least_one": True},
            ]
        }
    )
    result = asyncio.run(
        CompletionVerifier().verify(
            state,
            contract,
            store=runtime,
            cwd=str(tmp_path),
            attempt=1,
            provenance_service=service,
        )
    )
    assert result.verified


def test_unresolved_citation_is_reported(tmp_path) -> None:
    service, _ = _service(tmp_path)
    passed, reason = asyncio.run(
        service.citations_resolve(
            "run-root",
            "Unsupported [claim:clm_missing]",
            require_at_least_one=True,
        )
    )
    assert not passed
    assert "unresolved" in reason


def test_provenance_config_is_opt_in_and_environment_can_enable(tmp_path) -> None:
    assert not AxiomConfig().provenance.enabled
    config = load_config(
        project_root=tmp_path,
        env={
            "AXIOM_PROVENANCE_ENABLED": "true",
            "AXIOM_PROVENANCE_MAX_CODE_LINES": "12",
        },
    )
    assert config.provenance.enabled
    assert config.provenance.max_code_lines == 12


class _FakeRequest:
    def __init__(self, path: str) -> None:
        self.command = "GET"
        self.path = path
        self.headers = {"content-length": "0", "x-api-key": "test-key"}
        self.rfile = BytesIO()
        self.wfile = BytesIO()
        self.status = 0

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _name: str, _value: str) -> None:
        return

    def end_headers(self) -> None:
        return


def test_claim_api_lists_and_resolves_provenance(tmp_path) -> None:
    (tmp_path / "source.txt").write_text("evidence\n", encoding="utf-8")
    config = AxiomConfig()
    config.provenance.enabled = True
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
    claim, _ = asyncio.run(
        server.provenance.record_claim(
            invocation_id="run-1:claim-1",
            run_id="run-1",
            thread_id=thread_id,
            turn_id="turn-1",
            text="supported",
            evidence_descriptors=[
                {"type": "code", "path": "source.txt", "start_line": 1, "end_line": 1}
            ],
        )
    )
    listing = _FakeRequest("/v1/runs/run-1/claims")
    server._handle(listing)  # type: ignore[arg-type]
    assert listing.status == 200
    assert json.loads(listing.wfile.getvalue())["claims"][0]["claim_id"] == claim.claim_id
    detail = _FakeRequest(f"/v1/claims/{claim.claim_id}/provenance")
    server._handle(detail)  # type: ignore[arg-type]
    assert detail.status == 200
    assert json.loads(detail.wfile.getvalue())["evidence"][0]["source_type"] == "CODE_LOCATION"


def test_tool_context_accepts_provenance_service(tmp_path) -> None:
    service, _ = _service(tmp_path)
    context = ToolContext(cwd=str(tmp_path), config=AxiomConfig(), provenance_service=service)
    assert context.provenance_service is service


def test_record_claim_tool_returns_durable_citation_marker(tmp_path) -> None:
    (tmp_path / "source.txt").write_text("evidence\n", encoding="utf-8")
    service, _ = _service(tmp_path)
    result = asyncio.run(
        record_claim(
            {
                "text": "supported",
                "evidence": [
                    {
                        "type": "code",
                        "path": "source.txt",
                        "start_line": 1,
                        "end_line": 1,
                    }
                ],
            },
            ToolContext(
                cwd=str(tmp_path),
                config=AxiomConfig(),
                provenance_service=service,
                invocation_id="run-root:claim-tool",
                run_id="run-root",
                thread_id="thread-1",
                turn_id="turn-1",
            ),
        )
    )
    assert not result.is_error
    assert result.metadata["citation_marker"].startswith("[claim:clm_")
