from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from axiom import __version__
from axiom.evaluation.dataset import load_dataset, save_dataset
from axiom.evaluation.models import (
    EVALUATION_SCHEMA_VERSION,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunResult,
    EvaluationSuiteResult,
    ScorerSpec,
    now,
)
from axiom.runtime.checkpoints import RuntimeStore
from axiom.runtime.models import RunStatus, ToolExecutionRecord
from axiom.runtime.observability import SpanStatus, SpanType, TraceBundle
from axiom.runtime.observability_store import ObservabilityService, ObservabilityStore

BADCASE_SCHEMA_VERSION = 1
_MAX_TEXT = 4_000
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "arguments",
    "payload",
    "request",
}


class FailureType(StrEnum):
    RUN_FAILED = "run_failed"
    TIMEOUT = "timeout"
    WRONG_ANSWER = "wrong_answer"
    WRONG_TOOL = "wrong_tool"
    FORBIDDEN_TOOL = "forbidden_tool"
    TOOL_FAILURE = "tool_failure"
    TOOL_ARGUMENT_FAILURE = "tool_argument_failure"
    STEP_BUDGET_EXCEEDED = "step_budget_exceeded"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    CONTEXT_BUDGET_EXCEEDED = "context_budget_exceeded"
    POLICY_DENIED = "policy_denied"
    RECOVERY_FAILURE = "recovery_failure"
    NO_PROGRESS = "no_progress"
    UNKNOWN_FAILURE = "unknown_failure"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    IGNORED = "IGNORED"
    PROMOTED = "PROMOTED"


class BadCaseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class BadCasePromotionError(BadCaseError):
    pass


@dataclass(frozen=True, slots=True)
class BadCaseRecord:
    id: str
    source_type: str
    source_dataset: str | None
    source_case_id: str | None
    run_id: str | None
    thread_id: str | None
    turn_id: str | None
    trace_id: str | None
    task: str
    expected: dict[str, Any]
    actual: str
    failure_types: tuple[str, ...]
    failure_summary: str
    run_status: str
    failed_scorers: tuple[str, ...]
    tool_names: tuple[str, ...]
    error_metadata: dict[str, Any]
    failure_evidence: tuple[str, ...]
    model_provider: str | None
    model_name: str | None
    model_configuration_hash: str
    runtime_version: str
    prompt_version: str
    tool_schema_version: str
    policy_version: str
    context_policy_version: str
    dataset_version: str
    review_status: ReviewStatus = ReviewStatus.PENDING
    review_note: str = ""
    promoted_dataset: str | None = None
    promoted_case_id: str | None = None
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    scorer_specs: tuple[dict[str, Any], ...] = ()
    source_trial_index: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "source_dataset": self.source_dataset,
            "source_case_id": self.source_case_id,
            "source_trial_index": self.source_trial_index,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "task": self.task,
            "expected": _safe_json(self.expected),
            "actual": self.actual,
            "failure_types": list(self.failure_types),
            "failure_summary": self.failure_summary,
            "run_status": self.run_status,
            "failed_scorers": list(self.failed_scorers),
            "tool_names": list(self.tool_names),
            "error_metadata": _safe_json(self.error_metadata),
            "failure_evidence": list(self.failure_evidence),
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "model_configuration_hash": self.model_configuration_hash,
            "runtime_version": self.runtime_version,
            "prompt_version": self.prompt_version,
            "tool_schema_version": self.tool_schema_version,
            "policy_version": self.policy_version,
            "context_policy_version": self.context_policy_version,
            "dataset_version": self.dataset_version,
            "review_status": self.review_status.value,
            "review_note": self.review_note,
            "promoted_dataset": self.promoted_dataset,
            "promoted_case_id": self.promoted_case_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scorer_specs": [_safe_json(item) for item in self.scorer_specs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BadCaseRecord:
        return cls(
            id=str(data["id"]),
            source_type=str(data.get("source_type") or "evaluation"),
            source_dataset=_optional_text(data.get("source_dataset")),
            source_case_id=_optional_text(data.get("source_case_id")),
            source_trial_index=max(1, int(data.get("source_trial_index") or 1)),
            run_id=_optional_text(data.get("run_id")),
            thread_id=_optional_text(data.get("thread_id")),
            turn_id=_optional_text(data.get("turn_id")),
            trace_id=_optional_text(data.get("trace_id")),
            task=_bounded(data.get("task")),
            expected=_dict(data.get("expected")),
            actual=_bounded(data.get("actual")),
            failure_types=tuple(str(item) for item in _list(data.get("failure_types"))),
            failure_summary=_bounded(data.get("failure_summary")),
            run_status=str(data.get("run_status") or "UNKNOWN"),
            failed_scorers=tuple(str(item) for item in _list(data.get("failed_scorers"))),
            tool_names=tuple(str(item) for item in _list(data.get("tool_names"))),
            error_metadata=_dict(data.get("error_metadata")),
            failure_evidence=tuple(_bounded(item) for item in _list(data.get("failure_evidence"))),
            model_provider=_optional_text(data.get("model_provider")),
            model_name=_optional_text(data.get("model_name")),
            model_configuration_hash=str(data.get("model_configuration_hash") or "unknown"),
            runtime_version=str(data.get("runtime_version") or "unknown"),
            prompt_version=str(data.get("prompt_version") or "unknown"),
            tool_schema_version=str(data.get("tool_schema_version") or "unknown"),
            policy_version=str(data.get("policy_version") or "unknown"),
            context_policy_version=str(data.get("context_policy_version") or "unknown"),
            dataset_version=str(data.get("dataset_version") or "unknown"),
            review_status=ReviewStatus(str(data.get("review_status") or "PENDING")),
            review_note=_bounded(data.get("review_note"), limit=1_000),
            promoted_dataset=_optional_text(data.get("promoted_dataset")),
            promoted_case_id=_optional_text(data.get("promoted_case_id")),
            created_at=str(data.get("created_at") or now()),
            updated_at=str(data.get("updated_at") or now()),
            scorer_specs=tuple(
                _dict(item) for item in _list(data.get("scorer_specs")) if isinstance(item, dict)
            ),
        )


