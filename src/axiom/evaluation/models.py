from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

EVALUATION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ScorerSpec:
    type: str
    required: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScorerSpec:
        scorer_type = str(data.get("type") or "").strip()
        if not scorer_type:
            raise ValueError("scorer type is required")
        return cls(
            type=scorer_type,
            required=bool(data.get("required", True)),
            config={
                str(key): _json_value(value)
                for key, value in data.items()
                if key not in {"type", "required"}
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "required": self.required, **_json_dict(self.config)}


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    prompt: str
    name: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 120.0
    setup: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    scorers: tuple[ScorerSpec, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationCase:
        case_id = str(data.get("id") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        if not case_id:
            raise ValueError("evaluation case id is required")
        if not prompt:
            raise ValueError(f'evaluation case "{case_id}" prompt is required')
        timeout = float(data.get("timeout_seconds", data.get("timeout", 120.0)))
        if timeout <= 0:
            raise ValueError(f'evaluation case "{case_id}" timeout must be positive')
        raw_scorers = data.get("scorers") or []
        if not isinstance(raw_scorers, list):
            raise ValueError(f'evaluation case "{case_id}" scorers must be a list')
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raise ValueError(f'evaluation case "{case_id}" tags must be a list')
        return cls(
            id=case_id,
            name=str(data.get("name") or case_id),
            prompt=prompt,
            tags=tuple(str(tag) for tag in raw_tags),
            metadata=_json_dict(data.get("metadata")),
            timeout_seconds=timeout,
            setup=_json_dict(data.get("setup")),
            expected=_json_dict(data.get("expected")),
            scorers=tuple(
                ScorerSpec.from_dict(item) for item in raw_scorers if isinstance(item, dict)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prompt": self.prompt,
            "tags": list(self.tags),
            "metadata": _json_dict(self.metadata),
            "timeout_seconds": self.timeout_seconds,
            "setup": _json_dict(self.setup),
            "expected": _json_dict(self.expected),
            "scorers": [scorer.to_dict() for scorer in self.scorers],
        }


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    name: str
    version: str
    cases: tuple[EvaluationCase, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = EVALUATION_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationDataset:
        schema_version = int(data.get("schema_version") or EVALUATION_SCHEMA_VERSION)
        if schema_version != EVALUATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported evaluation dataset schema version: {schema_version}")
        name = str(data.get("name") or "").strip()
        version = str(data.get("version") or "").strip()
        raw_cases = data.get("cases")
        if not name:
            raise ValueError("evaluation dataset name is required")
        if not version:
            raise ValueError("evaluation dataset version is required")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("evaluation dataset cases must be a non-empty list")
        cases = tuple(
            EvaluationCase.from_dict(item) for item in raw_cases if isinstance(item, dict)
        )
        if len(cases) != len(raw_cases):
            raise ValueError("every evaluation case must be an object")
        ids = [case.id for case in cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case ids must be unique")
        return cls(
            name=name,
            version=version,
            cases=cases,
            metadata=_json_dict(data.get("metadata")),
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "version": self.version,
            "metadata": _json_dict(self.metadata),
            "cases": [case.to_dict() for case in self.cases],
        }


@dataclass(frozen=True, slots=True)
class ScoreResult:
    scorer: str
    passed: bool
    score: float
    reason: str
    required: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer": self.scorer,
            "passed": self.passed,
            "score": self.score,
            "reason": self.reason,
            "required": self.required,
            "details": _json_dict(self.details),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScoreResult:
        return cls(
            scorer=str(data.get("scorer") or "unknown"),
            passed=bool(data.get("passed")),
            score=float(data.get("score") or 0.0),
            reason=str(data.get("reason") or ""),
            required=bool(data.get("required", True)),
            details=_json_dict(data.get("details")),
        )


@dataclass(slots=True)
class EvaluationRunResult:
    case_id: str
    run_id: str
    thread_id: str
    turn_id: str
    trace_id: str | None
    status: str
    assistant_output: str
    duration_ms: float | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tool_calls: list[str]
    step_count: int
    error: str | None = None
    scores: list[ScoreResult] = field(default_factory=list)
    passed: bool = False

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "trace_id": self.trace_id,
            "status": self.status,
            "assistant_output": self.assistant_output,
            "passed": self.passed,
            "scores": [score.to_dict() for score in self.scores],
            "metrics": {
                "duration_ms": self.duration_ms,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
                "tool_calls": list(self.tool_calls),
                "tool_call_count": self.tool_call_count,
                "step_count": self.step_count,
            },
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationRunResult:
        metrics = _json_dict(data.get("metrics"))
        raw_tools = metrics.get("tool_calls") or []
        raw_scores = data.get("scores") or []
        return cls(
            case_id=str(data.get("case_id") or ""),
            run_id=str(data.get("run_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            turn_id=str(data.get("turn_id") or ""),
            trace_id=_optional_str(data.get("trace_id")),
            status=str(data.get("status") or "ERROR"),
            assistant_output=str(data.get("assistant_output") or ""),
            duration_ms=_optional_float(metrics.get("duration_ms")),
            prompt_tokens=int(metrics.get("prompt_tokens") or 0),
            completion_tokens=int(metrics.get("completion_tokens") or 0),
            total_tokens=int(metrics.get("total_tokens") or 0),
            tool_calls=[str(tool) for tool in raw_tools] if isinstance(raw_tools, list) else [],
            step_count=int(metrics.get("step_count") or 0),
            error=_optional_str(data.get("error")),
            scores=[ScoreResult.from_dict(item) for item in raw_scores if isinstance(item, dict)],
            passed=bool(data.get("passed")),
        )


@dataclass(frozen=True, slots=True)
class EvaluationSuiteResult:
    dataset: str
    dataset_version: str
    started_at: str
    ended_at: str
    cases_total: int
    cases_passed: int
    cases_failed: int
    pass_rate: float
    avg_latency_ms: float
    avg_tokens: float
    avg_steps: float
    results: tuple[EvaluationRunResult, ...]
    schema_version: int = EVALUATION_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        dataset: EvaluationDataset,
        results: list[EvaluationRunResult],
        *,
        started_at: str,
        ended_at: str | None = None,
    ) -> EvaluationSuiteResult:
        total = len(results)
        passed = sum(result.passed for result in results)
        return cls(
            dataset=dataset.name,
            dataset_version=dataset.version,
            started_at=started_at,
            ended_at=ended_at or now(),
            cases_total=total,
            cases_passed=passed,
            cases_failed=total - passed,
            pass_rate=round(passed / total, 4) if total else 0.0,
            avg_latency_ms=_average(
                result.duration_ms for result in results if result.duration_ms is not None
            ),
            avg_tokens=_average(result.total_tokens for result in results),
            avg_steps=_average(result.step_count for result in results),
            results=tuple(results),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset": self.dataset,
            "dataset_version": self.dataset_version,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cases_total": self.cases_total,
            "cases_passed": self.cases_passed,
            "cases_failed": self.cases_failed,
            "pass_rate": self.pass_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_tokens": self.avg_tokens,
            "avg_steps": self.avg_steps,
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EvaluationSuiteResult:
        schema_version = int(data.get("schema_version") or EVALUATION_SCHEMA_VERSION)
        if schema_version != EVALUATION_SCHEMA_VERSION:
            raise ValueError(f"unsupported evaluation result schema version: {schema_version}")
        raw_results = data.get("results") or []
        results = tuple(
            EvaluationRunResult.from_dict(item) for item in raw_results if isinstance(item, dict)
        )
        return cls(
            dataset=str(data.get("dataset") or ""),
            dataset_version=str(data.get("dataset_version") or ""),
            started_at=str(data.get("started_at") or ""),
            ended_at=str(data.get("ended_at") or ""),
            cases_total=int(data.get("cases_total") or len(results)),
            cases_passed=int(data.get("cases_passed") or 0),
            cases_failed=int(data.get("cases_failed") or 0),
            pass_rate=float(data.get("pass_rate") or 0.0),
            avg_latency_ms=float(data.get("avg_latency_ms") or 0.0),
            avg_tokens=float(data.get("avg_tokens") or 0.0),
            avg_steps=float(data.get("avg_steps") or 0.0),
            results=results,
            schema_version=schema_version,
        )


def now() -> str:
    return datetime.now(UTC).isoformat()


def _average(values) -> float:
    collected = list(values)
    return round(sum(collected) / len(collected), 3) if collected else 0.0


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise ValueError(f"value is not JSON-compatible: {type(value).__name__}")


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
