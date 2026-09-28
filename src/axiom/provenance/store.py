from __future__ import annotations

import json
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from axiom.provenance.models import (
    ClaimEvidenceLink,
    ClaimRecord,
    EvidenceRecord,
    EvidenceSourceType,
)


class ProvenanceStore(Protocol):
    backend: str

    def get_claim(self, claim_id: str) -> ClaimRecord | None: ...

    def get_claim_by_invocation(self, invocation_id: str) -> ClaimRecord | None: ...

    def list_run_claims(self, run_id: str) -> list[ClaimRecord]: ...

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None: ...

    def list_claim_evidence(
        self, claim_id: str
    ) -> list[tuple[ClaimEvidenceLink, EvidenceRecord]]: ...

    def create_bundle(
        self,
        claim: ClaimRecord,
        evidence: list[EvidenceRecord],
        links: list[ClaimEvidenceLink],
    ) -> ClaimRecord: ...


class MemoryProvenanceStore:
    backend = "memory"

    def __init__(self) -> None:
        self.claims: dict[str, ClaimRecord] = {}
        self.invocations: dict[str, str] = {}
        self.evidence: dict[str, EvidenceRecord] = {}
        self.links: dict[tuple[str, str], ClaimEvidenceLink] = {}
        self._lock = threading.Lock()

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        value = self.claims.get(claim_id)
        return deepcopy(value) if value is not None else None

    def get_claim_by_invocation(self, invocation_id: str) -> ClaimRecord | None:
        claim_id = self.invocations.get(invocation_id)
        return self.get_claim(claim_id) if claim_id is not None else None

    def list_run_claims(self, run_id: str) -> list[ClaimRecord]:
        return sorted(
            (deepcopy(claim) for claim in self.claims.values() if claim.run_id == run_id),
            key=lambda claim: (claim.created_at, claim.claim_id),
        )

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        value = self.evidence.get(evidence_id)
        return deepcopy(value) if value is not None else None

    def list_claim_evidence(self, claim_id: str) -> list[tuple[ClaimEvidenceLink, EvidenceRecord]]:
        rows = [
            (deepcopy(link), deepcopy(self.evidence[link.evidence_id]))
            for link in self.links.values()
            if link.claim_id == claim_id and link.evidence_id in self.evidence
        ]
        return sorted(rows, key=lambda item: (item[0].created_at, item[0].evidence_id))

    def create_bundle(
        self,
        claim: ClaimRecord,
        evidence: list[EvidenceRecord],
        links: list[ClaimEvidenceLink],
    ) -> ClaimRecord:
        with self._lock:
            existing_id = self.invocations.get(claim.invocation_id)
            if existing_id is not None:
                return deepcopy(self.claims[existing_id])
            self.claims[claim.claim_id] = deepcopy(claim)
            self.invocations[claim.invocation_id] = claim.claim_id
            self.evidence.update({item.evidence_id: deepcopy(item) for item in evidence})
            self.links.update({(item.claim_id, item.evidence_id): deepcopy(item) for item in links})
            return deepcopy(claim)


