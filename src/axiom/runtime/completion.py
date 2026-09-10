from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom.plan import ExecutionPlan, TaskStatus
from axiom.runtime.models import RunStatus, ToolExecutionRecord, ToolExecutionStatus

if TYPE_CHECKING:
    from axiom.runtime.checkpoints import RuntimeStore
    from axiom.runtime.models import Checkpoint


COMPLETION_NOT_VERIFIED = "COMPLETION_NOT_VERIFIED"


class CompletionVerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    NOT_VERIFIED = "NOT_VERIFIED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class CompletionCheck:
    id: str
    type: str
    required: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompletionCheck:
        check_type = str(data.get("type") or "").strip()
        if not check_type:
            raise ValueError("completion check type is required")
        check_id = str(data.get("id") or check_type).strip()
        if not check_id:
            raise ValueError("completion check id is required")
        return cls(
            id=check_id,
            type=check_type,
            required=bool(data.get("required", True)),
            config={
                str(key): _json_value(value)
                for key, value in data.items()
                if key not in {"id", "type", "required"}
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "required": self.required,
            **{key: _json_value(value) for key, value in self.config.items()},
        }


@dataclass(frozen=True, slots=True)
class CompletionContract:
    checks: tuple[CompletionCheck, ...]
    max_correction_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_correction_attempts < 0:
            raise ValueError("max_correction_attempts must be >= 0")
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("completion check ids must be unique")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompletionContract:
        raw_checks = data.get("checks") or []
        if not isinstance(raw_checks, list):
            raise ValueError("completion contract checks must be a list")
        if any(not isinstance(item, dict) for item in raw_checks):
            raise ValueError("every completion check must be an object")
        return cls(
            checks=tuple(
                CompletionCheck.from_dict(item) for item in raw_checks if isinstance(item, dict)
            ),
            max_correction_attempts=int(data.get("max_correction_attempts", 1)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [check.to_dict() for check in self.checks],
            "max_correction_attempts": self.max_correction_attempts,
        }


@dataclass(frozen=True, slots=True)
class CompletionCheckResult:
    check_id: str
    check_type: str
    passed: bool
    required: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "check_type": self.check_type,
            "passed": self.passed,
            "required": self.required,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompletionCheckResult:
        return cls(
            check_id=str(data.get("check_id") or "unknown"),
            check_type=str(data.get("check_type") or "unknown"),
            passed=bool(data.get("passed")),
            required=bool(data.get("required", True)),
            reason=str(data.get("reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class CompletionVerificationResult:
    status: CompletionVerificationStatus
    attempt: int
    checks: tuple[CompletionCheckResult, ...] = ()
    error: str | None = None

    @property
    def verified(self) -> bool | None:
        if self.status == CompletionVerificationStatus.VERIFIED:
            return True
        if self.status in {
            CompletionVerificationStatus.NOT_VERIFIED,
            CompletionVerificationStatus.ERROR,
        }:
            return False
        return None

    @property
    def failed_check_ids(self) -> tuple[str, ...]:
        return tuple(
            item.check_id for item in self.checks if item.required and not item.passed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "verified": self.verified,
            "attempt": self.attempt,
            "failed_check_ids": list(self.failed_check_ids),
            "checks": [check.to_dict() for check in self.checks],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompletionVerificationResult:
        raw_checks = data.get("checks") or []
        return cls(
            status=CompletionVerificationStatus(
                str(data.get("status") or CompletionVerificationStatus.NOT_APPLICABLE)
            ),
            attempt=max(0, int(data.get("attempt") or 0)),
            checks=tuple(
                CompletionCheckResult.from_dict(item)
                for item in raw_checks
                if isinstance(item, dict)
            ),
            error=str(data["error"]) if data.get("error") is not None else None,
        )


class CompletionVerifier:
    async def verify(
        self,
        state: Checkpoint,
        contract: CompletionContract,
        *,
        store: RuntimeStore,
        cwd: str,
        attempt: int,
        candidate_status: RunStatus = RunStatus.COMPLETED,
    ) -> CompletionVerificationResult:
        if not contract.checks:
            return CompletionVerificationResult(
                status=CompletionVerificationStatus.NOT_APPLICABLE,
                attempt=attempt,
            )
        try:
            needs_tool_evidence = any(
                check.type in {"required_tools", "forbidden_tools", "successful_tool"}
                for check in contract.checks
            )
            records = (
                await store.list_tool_executions(state.run_id) if needs_tool_evidence else []
            )
            collected: list[CompletionCheckResult] = []
            for check in contract.checks:
                collected.append(
                    await self._run_check(
                        check,
                        state=state,
                        records=records,
                        store=store,
                        cwd=cwd,
                        candidate_status=candidate_status,
                    )
                )
            results = tuple(collected)
        except Exception as exc:  # verification uncertainty must never become success
            return CompletionVerificationResult(
                status=CompletionVerificationStatus.ERROR,
                attempt=attempt,
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
        failed = [result for result in results if result.required and not result.passed]
        return CompletionVerificationResult(
            status=(
                CompletionVerificationStatus.NOT_VERIFIED
                if failed
                else CompletionVerificationStatus.VERIFIED
            ),
            attempt=attempt,
            checks=results,
        )

    async def _run_check(
        self,
        check: CompletionCheck,
        *,
        state: Checkpoint,
        records: list[ToolExecutionRecord],
        store: RuntimeStore,
        cwd: str,
        candidate_status: RunStatus,
    ) -> CompletionCheckResult:
        config = check.config
        if check.type == "run_status":
            accepted = _strings(config.get("accepted", [RunStatus.COMPLETED.value]))
            passed = candidate_status.value in accepted
            reason = (
                f"candidate status {candidate_status.value} is accepted"
                if passed
                else f"candidate status {candidate_status.value} is not accepted"
            )
        elif check.type == "required_tools":
            required = set(_strings(config.get("tools")))
            if not required:
                raise ValueError("required_tools.tools must not be empty")
            used = {record.tool_name for record in records}
            missing = sorted(required - used)
            passed = not missing
            reason = (
                "required tools were used"
                if passed
                else f"missing tools: {', '.join(missing)}"
            )
        elif check.type == "forbidden_tools":
            forbidden = set(_strings(config.get("tools")))
            if not forbidden:
                raise ValueError("forbidden_tools.tools must not be empty")
            used = {record.tool_name for record in records}
            found = sorted(forbidden & used)
            passed = not found
            reason = (
                "forbidden tools were not used"
                if passed
                else f"forbidden tools used: {', '.join(found)}"
            )
        elif check.type == "successful_tool":
            names = set(_strings(config.get("tools", config.get("tool"))))
            if not names:
                raise ValueError("successful_tool.tool must not be empty")
            expected_result = str(config.get("result_contains") or "")
            candidates = [record for record in records if record.tool_name in names]
            passed = any(
                record.status == ToolExecutionStatus.SUCCEEDED
                and not record.is_error
                and (not expected_result or expected_result in (record.result or ""))
                for record in candidates
            )
            reason = (
                "successful durable tool evidence found"
                if passed
                else "no successful durable tool execution matched"
            )
        elif check.type == "output_contains":
            expected = _strings(config.get("expected"))
            if not expected or any(not item for item in expected):
                raise ValueError("output_contains.expected must not be empty")
            case_sensitive = bool(config.get("case_sensitive", False))
            match_all = bool(config.get("match_all", True))
            candidate_output = _candidate_output(state)
            output = candidate_output if case_sensitive else candidate_output.casefold()
            targets = expected if case_sensitive else [item.casefold() for item in expected]
            matches = [target in output for target in targets]
            passed = all(matches) if match_all else any(matches)
            reason = "required output text found" if passed else "required output text missing"
        elif check.type == "output_exact":
            expected = str(config.get("expected") or "")
            actual = _candidate_output(state)
            if bool(config.get("strip", True)):
                actual, expected = actual.strip(), expected.strip()
            if not bool(config.get("case_sensitive", True)):
                actual, expected = actual.casefold(), expected.casefold()
            passed = actual == expected
            reason = "output exactly matched" if passed else "output did not exactly match"
        elif check.type == "artifacts_exist":
            paths = _strings(config.get("paths"))
            if not paths:
                raise ValueError("artifacts_exist.paths must not be empty")
            missing = [path for path in paths if not _workspace_artifact_exists(cwd, path)]
            passed = not missing
            reason = (
                "required artifacts exist"
                if passed
                else f"missing artifacts: {', '.join(missing)}"
            )
        elif check.type == "plan_tasks_completed":
            passed, reason = await _check_plan_tasks(state, store, _strings(config.get("task_ids")))
        else:
            raise ValueError(f"unknown completion check: {check.type}")
        return CompletionCheckResult(
            check_id=check.id,
            check_type=check.type,
            passed=passed,
            required=check.required,
            reason=reason,
        )


def verification_feedback(result: CompletionVerificationResult) -> str:
    rows = [
        "[completion verification feedback]",
        f"status: {result.status.value}",
        f"attempt: {result.attempt}",
        "failed_checks:",
    ]
    failures = [check for check in result.checks if check.required and not check.passed]
    rows.extend(f"- {check.check_id}: {check.reason}" for check in failures)
    if result.error:
        rows.append(f"- verifier_error: {result.error}")
    rows.extend(
        (
            "Continue the task and address these deterministic acceptance criteria.",
            "[end completion verification feedback]",
        )
    )
    return "\n".join(rows)


async def _check_plan_tasks(
    state: Checkpoint,
    store: RuntimeStore,
    required_ids: list[str],
) -> tuple[bool, str]:
    raw = state.strategy_state.get("plan")
    if not isinstance(raw, dict):
        return False, "no durable plan state exists"
    plan = ExecutionPlan.from_dict(raw)
    tasks = {task.id: task for task in plan.all_tasks()}
    selected = required_ids or list(tasks)
    missing = sorted(task_id for task_id in selected if task_id not in tasks)
    incomplete = sorted(
        task_id
        for task_id in selected
        if task_id in tasks and tasks[task_id].status != TaskStatus.COMPLETED
    )
    child_incomplete: list[str] = []
    for task_id in selected:
        task = tasks.get(task_id)
        if task is None or not task.child_run_id:
            continue
        child = await store.load(task.child_run_id)
        if child is None or child.status != RunStatus.COMPLETED:
            child_incomplete.append(task_id)
    if missing or incomplete or child_incomplete:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if incomplete:
            details.append(f"incomplete: {', '.join(incomplete)}")
        if child_incomplete:
            details.append(f"child runs incomplete: {', '.join(sorted(child_incomplete))}")
        return False, "; ".join(details)
    return True, "required plan tasks and child runs completed"


def _workspace_artifact_exists(cwd: str, value: str) -> bool:
    root = Path(cwd).resolve()
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return False
    return resolved.exists()


def _candidate_output(state: Checkpoint) -> str:
    for message in reversed(state.messages):
        if message.role == "assistant" and not message.tool_calls:
            return message.content if isinstance(message.content, str) else str(message.content)
    return state.output_text


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if value is None:
        return []
    raise ValueError("completion check value must be a string or list of strings")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    raise TypeError(f"completion contract value is not JSON-compatible: {type(value).__name__}")
