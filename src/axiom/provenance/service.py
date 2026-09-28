from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from axiom.artifacts import ArtifactService
from axiom.policy import PathGuard
from axiom.provenance.models import (
    ClaimEvidenceLink,
    ClaimRecord,
    EvidenceIntegrity,
    EvidenceRecord,
    EvidenceSourceType,
)
from axiom.provenance.store import ProvenanceStore

if TYPE_CHECKING:
    from axiom.runtime.checkpoints import RuntimeStore
    from axiom.runtime.observability_store import ObservabilityStore

_CITATION_PATTERN = re.compile(r"\[claim:(clm_[A-Za-z0-9]+)\]")


class ProvenanceValidationError(ValueError):
    pass


class ProvenanceService:
    def __init__(
        self,
        *,
        store: ProvenanceStore,
        runtime_store: RuntimeStore,
        workspace: str | Path,
        artifact_service: ArtifactService | None = None,
        observability_store: ObservabilityStore | None = None,
        sensitive_path_patterns: tuple[str, ...] = (),
        max_claim_chars: int = 4_000,
        max_summary_chars: int = 1_000,
        max_metadata_bytes: int = 16 * 1024,
        max_code_lines: int = 60,
        max_excerpt_chars: int = 8_000,
        max_evidence_per_claim: int = 16,
    ) -> None:
        self.store = store
        self.runtime_store = runtime_store
        self.workspace = Path(workspace).resolve()
        self.artifact_service = artifact_service
        self.observability_store = observability_store
        self.sensitive_path_patterns = sensitive_path_patterns
        self.max_claim_chars = max_claim_chars
        self.max_summary_chars = max_summary_chars
        self.max_metadata_bytes = max_metadata_bytes
        self.max_code_lines = max_code_lines
        self.max_excerpt_chars = max_excerpt_chars
        self.max_evidence_per_claim = max_evidence_per_claim

    async def record_claim(
        self,
        *,
        invocation_id: str,
        run_id: str,
        thread_id: str,
        turn_id: str,
        text: str,
        evidence_descriptors: list[Mapping[str, Any]],
        claim_kind: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[ClaimRecord, list[EvidenceRecord]]:
        existing = await asyncio.to_thread(self.store.get_claim_by_invocation, invocation_id)
        if existing is not None:
            rows = await asyncio.to_thread(self.store.list_claim_evidence, existing.claim_id)
            return existing, [item for _, item in rows]
        normalized_text = text.strip()
        if not normalized_text:
            raise ProvenanceValidationError("claim text is required")
        if len(normalized_text) > self.max_claim_chars:
            raise ProvenanceValidationError(
                f"claim text exceeds configured limit of {self.max_claim_chars} characters"
            )
        if not evidence_descriptors:
            raise ProvenanceValidationError("at least one evidence source is required")
        if len(evidence_descriptors) > self.max_evidence_per_claim:
            raise ProvenanceValidationError(
                f"claim exceeds configured limit of {self.max_evidence_per_claim} evidence sources"
            )
        run = await self.runtime_store.load(run_id)
        if run is None or run.thread_id != thread_id or run.turn_id != turn_id:
            raise ProvenanceValidationError("claim durable Run context is invalid")
        clean_metadata = self._bounded_object(metadata or {}, "claim metadata")
        resolved: list[EvidenceRecord] = []
        for descriptor in evidence_descriptors:
            resolved.append(
                await self._resolve_descriptor(
                    descriptor,
                    claim_run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
            )
        claim = ClaimRecord(
            claim_id=f"clm_{uuid4().hex}",
            invocation_id=invocation_id,
            run_id=run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            text=normalized_text,
            claim_kind=claim_kind.strip()[:64] if claim_kind and claim_kind.strip() else None,
            metadata=clean_metadata,
        )
        links = [
            ClaimEvidenceLink(claim_id=claim.claim_id, evidence_id=item.evidence_id)
            for item in resolved
        ]
        stored = await asyncio.to_thread(self.store.create_bundle, claim, resolved, links)
        if stored.claim_id != claim.claim_id:
            rows = await asyncio.to_thread(self.store.list_claim_evidence, stored.claim_id)
            return stored, [item for _, item in rows]
        return stored, resolved

    async def list_run_claims(self, run_id: str) -> list[dict[str, Any]]:
        claims = await asyncio.to_thread(self.store.list_run_claims, run_id)
        return [await self.claim_summary(claim) for claim in claims]

    async def claim_summary(
        self, claim: ClaimRecord, *, verify_artifact_blob: bool = False
    ) -> dict[str, Any]:
        rows = await asyncio.to_thread(self.store.list_claim_evidence, claim.claim_id)
        statuses = [
            await self.evidence_integrity(
                claim,
                evidence,
                verify_artifact_blob=verify_artifact_blob,
            )
            for _, evidence in rows
        ]
        return {
            "claim_id": claim.claim_id,
            "run_id": claim.run_id,
            "thread_id": claim.thread_id,
            "turn_id": claim.turn_id,
            "text": claim.text,
            "claim_kind": claim.claim_kind,
            "evidence_count": len(rows),
            "citation_marker": claim.citation_marker,
            "integrity": _aggregate_integrity(statuses).value,
            "created_at": claim.created_at,
        }

    async def get_claim(self, claim_id: str) -> ClaimRecord | None:
        return await asyncio.to_thread(self.store.get_claim, claim_id)

    async def provenance(self, claim_id: str) -> dict[str, Any] | None:
        claim = await self.get_claim(claim_id)
        if claim is None:
            return None
        rows = await asyncio.to_thread(self.store.list_claim_evidence, claim_id)
        evidence = []
        for link, item in rows:
            status = await self.evidence_integrity(claim, item)
            evidence.append(
                {
                    "evidence_id": item.evidence_id,
                    "source_type": item.source_type.value,
                    "source_id": item.source_id,
                    "source_run_id": item.source_run_id,
                    "relation": link.relation,
                    "summary": item.summary,
                    "locator": await self._public_locator(item),
                    "source_digest": item.source_digest,
                    "integrity": status.value,
                    "created_at": item.created_at,
                }
            )
        return {
            "claim": await self.claim_summary(claim, verify_artifact_blob=True),
            "evidence": evidence,
        }

    async def claims_have_evidence(
        self,
        run_id: str,
        *,
        min_claims: int = 1,
        min_evidence_per_claim: int = 1,
    ) -> tuple[bool, str]:
        claims = await asyncio.to_thread(self.store.list_run_claims, run_id)
        if len(claims) < min_claims:
            return False, f"recorded claims {len(claims)} is below required {min_claims}"
        for claim in claims:
            rows = await asyncio.to_thread(self.store.list_claim_evidence, claim.claim_id)
            valid = 0
            for _, evidence in rows:
                if await self.evidence_integrity(claim, evidence) == EvidenceIntegrity.VALID:
                    valid += 1
            if valid < min_evidence_per_claim:
                return (
                    False,
                    f"claim {claim.claim_id} has {valid} valid evidence sources; "
                    f"requires {min_evidence_per_claim}",
                )
        return True, "recorded claims have sufficient structurally valid evidence"

    async def citations_resolve(
        self,
        run_id: str,
        output: str,
        *,
        require_at_least_one: bool = False,
    ) -> tuple[bool, str]:
        claim_ids = parse_claim_citations(output)
        if require_at_least_one and not claim_ids:
            return False, "final output has no claim citation marker"
        for claim_id in claim_ids:
            claim = await self.get_claim(claim_id)
            if claim is None:
                return False, f"unresolved claim citation: {claim_id}"
            viewer = await self.runtime_store.load(run_id)
            if (
                viewer is None
                or viewer.thread_id != claim.thread_id
                or not await self._run_in_scope(run_id, claim.run_id)
            ):
                return False, f"claim citation is outside Run lineage: {claim_id}"
            rows = await asyncio.to_thread(self.store.list_claim_evidence, claim_id)
            if not rows:
                return False, f"claim citation has no evidence: {claim_id}"
            for _, evidence in rows:
                if await self.evidence_integrity(claim, evidence) != EvidenceIntegrity.VALID:
                    return False, f"claim citation has invalid evidence: {claim_id}"
        return True, "claim citations resolve to structurally valid evidence"

    async def evidence_integrity(
        self,
        claim: ClaimRecord,
        evidence: EvidenceRecord,
        *,
        verify_artifact_blob: bool = True,
    ) -> EvidenceIntegrity:
        source_run = await self.runtime_store.load(evidence.source_run_id)
        if source_run is None:
            return EvidenceIntegrity.MISSING_SOURCE
        if source_run.thread_id != claim.thread_id or not await self._run_in_scope(
            claim.run_id, evidence.source_run_id
        ):
            return EvidenceIntegrity.INVALID_SCOPE
        if evidence.source_type == EvidenceSourceType.TOOL_EXECUTION:
            record = await self.runtime_store.load_tool_execution(evidence.source_id)
            return (
                EvidenceIntegrity.VALID
                if record is not None and record.run_id == evidence.source_run_id
                else EvidenceIntegrity.MISSING_SOURCE
            )
        if evidence.source_type == EvidenceSourceType.ARTIFACT:
            if self.artifact_service is None:
                return EvidenceIntegrity.MISSING_SOURCE
            artifact = await asyncio.to_thread(self.artifact_service.get, evidence.source_id)
            if artifact is None or artifact.run_id != evidence.source_run_id:
                return EvidenceIntegrity.MISSING_SOURCE
            blob = await asyncio.to_thread(
                self.artifact_service.metadata.get_blob, artifact.blob_sha256
            )
            if blob is None:
                return EvidenceIntegrity.MISSING_BLOB
            if verify_artifact_blob and not await asyncio.to_thread(
                self.artifact_service.blobs.exists, blob.storage_key
            ):
                return EvidenceIntegrity.MISSING_BLOB
            return EvidenceIntegrity.VALID
        if evidence.source_type == EvidenceSourceType.CODE_LOCATION:
            try:
                path = self._safe_code_path(str(evidence.locator.get("path") or ""))
            except (ValueError, OSError):
                return EvidenceIntegrity.MISSING_SOURCE
            if not path.is_file():
                return EvidenceIntegrity.MISSING_SOURCE
            current = await asyncio.to_thread(_sha256_file, path)
            return (
                EvidenceIntegrity.VALID
                if current == evidence.source_digest
                else EvidenceIntegrity.STALE_CODE_SNAPSHOT
            )
        if evidence.source_type == EvidenceSourceType.TRACE_SPAN:
            if self.observability_store is None:
                return EvidenceIntegrity.MISSING_SOURCE
            span = await self.observability_store.load_span(evidence.source_id)
            trace = await self.observability_store.load_trace(evidence.source_run_id)
            return (
                EvidenceIntegrity.VALID
                if span is not None and trace is not None and span.trace_id == trace.trace_id
                else EvidenceIntegrity.MISSING_SOURCE
            )
        return EvidenceIntegrity.MISSING_SOURCE

    async def _resolve_descriptor(
        self,
        descriptor: Mapping[str, Any],
        *,
        claim_run_id: str,
        thread_id: str,
        turn_id: str,
    ) -> EvidenceRecord:
        raw_type = str(descriptor.get("type") or descriptor.get("source_type") or "").strip()
        normalized = raw_type.upper().replace("-", "_")
        aliases = {
            "TOOL": EvidenceSourceType.TOOL_EXECUTION,
            "TOOL_EXECUTION": EvidenceSourceType.TOOL_EXECUTION,
            "ARTIFACT": EvidenceSourceType.ARTIFACT,
            "CODE": EvidenceSourceType.CODE_LOCATION,
            "CODE_LOCATION": EvidenceSourceType.CODE_LOCATION,
            "SPAN": EvidenceSourceType.TRACE_SPAN,
            "TRACE_SPAN": EvidenceSourceType.TRACE_SPAN,
        }
        source_type = aliases.get(normalized)
        if source_type is None:
            raise ProvenanceValidationError(f"unsupported evidence source type: {raw_type}")
        if source_type == EvidenceSourceType.TOOL_EXECUTION:
            return await self._resolve_tool(descriptor, claim_run_id, thread_id, turn_id)
        if source_type == EvidenceSourceType.ARTIFACT:
            return await self._resolve_artifact(descriptor, claim_run_id, thread_id, turn_id)
        if source_type == EvidenceSourceType.CODE_LOCATION:
            return await self._resolve_code(descriptor, claim_run_id, thread_id, turn_id)
        return await self._resolve_span(descriptor, claim_run_id, thread_id, turn_id)

    async def _resolve_tool(
        self, descriptor: Mapping[str, Any], claim_run_id: str, thread_id: str, turn_id: str
    ) -> EvidenceRecord:
        source_run_id = str(descriptor.get("run_id") or claim_run_id)
        invocation_id = str(descriptor.get("invocation_id") or "").strip()
        tool_call_id = str(descriptor.get("tool_call_id") or "").strip()
        if not invocation_id and tool_call_id:
            invocation_id = f"{source_run_id}:{tool_call_id}"
        if not invocation_id:
            raise ProvenanceValidationError("tool evidence requires invocation_id or tool_call_id")
        record = await self.runtime_store.load_tool_execution(invocation_id)
        if record is None:
            raise ProvenanceValidationError("tool execution evidence source does not exist")
        source_run_id = record.run_id
        await self._require_scope(claim_run_id, source_run_id, thread_id)
        locator = {
            "invocation_id": record.invocation_id,
            "tool_call_id": record.tool_call_id,
            "tool_name": record.tool_name,
            "status": record.status.value,
            "completed_at": record.completed_at,
            "artifact_ids": list(record.artifact_ids),
        }
        return self._evidence(
            EvidenceSourceType.TOOL_EXECUTION,
            record.invocation_id,
            source_run_id,
            claim_run_id,
            thread_id,
            turn_id,
            f"{record.tool_name} · {record.status.value}",
            locator,
            _digest(locator),
        )

    async def _resolve_artifact(
        self, descriptor: Mapping[str, Any], claim_run_id: str, thread_id: str, turn_id: str
    ) -> EvidenceRecord:
        artifact_id = str(descriptor.get("artifact_id") or "").strip()
        if not artifact_id or self.artifact_service is None:
            raise ProvenanceValidationError("artifact evidence source is unavailable")
        artifact = await asyncio.to_thread(self.artifact_service.get, artifact_id)
        if artifact is None:
            raise ProvenanceValidationError("artifact evidence source does not exist")
        await self._require_scope(claim_run_id, artifact.run_id, thread_id)
        blob = await asyncio.to_thread(
            self.artifact_service.metadata.get_blob, artifact.blob_sha256
        )
        if blob is None:
            raise ProvenanceValidationError("artifact blob metadata does not exist")
        if not await asyncio.to_thread(self.artifact_service.blobs.exists, blob.storage_key):
            raise ProvenanceValidationError("artifact blob does not exist")
        locator = {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.blob_sha256,
            "tool_name": artifact.tool_name,
            "invocation_id": artifact.invocation_id,
            "reused": artifact.reused,
            "reused_from_artifact_id": artifact.reused_from_artifact_id,
        }
        return self._evidence(
            EvidenceSourceType.ARTIFACT,
            artifact.artifact_id,
            artifact.run_id,
            claim_run_id,
            thread_id,
            turn_id,
            f"Artifact {artifact.name} · {artifact.size_bytes} bytes",
            locator,
            artifact.blob_sha256,
        )

    async def _resolve_code(
        self, descriptor: Mapping[str, Any], claim_run_id: str, thread_id: str, turn_id: str
    ) -> EvidenceRecord:
        relative = str(descriptor.get("path") or "").strip()
        path = self._safe_code_path(relative)
        if not path.is_file():
            raise ProvenanceValidationError("code evidence source does not exist")
        start_line = int(descriptor.get("start_line") or 0)
        end_line = int(descriptor.get("end_line") or 0)
        if start_line < 1 or end_line < start_line:
            raise ProvenanceValidationError("code evidence line range is invalid")
        if end_line - start_line + 1 > self.max_code_lines:
            raise ProvenanceValidationError(
                f"code evidence exceeds configured limit of {self.max_code_lines} lines"
            )
        file_sha256, excerpt, line_count = await asyncio.to_thread(
            _capture_code_snapshot,
            path,
            start_line,
            end_line,
        )
        if start_line > line_count or end_line > line_count:
            raise ProvenanceValidationError("code evidence line range exceeds file length")
        if len(excerpt) > self.max_excerpt_chars:
            raise ProvenanceValidationError(
                "code evidence excerpt exceeds configured limit of "
                f"{self.max_excerpt_chars} characters"
            )
        safe_relative = path.relative_to(self.workspace).as_posix()
        locator = {
            "path": safe_relative,
            "start_line": start_line,
            "end_line": end_line,
            "captured_sha256": file_sha256,
            "excerpt": excerpt,
        }
        return self._evidence(
            EvidenceSourceType.CODE_LOCATION,
            f"{safe_relative}:{start_line}-{end_line}",
            claim_run_id,
            claim_run_id,
            thread_id,
            turn_id,
            f"{safe_relative}:{start_line}-{end_line}",
            locator,
            file_sha256,
        )

    async def _resolve_span(
        self, descriptor: Mapping[str, Any], claim_run_id: str, thread_id: str, turn_id: str
    ) -> EvidenceRecord:
        if self.observability_store is None:
            raise ProvenanceValidationError("trace evidence source is unavailable")
        span_id = str(descriptor.get("span_id") or "").strip()
        source_run_id = str(descriptor.get("run_id") or claim_run_id)
        if not span_id:
            invocation_id = str(descriptor.get("invocation_id") or "").strip()
            tool_call_id = str(descriptor.get("tool_call_id") or "").strip()
            if not invocation_id and tool_call_id:
                invocation_id = f"{source_run_id}:{tool_call_id}"
            record = (
                await self.runtime_store.load_tool_execution(invocation_id)
                if invocation_id
                else None
            )
            if record is None:
                raise ProvenanceValidationError(
                    "trace span evidence requires span_id or a real Tool invocation"
                )
            source_run_id = record.run_id
            span_id = _tool_span_id(record.invocation_id)
        span = await self.observability_store.load_span(span_id)
        trace = await self.observability_store.load_trace(source_run_id)
        if span is None or trace is None or span.trace_id != trace.trace_id:
            raise ProvenanceValidationError("trace span evidence source does not exist")
        await self._require_scope(claim_run_id, source_run_id, thread_id)
        locator = {
            "span_id": span.span_id,
            "span_type": span.span_type.value,
            "name": span.name,
            "status": span.status.value,
            "duration_ms": _duration_ms(span.started_at, span.ended_at),
            "started_at": span.started_at,
            "ended_at": span.ended_at,
        }
        return self._evidence(
            EvidenceSourceType.TRACE_SPAN,
            span.span_id,
            source_run_id,
            claim_run_id,
            thread_id,
            turn_id,
            f"{span.span_type.value} span · {span.name}",
            locator,
            _digest(locator),
        )

    def _evidence(
        self,
        source_type: EvidenceSourceType,
        source_id: str,
        source_run_id: str,
        claim_run_id: str,
        thread_id: str,
        turn_id: str,
        summary: str,
        locator: dict[str, Any],
        digest: str | None,
    ) -> EvidenceRecord:
        clean_summary = summary.strip()[: self.max_summary_chars]
        clean_locator = self._bounded_object(locator, "evidence locator")
        return EvidenceRecord(
            evidence_id=f"evd_{uuid4().hex}",
            run_id=claim_run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            source_type=source_type,
            source_id=source_id,
            source_run_id=source_run_id,
            summary=clean_summary,
            locator=clean_locator,
            source_digest=digest,
        )

    async def _require_scope(self, claim_run_id: str, source_run_id: str, thread_id: str) -> None:
        source = await self.runtime_store.load(source_run_id)
        if source is None or source.thread_id != thread_id:
            raise ProvenanceValidationError("evidence source is outside the Claim thread")
        if not await self._run_in_scope(claim_run_id, source_run_id):
            raise ProvenanceValidationError("evidence source is outside the Claim Run lineage")

    async def _run_in_scope(self, claim_run_id: str, source_run_id: str) -> bool:
        current_id: str | None = source_run_id
        visited: set[str] = set()
        for _ in range(64):
            if current_id is None or current_id in visited:
                return False
            if current_id == claim_run_id:
                return True
            visited.add(current_id)
            state = await self.runtime_store.load(current_id)
            if state is None:
                return False
            current_id = state.parent_run_id
        return False

    def _safe_code_path(self, value: str) -> Path:
        if not value:
            raise ProvenanceValidationError("code evidence path is required")
        path = PathGuard(self.workspace).validate(value)
        if any(
            fnmatch(path.name.casefold(), pattern.casefold())
            for pattern in self.sensitive_path_patterns
        ):
            raise ProvenanceValidationError("sensitive code path is protected by policy")
        return path

    def _bounded_object(self, value: Mapping[str, Any], label: str) -> dict[str, Any]:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > self.max_metadata_bytes:
            raise ProvenanceValidationError(
                f"{label} exceeds configured limit of {self.max_metadata_bytes} bytes"
            )
        parsed = json.loads(encoded)
        if not isinstance(parsed, dict):
            raise ProvenanceValidationError(f"{label} must be an object")
        return parsed

    async def _public_locator(self, evidence: EvidenceRecord) -> dict[str, Any]:
        locator = dict(evidence.locator)
        if evidence.source_type == EvidenceSourceType.ARTIFACT and self.artifact_service:
            chain: list[dict[str, str]] = []
            current = locator.get("reused_from_artifact_id")
            visited: set[str] = set()
            for _ in range(4):
                if not isinstance(current, str) or not current or current in visited:
                    break
                visited.add(current)
                artifact = await asyncio.to_thread(self.artifact_service.get, current)
                if artifact is None:
                    break
                chain.append({"artifact_id": artifact.artifact_id, "sha256": artifact.blob_sha256})
                current = artifact.reused_from_artifact_id
            if chain:
                locator["reuse_chain"] = chain
        return locator


def parse_claim_citations(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in _CITATION_PATTERN.finditer(text):
        claim_id = match.group(1)
        if claim_id not in seen:
            seen.add(claim_id)
            result.append(claim_id)
    return result


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_code_snapshot(path: Path, start_line: int, end_line: int) -> tuple[str, str, int]:
    digest = hashlib.sha256()
    selected: list[str] = []
    line_count = 0
    with path.open("rb") as handle:
        for line_count, raw_line in enumerate(handle, start=1):
            digest.update(raw_line)
            if start_line <= line_count <= end_line:
                selected.append(raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))
    return digest.hexdigest(), "\n".join(selected), line_count


def _tool_span_id(invocation_id: str) -> str:
    value = hashlib.sha256(invocation_id.encode("utf-8")).hexdigest()[:32]
    return f"span_tool_{value}"


def _duration_ms(started_at: str, ended_at: str | None) -> float | None:
    if ended_at is None:
        return None
    try:
        elapsed = (
            datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)
        ).total_seconds()
    except ValueError:
        return None
    return round(max(0.0, elapsed * 1000), 3)


def _aggregate_integrity(statuses: list[EvidenceIntegrity]) -> EvidenceIntegrity:
    if not statuses:
        return EvidenceIntegrity.MISSING_SOURCE
    priority = (
        EvidenceIntegrity.INVALID_SCOPE,
        EvidenceIntegrity.MISSING_BLOB,
        EvidenceIntegrity.MISSING_SOURCE,
        EvidenceIntegrity.STALE_CODE_SNAPSHOT,
    )
    for status in priority:
        if status in statuses:
            return status
    return EvidenceIntegrity.VALID
