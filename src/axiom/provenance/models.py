from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def provenance_now() -> str:
    return datetime.now(UTC).isoformat()


class EvidenceSourceType(StrEnum):
    TOOL_EXECUTION = "TOOL_EXECUTION"
    ARTIFACT = "ARTIFACT"
    CODE_LOCATION = "CODE_LOCATION"
    TRACE_SPAN = "TRACE_SPAN"


class EvidenceIntegrity(StrEnum):
    VALID = "VALID"
    MISSING_SOURCE = "MISSING_SOURCE"
    INVALID_SCOPE = "INVALID_SCOPE"
    STALE_CODE_SNAPSHOT = "STALE_CODE_SNAPSHOT"
    MISSING_BLOB = "MISSING_BLOB"


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    invocation_id: str
    run_id: str
    thread_id: str
    turn_id: str
    text: str
    claim_kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=provenance_now)

    @property
    def citation_marker(self) -> str:
        return f"[claim:{self.claim_id}]"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    run_id: str
    thread_id: str
    turn_id: str
    source_type: EvidenceSourceType
    source_id: str
    source_run_id: str
    summary: str
    locator: dict[str, Any]
    source_digest: str | None = None
    created_at: str = field(default_factory=provenance_now)


@dataclass(frozen=True, slots=True)
class ClaimEvidenceLink:
    claim_id: str
    evidence_id: str
    relation: str = "supports"
    created_at: str = field(default_factory=provenance_now)