class BadCaseStore:
    """Restart-safe local JSON store with deterministic, idempotent record writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def save(self, record: BadCaseRecord) -> BadCaseRecord:
        records = self._load()
        existing = records.get(record.id)
        if existing is not None:
            return existing
        records[record.id] = record
        self._write(records)
        return record

    def get(self, badcase_id: str) -> BadCaseRecord | None:
        return self._load().get(badcase_id)

    def list(self, *, status: ReviewStatus | None = None) -> list[BadCaseRecord]:
        records = self._load().values()
        if status is not None:
            records = (record for record in records if record.review_status == status)
        return sorted(records, key=lambda record: (record.created_at, record.id))

    def update_review_status(
        self,
        badcase_id: str,
        status: ReviewStatus,
        *,
        note: str = "",
    ) -> BadCaseRecord:
        record = self._require(badcase_id)
        allowed = {
            ReviewStatus.PENDING: {ReviewStatus.APPROVED, ReviewStatus.IGNORED},
            ReviewStatus.APPROVED: {ReviewStatus.IGNORED},
            ReviewStatus.IGNORED: {ReviewStatus.APPROVED},
            ReviewStatus.PROMOTED: set(),
        }
        if status == record.review_status:
            if note == record.review_note or not note:
                return record
        elif status not in allowed[record.review_status]:
            raise BadCaseError(
                "BADCASE_INVALID_REVIEW_TRANSITION",
                f"cannot transition {record.review_status.value} to {status.value}",
            )
        updated = replace(
            record,
            review_status=status,
            review_note=_bounded(note, limit=1_000),
            updated_at=now(),
        )
        self._replace(updated)
        return updated

    def mark_promoted(
        self,
        badcase_id: str,
        *,
        dataset: str,
        case_id: str,
    ) -> BadCaseRecord:
        record = self._require(badcase_id)
        if record.review_status == ReviewStatus.PROMOTED:
            if record.promoted_dataset == dataset and record.promoted_case_id == case_id:
                return record
            raise BadCaseError("BADCASE_ALREADY_PROMOTED", "badcase was promoted elsewhere")
        if record.review_status != ReviewStatus.APPROVED:
            raise BadCaseError("BADCASE_NOT_APPROVED", "only an APPROVED badcase may be promoted")
        updated = replace(
            record,
            review_status=ReviewStatus.PROMOTED,
            promoted_dataset=dataset,
            promoted_case_id=case_id,
            updated_at=now(),
        )
        self._replace(updated)
        return updated

    def _require(self, badcase_id: str) -> BadCaseRecord:
        record = self.get(badcase_id)
        if record is None:
            raise BadCaseError("BADCASE_NOT_FOUND", f"badcase not found: {badcase_id}")
        return record

    def _replace(self, record: BadCaseRecord) -> None:
        records = self._load()
        records[record.id] = record
        self._write(records)

    def _load(self) -> dict[str, BadCaseRecord]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BadCaseError("BADCASE_STORE_INVALID", f"invalid badcase store: {exc}") from exc
        if not isinstance(data, dict) or int(data.get("schema_version") or 0) != 1:
            raise BadCaseError("BADCASE_STORE_INVALID", "unsupported badcase store schema")
        raw = data.get("records") or []
        if not isinstance(raw, list):
            raise BadCaseError("BADCASE_STORE_INVALID", "badcase records must be a list")
        records = [BadCaseRecord.from_dict(item) for item in raw if isinstance(item, dict)]
        return {record.id: record for record in records}

    def _write(self, records: dict[str, BadCaseRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = {
            "schema_version": BADCASE_SCHEMA_VERSION,
            "records": [record.to_dict() for record in self._ordered(records)],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)

    @staticmethod
    def _ordered(records: dict[str, BadCaseRecord]) -> list[BadCaseRecord]:
        return sorted(records.values(), key=lambda record: (record.created_at, record.id))


class BadCaseCollector:
    def __init__(
        self,
        store: BadCaseStore,
        *,
        runtime_store: RuntimeStore | None = None,
        observability_store: ObservabilityStore | None = None,
    ) -> None:
        self.store = store
        self.runtime_store = runtime_store
        self.observability_store = observability_store

    def collect_suite(
        self,
        suite: EvaluationSuiteResult,
        *,
        include_passed: bool = False,
    ) -> list[BadCaseRecord]:
        collected = []
        for result in suite.results:
            if result.passed and not include_passed:
                continue
            collected.append(self.store.save(self._from_result(suite, result)))
        return collected

    async def collect_run(self, run_id: str) -> BadCaseRecord:
        if self.runtime_store is None or self.observability_store is None:
            raise BadCaseError(
                "BADCASE_RUNTIME_STORES_REQUIRED",
                "runtime and observability stores are required to collect a run",
            )
        state = await self.runtime_store.load(run_id)
        if state is None:
            raise BadCaseError("BADCASE_RUN_NOT_FOUND", f"run not found: {run_id}")
        if not state.finished:
            raise BadCaseError("BADCASE_RUN_NOT_TERMINAL", "only terminal runs may be collected")
        observability = ObservabilityService(self.observability_store)
        bundle = await observability.trace(run_id)
        metrics = await observability.metrics(run_id)
        tools = await self._tool_records(bundle)
        attribution = _runtime_attribution(bundle)
        result = EvaluationRunResult(
            case_id=run_id,
            run_id=run_id,
            thread_id=state.thread_id,
            turn_id=state.turn_id,
            trace_id=bundle.trace.trace_id if bundle else None,
            status=state.status.value,
            assistant_output=state.output_text,
            duration_ms=metrics.duration_ms if metrics else None,
            prompt_tokens=metrics.prompt_tokens if metrics else 0,
            completion_tokens=metrics.completion_tokens if metrics else 0,
            total_tokens=metrics.total_tokens if metrics else state.total_tokens,
            tool_calls=[record.tool_name for record in tools],
            step_count=metrics.step_count if metrics else state.step_index,
            error=state.error.message if state.error else None,
            error_metadata=state.error.to_dict() if state.error else {},
            attribution=attribution,
            passed=state.status == RunStatus.COMPLETED,
            case_definition={"id": run_id, "prompt": state.input},
        )
        return self.store.save(
            self._from_result(None, result, source_type="runtime", bundle=bundle, tools=tools)
        )

    async def _tool_records(self, bundle: TraceBundle | None) -> list[ToolExecutionRecord]:
        records = []
        if bundle is None or self.runtime_store is None:
            return records
        for span in bundle.spans:
            if span.span_type != SpanType.TOOL:
                continue
            invocation_id = span.attributes.get("invocation_id")
            if isinstance(invocation_id, str):
                record = await self.runtime_store.load_tool_execution(invocation_id)
                if record is not None:
                    records.append(record)
        return records

    def _from_result(
        self,
        suite: EvaluationSuiteResult | None,
        result: EvaluationRunResult,
        *,
        source_type: str = "evaluation",
        bundle: TraceBundle | None = None,
        tools: list[ToolExecutionRecord] | None = None,
    ) -> BadCaseRecord:
        failure_types, evidence = classify_failure(result, bundle=bundle, tools=tools or [])
        case = result.case_definition
        attribution = {**(suite.attribution if suite else {}), **result.attribution}
        failed_scores = tuple(
            score.scorer for score in result.scores if score.required and not score.passed
        )
        source_execution = (
            result.run_id
            or result.trace_id
            or result.thread_id
            or (suite.started_at if suite else "unknown")
        )
        identity = "|".join(
            [
                source_type,
                suite.dataset if suite else "",
                result.case_id,
                source_execution,
                str(result.trial_index),
            ]
        )
        identifier = f"badcase_{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        summary = ", ".join(failure_types) or FailureType.UNKNOWN_FAILURE.value
        return BadCaseRecord(
            id=identifier,
            source_type=source_type,
            source_dataset=suite.dataset if suite else None,
            source_case_id=result.case_id,
            source_trial_index=result.trial_index,
            run_id=result.run_id or None,
            thread_id=result.thread_id or None,
            turn_id=result.turn_id or None,
            trace_id=result.trace_id,
            task=_bounded(case.get("prompt")),
            expected=_dict(case.get("expected")),
            actual=_bounded(result.assistant_output),
            failure_types=tuple(failure_types),
            failure_summary=summary,
            run_status=result.status,
            failed_scorers=failed_scores,
            tool_names=tuple(dict.fromkeys(result.tool_calls)),
            error_metadata=_safe_json(result.error_metadata),
            failure_evidence=tuple(evidence),
            model_provider=_optional_text(attribution.get("model_provider")),
            model_name=_optional_text(attribution.get("model_name")),
            model_configuration_hash=str(attribution.get("model_configuration_hash") or "unknown"),
            runtime_version=str(attribution.get("runtime_version") or __version__ or "unknown"),
            prompt_version=str(attribution.get("prompt_version") or "unknown"),
            tool_schema_version=str(attribution.get("tool_schema_version") or "unknown"),
            policy_version=str(attribution.get("policy_version") or "unknown"),
            context_policy_version=str(attribution.get("context_policy_version") or "unknown"),
            dataset_version=str(
                attribution.get("dataset_version")
                or (suite.dataset_version if suite else "unknown")
            ),
            scorer_specs=tuple(
                _dict(item) for item in _list(case.get("scorers")) if isinstance(item, dict)
            ),
        )


def classify_failure(
    result: EvaluationRunResult,
    *,
    bundle: TraceBundle | None = None,
    tools: list[ToolExecutionRecord] | None = None,
) -> tuple[list[str], list[str]]:
    found: list[str] = []
    evidence: list[str] = []

    def add(kind: FailureType, reason: str) -> None:
        if kind.value not in found:
            found.append(kind.value)
        if reason not in evidence:
            evidence.append(_bounded(reason, limit=500))

    status = result.status.upper()
    if status in {"FAILED", "ERROR", "CANCELLED"}:
        add(FailureType.RUN_FAILED, f"terminal run status was {status}")
    error_text = " ".join(
        [result.error or "", *[str(value) for value in result.error_metadata.values()]]
    ).casefold()
    error_code = str(result.error_metadata.get("type") or "").upper()
    runtime_metadata = _dict(result.error_metadata.get("metadata"))
    if "timeout" in error_text or "timed out" in error_text:
        add(FailureType.TIMEOUT, "runtime error indicated a timeout")
    if "context_budget_exceeded" in error_text or "hard input" in error_text:
        add(FailureType.CONTEXT_BUDGET_EXCEEDED, "runtime exceeded the context input budget")
    if error_code == "STEP_BUDGET_EXCEEDED" or (
        "step budget" in error_text or "max steps" in error_text
    ):
        add(FailureType.STEP_BUDGET_EXCEEDED, "runtime exceeded its step budget")
    if (
        error_code
        in {
            "INPUT_TOKEN_BUDGET_EXCEEDED",
            "OUTPUT_TOKEN_BUDGET_EXCEEDED",
            "TOTAL_TOKEN_BUDGET_EXCEEDED",
        }
        or "token budget" in error_text
    ):
        add(FailureType.TOKEN_BUDGET_EXCEEDED, "runtime exceeded its token budget")
    if "policy" in error_text and ("deny" in error_text or "denied" in error_text):
        add(FailureType.POLICY_DENIED, "runtime error indicated policy denial")
    if "recover" in error_text and ("fail" in error_text or "error" in error_text):
        add(FailureType.RECOVERY_FAILURE, "runtime recovery failed")
    if error_code == "NO_PROGRESS" or "no_progress" in error_text:
        detector = str(
            runtime_metadata.get("detector_type")
            or result.error_metadata.get("detector_type")
            or "unknown"
        )
        add(FailureType.NO_PROGRESS, f"runtime detected no progress: {detector}")
    if "tool" in error_text and any(
        marker in error_text for marker in ("fail", "error", "invalid")
    ):
        add(FailureType.TOOL_FAILURE, "runtime error indicated a tool failure")
        if any(marker in error_text for marker in ("argument", "invalid input", "required input")):
            add(FailureType.TOOL_ARGUMENT_FAILURE, "runtime error indicated invalid tool input")

    for score in result.scores:
        if score.passed:
            continue
        if score.scorer in {"contains", "exact_match"}:
            add(FailureType.WRONG_ANSWER, f"{score.scorer} scorer failed: {score.reason}")
        elif score.scorer == "tool_usage":
            used = set(str(item) for item in _list(score.details.get("used")))
            forbidden = set(str(item) for item in _list(score.details.get("forbidden")))
            required = set(str(item) for item in _list(score.details.get("required")))
            if used & forbidden:
                add(FailureType.FORBIDDEN_TOOL, f"forbidden tools used: {sorted(used & forbidden)}")
            if required - used:
                add(FailureType.WRONG_TOOL, f"required tools missing: {sorted(required - used)}")
        elif score.scorer == "metric_threshold":
            if _metric_failed(score.details.get("steps")):
                add(FailureType.STEP_BUDGET_EXCEEDED, "step threshold scorer failed")
            if _metric_failed(score.details.get("tokens")):
                add(FailureType.TOKEN_BUDGET_EXCEEDED, "token threshold scorer failed")

    for record in tools or []:
        if record.is_error or record.status.value == "FAILED":
            add(FailureType.TOOL_FAILURE, f"tool {record.tool_name} failed")
            tool_error = f"{record.error or ''} {record.result or ''}".casefold()
            if any(word in tool_error for word in ("argument", "invalid", "required input")):
                add(FailureType.TOOL_ARGUMENT_FAILURE, f"tool {record.tool_name} input failed")

    if bundle is not None:
        for span in bundle.spans:
            if span.span_type == SpanType.TOOL and span.status == SpanStatus.FAILED:
                add(FailureType.TOOL_FAILURE, f"tool span {span.name} failed")
            action = str(
                span.attributes.get("decision") or span.attributes.get("permission_action") or ""
            ).upper()
            if action == "DENY":
                add(FailureType.POLICY_DENIED, f"policy span {span.name} denied execution")
            reason = str(span.attributes.get("context.trigger_reason") or "")
            if reason == "hard_input_limit":
                add(FailureType.CONTEXT_BUDGET_EXCEEDED, "context trace hit hard input limit")
            if span.attributes.get("progress.detected"):
                detector = str(span.attributes.get("progress.detector_type") or "unknown")
                add(FailureType.NO_PROGRESS, f"progress detector triggered: {detector}")

    if not found:
        add(FailureType.UNKNOWN_FAILURE, "no deterministic taxonomy rule matched")
    return found, evidence


def promote_badcase(
    store: BadCaseStore,
    badcase_id: str,
    dataset_path: str | Path,
    *,
    expected_override: dict[str, Any] | None = None,
    scorers_override: list[dict[str, Any]] | None = None,
) -> tuple[BadCaseRecord, EvaluationCase]:
    record = store.get(badcase_id)
    if record is None:
        raise BadCasePromotionError("BADCASE_NOT_FOUND", f"badcase not found: {badcase_id}")
    target = Path(dataset_path).expanduser()
    case_id = f"regression_{record.id.removeprefix('badcase_')[:16]}"
    if record.review_status == ReviewStatus.PROMOTED:
        if record.promoted_dataset == str(target) and record.promoted_case_id == case_id:
            dataset = load_dataset(target)
            existing = next(case for case in dataset.cases if case.id == case_id)
            return record, existing
        raise BadCasePromotionError("BADCASE_ALREADY_PROMOTED", "badcase was promoted elsewhere")
    if record.review_status != ReviewStatus.APPROVED:
        raise BadCasePromotionError(
            "BADCASE_NOT_APPROVED", "only an APPROVED badcase may be promoted"
        )
    scorer_data = scorers_override or list(record.scorer_specs)
    if not scorer_data and "run_status" in record.failed_scorers:
        scorer_data = [{"type": "run_status", "required": True, "accepted": ["COMPLETED"]}]
    if not scorer_data and expected_override and "answer" in expected_override:
        scorer_data = [
            {"type": "exact_match", "required": True, "expected": expected_override["answer"]}
        ]
    if not scorer_data:
        raise BadCasePromotionError(
            "BADCASE_PROMOTION_INSUFFICIENT_EXPECTATION",
            "promotion requires preserved scorer configuration or explicit "
            "expected/scorer overrides",
        )
    if not record.task.strip():
        raise BadCasePromotionError(
            "BADCASE_PROMOTION_MISSING_TASK", "promotion requires a task prompt"
        )
    scorers = tuple(ScorerSpec.from_dict(item) for item in scorer_data)
    evaluation_case = EvaluationCase(
        id=case_id,
        name=f"Regression from {record.id}",
        prompt=record.task,
        tags=("regression", "badcase"),
        metadata={
            "source_badcase_id": record.id,
            "source_run_id": record.run_id,
            "failure_types": list(record.failure_types),
        },
        expected=expected_override if expected_override is not None else record.expected,
        scorers=scorers,
    )
    if target.exists():
        dataset = load_dataset(target)
        existing = next((case for case in dataset.cases if case.id == case_id), None)
        if existing is not None:
            if existing.metadata.get("source_badcase_id") != record.id:
                raise BadCasePromotionError(
                    "BADCASE_PROMOTION_CASE_COLLISION",
                    f"dataset already contains unrelated case {case_id}",
                )
            promoted = store.mark_promoted(record.id, dataset=str(target), case_id=case_id)
            return promoted, existing
        dataset = replace(dataset, cases=(*dataset.cases, evaluation_case))
    else:
        dataset = EvaluationDataset(
            name="regression",
            version="1.0.0",
            cases=(evaluation_case,),
            metadata={"source": "reviewed badcases"},
            schema_version=EVALUATION_SCHEMA_VERSION,
        )
    save_dataset(dataset, target)
    promoted = store.mark_promoted(record.id, dataset=str(target), case_id=case_id)
    return promoted, evaluation_case


def _runtime_attribution(bundle: TraceBundle | None) -> dict[str, Any]:
    result: dict[str, Any] = {"runtime_version": __version__ or "unknown"}
    if bundle is None:
        return result
    llm = next((span for span in bundle.spans if span.span_type == SpanType.LLM), None)
    if llm is not None:
        result["model_provider"] = str(llm.attributes.get("provider") or "unknown")
        result["model_name"] = str(llm.attributes.get("model") or "unknown")
    return result


def _metric_failed(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    actual = value.get("actual")
    maximum = value.get("max")
    return (
        isinstance(actual, (int, float)) and isinstance(maximum, (int, float)) and actual > maximum
    )


def _safe_json(value: Any, *, key: str = "") -> Any:
    lowered = key.casefold()
    if any(sensitive in lowered for sensitive in _SENSITIVE_KEYS):
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded(value)
    if isinstance(value, list | tuple):
        return [_safe_json(item) for item in value[:100]]
    if isinstance(value, dict):
        return {str(name): _safe_json(item, key=str(name)) for name, item in value.items()}
    return _bounded(value)


def _bounded(value: Any, *, limit: int = _MAX_TEXT) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n...[truncated; original_chars={len(text)}]...\n{tail}"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list | tuple) else []


def _optional_text(value: Any) -> str | None:
    return str(value) if value is not None else None