class SQLiteProvenanceStore:
    backend = "sqlite"

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        with self._connect() as conn:
            row = conn.execute(_CLAIM_SELECT + " where claim_id = ?", (claim_id,)).fetchone()
        return _claim_from_row(row) if row else None

    def get_claim_by_invocation(self, invocation_id: str) -> ClaimRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                _CLAIM_SELECT + " where invocation_id = ?", (invocation_id,)
            ).fetchone()
        return _claim_from_row(row) if row else None

    def list_run_claims(self, run_id: str) -> list[ClaimRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                _CLAIM_SELECT + " where run_id = ? order by created_at, claim_id", (run_id,)
            ).fetchall()
        return [_claim_from_row(row) for row in rows]

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                _EVIDENCE_SELECT + " where evidence_id = ?", (evidence_id,)
            ).fetchone()
        return _evidence_from_row(row) if row else None

    def list_claim_evidence(self, claim_id: str) -> list[tuple[ClaimEvidenceLink, EvidenceRecord]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select l.claim_id, l.evidence_id, l.relation, l.created_at,
                       e.run_id, e.thread_id, e.turn_id, e.source_type, e.source_id,
                       e.source_run_id, e.summary, e.locator_json, e.source_digest, e.created_at
                from claim_evidence l
                join evidence_records e on e.evidence_id = l.evidence_id
                where l.claim_id = ? order by l.created_at, l.evidence_id
                """,
                (claim_id,),
            ).fetchall()
        return [_link_evidence_from_row(row) for row in rows]

    def create_bundle(
        self,
        claim: ClaimRecord,
        evidence: list[EvidenceRecord],
        links: list[ClaimEvidenceLink],
    ) -> ClaimRecord:
        with self._connect() as conn:
            conn.execute("begin immediate")
            existing = conn.execute(
                _CLAIM_SELECT + " where invocation_id = ?", (claim.invocation_id,)
            ).fetchone()
            if existing:
                return _claim_from_row(existing)
            conn.execute(
                """
                insert into claims(
                    claim_id, invocation_id, run_id, thread_id, turn_id, text,
                    claim_kind, metadata_json, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _claim_values(claim),
            )
            conn.executemany(
                """
                insert into evidence_records(
                    evidence_id, run_id, thread_id, turn_id, source_type, source_id,
                    source_run_id, summary, locator_json, source_digest, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_evidence_values(item) for item in evidence],
            )
            conn.executemany(
                "insert into claim_evidence(claim_id, evidence_id, relation, created_at) "
                "values (?, ?, ?, ?)",
                [
                    (item.claim_id, item.evidence_id, item.relation, item.created_at)
                    for item in links
                ],
            )
        return claim

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            for statement in sqlite_provenance_schema():
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma journal_mode = wal")
        conn.execute("pragma busy_timeout = 30000")
        conn.execute("pragma foreign_keys = on")
        return conn


class PostgresProvenanceStore:
    backend = "postgres"

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def get_claim(self, claim_id: str) -> ClaimRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(_CLAIM_SELECT + " where claim_id = %s", (claim_id,)).fetchone()
        return _claim_from_row(row) if row else None

    def get_claim_by_invocation(self, invocation_id: str) -> ClaimRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                _CLAIM_SELECT + " where invocation_id = %s", (invocation_id,)
            ).fetchone()
        return _claim_from_row(row) if row else None

    def list_run_claims(self, run_id: str) -> list[ClaimRecord]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                _CLAIM_SELECT + " where run_id = %s order by created_at, claim_id", (run_id,)
            ).fetchall()
        return [_claim_from_row(row) for row in rows]

    def get_evidence(self, evidence_id: str) -> EvidenceRecord | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                _EVIDENCE_SELECT + " where evidence_id = %s", (evidence_id,)
            ).fetchone()
        return _evidence_from_row(row) if row else None

    def list_claim_evidence(self, claim_id: str) -> list[tuple[ClaimEvidenceLink, EvidenceRecord]]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                """
                select l.claim_id, l.evidence_id, l.relation, l.created_at,
                       e.run_id, e.thread_id, e.turn_id, e.source_type, e.source_id,
                       e.source_run_id, e.summary, e.locator_json, e.source_digest, e.created_at
                from claim_evidence l
                join evidence_records e on e.evidence_id = l.evidence_id
                where l.claim_id = %s order by l.created_at, l.evidence_id
                """,
                (claim_id,),
            ).fetchall()
        return [_link_evidence_from_row(row) for row in rows]

    def create_bundle(
        self,
        claim: ClaimRecord,
        evidence: list[EvidenceRecord],
        links: list[ClaimEvidenceLink],
    ) -> ClaimRecord:
        with self.pool.connection() as conn:
            conn.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (claim.invocation_id,),
            )
            existing = conn.execute(
                _CLAIM_SELECT + " where invocation_id = %s", (claim.invocation_id,)
            ).fetchone()
            if existing:
                return _claim_from_row(existing)
            conn.execute(
                """
                insert into claims(
                    claim_id, invocation_id, run_id, thread_id, turn_id, text,
                    claim_kind, metadata_json, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                _claim_values(claim),
            )
            for item in evidence:
                values = _evidence_values(item)
                conn.execute(
                    """
                    insert into evidence_records(
                        evidence_id, run_id, thread_id, turn_id, source_type, source_id,
                        source_run_id, summary, locator_json, source_digest, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    values,
                )
            for item in links:
                conn.execute(
                    "insert into claim_evidence(claim_id, evidence_id, relation, created_at) "
                    "values (%s, %s, %s, %s)",
                    (item.claim_id, item.evidence_id, item.relation, item.created_at),
                )
        return claim


def sqlite_provenance_schema() -> tuple[str, ...]:
    return (
        """
        create table if not exists claims (
            claim_id text primary key,
            invocation_id text not null unique,
            run_id text not null,
            thread_id text not null,
            turn_id text not null,
            text text not null,
            claim_kind text,
            metadata_json text not null,
            created_at text not null
        )
        """,
        "create index if not exists idx_claims_run on claims(run_id, created_at)",
        """
        create table if not exists evidence_records (
            evidence_id text primary key,
            run_id text not null,
            thread_id text not null,
            turn_id text not null,
            source_type text not null,
            source_id text not null,
            source_run_id text not null,
            summary text not null,
            locator_json text not null,
            source_digest text,
            created_at text not null
        )
        """,
        "create index if not exists idx_evidence_run on evidence_records(run_id, created_at)",
        "create index if not exists idx_evidence_source "
        "on evidence_records(source_type, source_id)",
        """
        create table if not exists claim_evidence (
            claim_id text not null references claims(claim_id),
            evidence_id text not null references evidence_records(evidence_id),
            relation text not null,
            created_at text not null,
            primary key(claim_id, evidence_id)
        )
        """,
        "create index if not exists idx_claim_evidence_claim on claim_evidence(claim_id)",
    )


def postgres_provenance_schema() -> tuple[str, ...]:
    return (
        """
        create table if not exists claims (
            claim_id text primary key,
            invocation_id text not null unique,
            run_id text not null,
            thread_id text not null,
            turn_id text not null,
            text text not null,
            claim_kind text,
            metadata_json jsonb not null default '{}'::jsonb,
            created_at timestamptz not null
        )
        """,
        "create index if not exists idx_claims_run on claims(run_id, created_at)",
        """
        create table if not exists evidence_records (
            evidence_id text primary key,
            run_id text not null,
            thread_id text not null,
            turn_id text not null,
            source_type text not null,
            source_id text not null,
            source_run_id text not null,
            summary text not null,
            locator_json jsonb not null default '{}'::jsonb,
            source_digest text,
            created_at timestamptz not null
        )
        """,
        "create index if not exists idx_evidence_run on evidence_records(run_id, created_at)",
        "create index if not exists idx_evidence_source "
        "on evidence_records(source_type, source_id)",
        """
        create table if not exists claim_evidence (
            claim_id text not null references claims(claim_id),
            evidence_id text not null references evidence_records(evidence_id),
            relation text not null,
            created_at timestamptz not null,
            primary key(claim_id, evidence_id)
        )
        """,
        "create index if not exists idx_claim_evidence_claim on claim_evidence(claim_id)",
    )


_CLAIM_SELECT = (
    "select claim_id, invocation_id, run_id, thread_id, turn_id, text, "
    "claim_kind, metadata_json, created_at from claims"
)
_EVIDENCE_SELECT = (
    "select evidence_id, run_id, thread_id, turn_id, source_type, source_id, "
    "source_run_id, summary, locator_json, source_digest, created_at from evidence_records"
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _timestamp(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _claim_values(claim: ClaimRecord) -> tuple[object, ...]:
    return (
        claim.claim_id,
        claim.invocation_id,
        claim.run_id,
        claim.thread_id,
        claim.turn_id,
        claim.text,
        claim.claim_kind,
        _json(claim.metadata),
        claim.created_at,
    )


def _claim_from_row(row: Any) -> ClaimRecord:
    return ClaimRecord(
        claim_id=str(row[0]),
        invocation_id=str(row[1]),
        run_id=str(row[2]),
        thread_id=str(row[3]),
        turn_id=str(row[4]),
        text=str(row[5]),
        claim_kind=str(row[6]) if row[6] is not None else None,
        metadata=_object(row[7]),
        created_at=_timestamp(row[8]),
    )


def _evidence_values(evidence: EvidenceRecord) -> tuple[object, ...]:
    return (
        evidence.evidence_id,
        evidence.run_id,
        evidence.thread_id,
        evidence.turn_id,
        evidence.source_type.value,
        evidence.source_id,
        evidence.source_run_id,
        evidence.summary,
        _json(evidence.locator),
        evidence.source_digest,
        evidence.created_at,
    )


def _evidence_from_row(row: Any) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=str(row[0]),
        run_id=str(row[1]),
        thread_id=str(row[2]),
        turn_id=str(row[3]),
        source_type=EvidenceSourceType(str(row[4])),
        source_id=str(row[5]),
        source_run_id=str(row[6]),
        summary=str(row[7]),
        locator=_object(row[8]),
        source_digest=str(row[9]) if row[9] is not None else None,
        created_at=_timestamp(row[10]),
    )


def _link_evidence_from_row(row: Any) -> tuple[ClaimEvidenceLink, EvidenceRecord]:
    link = ClaimEvidenceLink(
        claim_id=str(row[0]),
        evidence_id=str(row[1]),
        relation=str(row[2]),
        created_at=_timestamp(row[3]),
    )
    evidence = EvidenceRecord(
        evidence_id=str(row[1]),
        run_id=str(row[4]),
        thread_id=str(row[5]),
        turn_id=str(row[6]),
        source_type=EvidenceSourceType(str(row[7])),
        source_id=str(row[8]),
        source_run_id=str(row[9]),
        summary=str(row[10]),
        locator=_object(row[11]),
        source_digest=str(row[12]) if row[12] is not None else None,
        created_at=_timestamp(row[13]),
    )
    return link, evidence
