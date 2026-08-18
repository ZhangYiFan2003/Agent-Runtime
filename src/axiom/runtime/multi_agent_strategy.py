from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from axiom.runtime.models import Checkpoint, RunStatus
from axiom.runtime.observability import SpanStatus, SpanType, now
from axiom.runtime.observability_store import RunTracer
from axiom.types import Message

if TYPE_CHECKING:
    from axiom.runtime.durable import DurableAgentRuntime

MULTI_AGENT_SCHEMA_VERSION = 1
_STATE_KEY = "multi_agent"


class MultiAgentStatus(StrEnum):
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_CHILD = "WAITING_CHILD"
    REVIEWING = "REVIEWING"
    SYNTHESIZED = "SYNTHESIZED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssignmentStatus(StrEnum):
    PENDING = "PENDING"
    ASSIGNED = "ASSIGNED"
    WAITING_CHILD = "WAITING_CHILD"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


@dataclass(slots=True)
class WorkerAssignment:
    assignment_id: str
    worker_role: str
    task: str
    dependencies: list[str] = field(default_factory=list)
    status: AssignmentStatus = AssignmentStatus.PENDING
    child_run_id: str | None = None
    result: str = ""
    error: str = ""
    attempt: int = 1
    review_output: str = ""
    review_approved: bool | None = None
    review_issues: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "worker_role": self.worker_role,
            "task": self.task,
            "dependencies": list(self.dependencies),
            "status": self.status.value,
            "child_run_id": self.child_run_id,
            "result": self.result,
            "error": self.error,
            "attempt": self.attempt,
            "review_output": self.review_output,
            "review_approved": self.review_approved,
            "review_issues": self.review_issues,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkerAssignment:
        raw_dependencies = data.get("dependencies")
        return cls(
            assignment_id=str(data["assignment_id"]),
            worker_role=str(data.get("worker_role") or "worker"),
            task=str(data.get("task") or ""),
            dependencies=[str(item) for item in raw_dependencies]
            if isinstance(raw_dependencies, list)
            else [],
            status=AssignmentStatus(str(data.get("status") or AssignmentStatus.PENDING)),
            child_run_id=_optional_str(data.get("child_run_id")),
            result=str(data.get("result") or ""),
            error=str(data.get("error") or ""),
            attempt=max(1, int(data.get("attempt") or 1)),
            review_output=str(data.get("review_output") or ""),
            review_approved=(
                bool(data["review_approved"])
                if isinstance(data.get("review_approved"), bool)
                else None
            ),
            review_issues=str(data.get("review_issues") or ""),
        )


@dataclass(slots=True)
class MultiAgentState:
    orchestration_goal: str
    status: MultiAgentStatus = MultiAgentStatus.PLANNING
    assignments: list[WorkerAssignment] = field(default_factory=list)
    current_assignment_id: str | None = None
    planner_output: str = ""
    review_rounds: int = 0
    synthesis_result: str = ""
    schema_version: int = MULTI_AGENT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "orchestration_goal": self.orchestration_goal,
            "status": self.status.value,
            "assignments": [assignment.to_dict() for assignment in self.assignments],
            "current_assignment_id": self.current_assignment_id,
            "planner_output": self.planner_output,
            "review_rounds": self.review_rounds,
            "synthesis_result": self.synthesis_result,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MultiAgentState:
        schema_version = int(data.get("schema_version") or 0)
        if schema_version != MULTI_AGENT_SCHEMA_VERSION:
            raise ValueError(f"unsupported multi-agent schema version: {schema_version}")
        raw_assignments = data.get("assignments")
        return cls(
            orchestration_goal=str(data.get("orchestration_goal") or ""),
            status=MultiAgentStatus(str(data.get("status") or MultiAgentStatus.PLANNING)),
            assignments=[
                WorkerAssignment.from_dict(item)
                for item in raw_assignments
                if isinstance(item, dict)
            ]
            if isinstance(raw_assignments, list)
            else [],
            current_assignment_id=_optional_str(data.get("current_assignment_id")),
            planner_output=str(data.get("planner_output") or ""),
            review_rounds=int(data.get("review_rounds") or 0),
            synthesis_result=str(data.get("synthesis_result") or ""),
            schema_version=schema_version,
        )

    def assignment(self, assignment_id: str | None) -> WorkerAssignment | None:
        return next(
            (item for item in self.assignments if item.assignment_id == assignment_id),
            None,
        )


@dataclass(slots=True)
class MultiAgentExecutionStrategy:
    max_retries_per_assignment: int = 2
    child_max_turns: int = 8
    name: str = "multi_agent"

    async def advance(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> Checkpoint:
        while state.status == RunStatus.RUNNING:
            state = await runtime._refresh(state)
            if state.status != RunStatus.RUNNING:
                return state
            orchestration = self.load_state(state)
            if orchestration is None:
                orchestration = MultiAgentState(orchestration_goal=state.input)
                self.store_state(state, orchestration)
                await runtime._save_checkpoint(state, operation="multi_agent.started")
                await runtime._emit("multi_agent.started", self._event(state, orchestration))
                continue

            if not orchestration.assignments:
                state = await self._create_assignments(runtime, state, orchestration)
                continue

            current = orchestration.assignment(orchestration.current_assignment_id)
            if current is not None:
                state = await self._advance_assignment(runtime, state, orchestration, current)
                continue

            executable = self._next_executable(orchestration)
            if executable is not None:
                state = await self._assign_worker(runtime, state, orchestration, executable)
                continue

            if not orchestration.synthesis_result:
                state = await self._synthesize(runtime, state, orchestration)
                continue

            return await self._complete(runtime, state, orchestration)
        return state

    async def on_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None:
        orchestration = self.load_state(state)
        if orchestration is None:
            return
        current = orchestration.assignment(orchestration.current_assignment_id)
        if current and current.child_run_id:
            child_state = await runtime.store.load(current.child_run_id)
            if child_state is not None and not child_state.finished:
                child_runtime = self._child_runtime(runtime, current)
                await child_runtime.cancel(current.child_run_id)
                current.status = AssignmentStatus.CANCELLED
        orchestration.status = MultiAgentStatus.CANCELLED
        self.store_state(state, orchestration)

    async def after_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None:
        await runtime._emit("multi_agent.cancelled", {"run_id": state.run_id})

    async def resume_waiting_child(
        self,
        runtime: DurableAgentRuntime,
        parent: Checkpoint,
        *,
        decision: str,
    ) -> Checkpoint:
        orchestration = self.load_state(parent)
        current = (
            orchestration.assignment(orchestration.current_assignment_id) if orchestration else None
        )
        if current is None or current.child_run_id is None:
            raise ValueError("parent has no active child run")
        child_runtime = self._child_runtime(runtime, current)
        return await child_runtime.resume(current.child_run_id, decision=decision)

    @staticmethod
    def load_state(state: Checkpoint) -> MultiAgentState | None:
        raw = state.strategy_state.get(_STATE_KEY)
        return MultiAgentState.from_dict(raw) if isinstance(raw, dict) else None

    @staticmethod
    def store_state(state: Checkpoint, orchestration: MultiAgentState) -> None:
        state.strategy_state[_STATE_KEY] = orchestration.to_dict()

    async def _create_assignments(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        orchestration: MultiAgentState,
    ) -> Checkpoint:
        text = await self._role_call(
            runtime,
            state,
            role="planner",
            name="multi_agent.planning",
            content=f"Create an execution plan for:\n{orchestration.orchestration_goal}",
        )
        if text is None:
            return state
        assignments = _parse_assignments(text)
        if not assignments:
            orchestration.status = MultiAgentStatus.FAILED
            self.store_state(state, orchestration)
            state = await runtime._fail(
                state,
                ValueError("multi-agent planner output could not be parsed"),
                step="multi_agent.planning",
            )
            await runtime._emit(
                "multi_agent.failed",
                {**self._event(state, orchestration), "error": "planner output invalid"},
            )
            return state
        orchestration.planner_output = text
        orchestration.assignments = assignments
        orchestration.status = MultiAgentStatus.RUNNING
        self.store_state(state, orchestration)
        await runtime._save_checkpoint(state, operation="worker.assignments.created")
        await runtime._emit(
            "worker.assignments.created",
            {**self._event(state, orchestration), "assignments": len(assignments)},
        )
        return state

    async def _assign_worker(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        orchestration: MultiAgentState,
        assignment: WorkerAssignment,
    ) -> Checkpoint:
        assignment.child_run_id = child_run_id(
            state.run_id,
            assignment.assignment_id,
            assignment.attempt,
        )
        assignment.status = AssignmentStatus.ASSIGNED
        orchestration.current_assignment_id = assignment.assignment_id
        orchestration.status = MultiAgentStatus.WAITING_CHILD
        self.store_state(state, orchestration)
        span = await self._worker_span(runtime, state, assignment, reopen=True)
        await runtime._save_checkpoint(
            state,
            operation="worker.assigned",
            parent_span_id=_span_id(span),
        )
        await runtime._emit(
            "worker.assigned",
            self._assignment_event(state, assignment),
        )
        return state

    async def _advance_assignment(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        orchestration: MultiAgentState,
        assignment: WorkerAssignment,
    ) -> Checkpoint:
        if assignment.status in {AssignmentStatus.ASSIGNED, AssignmentStatus.WAITING_CHILD}:
            return await self._advance_child(runtime, state, orchestration, assignment)
        if assignment.status == AssignmentStatus.REVIEWING:
            return await self._review(runtime, state, orchestration, assignment)
        orchestration.current_assignment_id = None
        self.store_state(state, orchestration)
        await runtime._save_checkpoint(state, operation="worker.state.normalized")
        return state

    async def _advance_child(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        orchestration: MultiAgentState,
        assignment: WorkerAssignment,
    ) -> Checkpoint:
        if assignment.child_run_id is None:
            raise RuntimeError("assigned worker is missing child_run_id")
        child_runtime = self._child_runtime(runtime, assignment)
        child = await runtime.store.load(assignment.child_run_id)
        if child is None:
            assignment.status = AssignmentStatus.WAITING_CHILD
            self.store_state(state, orchestration)
            await runtime._save_checkpoint(state, operation="worker.child.starting")
            await runtime._emit("worker.started", self._assignment_event(state, assignment))
            child = await child_runtime.start(
                thread_id=state.thread_id,
                turn_id=state.turn_id,
                run_id=assignment.child_run_id,
                input=_worker_input(orchestration, assignment),
                parent_run_id=state.run_id,
                parent_step_id=worker_span_id(
                    state.run_id,
                    assignment.assignment_id,
                    assignment.attempt,
                ),
                run_kind="worker",
            )
        elif child.status == RunStatus.RUNNING:
            child = await child_runtime.resume(child.run_id)

        if child.status in {RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED}:
            assignment.status = AssignmentStatus.WAITING_CHILD
            orchestration.status = MultiAgentStatus.WAITING_CHILD
            state.status = RunStatus.WAITING_CHILD
            self.store_state(state, orchestration)
            await runtime._save_checkpoint(state, operation="worker.waiting")
            await runtime._update_run_trace(state.status)
            await runtime._emit(
                "worker.waiting",
                {
                    **self._assignment_event(state, assignment),
                    "child_status": child.status.value,
                },
            )
            return state

        span = await self._worker_span(runtime, state, assignment, reopen=True)
        if child.status == RunStatus.COMPLETED:
            assignment.result = child.output_text
            assignment.error = ""
            assignment.status = AssignmentStatus.REVIEWING
            orchestration.status = MultiAgentStatus.REVIEWING
            state.total_tokens += child.total_tokens
            self.store_state(state, orchestration)
            await runtime._save_checkpoint(
                state,
                operation="worker.completed",
                parent_span_id=_span_id(span),
            )
            await runtime._finish_span(
                span,
                SpanStatus.SUCCEEDED,
                attributes={"child_run_id": child.run_id, "output_chars": len(child.output_text)},
            )
            await runtime._emit("worker.completed", self._assignment_event(state, assignment))
            return state

        assignment.error = child.error.message if child.error else child.status.value.lower()
        assignment.status = (
            AssignmentStatus.CANCELLED
            if child.status == RunStatus.CANCELLED
            else AssignmentStatus.FAILED
        )
        orchestration.current_assignment_id = None
        orchestration.status = MultiAgentStatus.RUNNING
        self.store_state(state, orchestration)
        await runtime._save_checkpoint(
            state,
            operation="worker.failed",
            parent_span_id=_span_id(span),
        )
        span_status = (
            SpanStatus.CANCELLED if child.status == RunStatus.CANCELLED else SpanStatus.FAILED
        )
        await runtime._finish_span(span, span_status, attributes={"error": assignment.error})
        await runtime._emit(
            "worker.cancelled" if child.status == RunStatus.CANCELLED else "worker.failed",
            self._assignment_event(state, assignment),
        )
        return state

    async def _review(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        orchestration: MultiAgentState,
        assignment: WorkerAssignment,
    ) -> Checkpoint:
        if not assignment.review_output:
            await runtime._emit("review.started", self._assignment_event(state, assignment))
            text = await self._role_call(
                runtime,
                state,
                role="reviewer",
                name="multi_agent.review",
                content=(
                    f"Original task:\n{assignment.task}\n\nExecution result:\n{assignment.result}"
                ),
                parent_span_id=worker_span_id(
                    state.run_id,
                    assignment.assignment_id,
                    assignment.attempt,
                ),
            )
            if text is None:
                return state
            assignment.review_output = text
            assignment.review_approved = _parse_review_approval(text)
            assignment.review_issues = _parse_review_issues(text)
            orchestration.review_rounds += 1
            self.store_state(state, orchestration)
            await runtime._save_checkpoint(state, operation="review.completed")
            await runtime._emit(
                "review.completed",
                {
                    **self._assignment_event(state, assignment),
                    "approved": assignment.review_approved,
                },
            )
            return state

        if assignment.review_approved or assignment.attempt > self.max_retries_per_assignment:
            assignment.status = AssignmentStatus.COMPLETED
            orchestration.current_assignment_id = None
            orchestration.status = MultiAgentStatus.RUNNING
            self.store_state(state, orchestration)
            await runtime._save_checkpoint(state, operation="review.accepted")
            return state

        assignment.attempt += 1
        assignment.status = AssignmentStatus.PENDING
        assignment.child_run_id = None
        assignment.result = ""
        assignment.error = ""
        assignment.review_output = ""
        assignment.review_approved = None
        orchestration.current_assignment_id = None
        orchestration.status = MultiAgentStatus.RUNNING
        self.store_state(state, orchestration)
        await runtime._save_checkpoint(state, operation="worker.reassigned")
        await runtime._emit("worker.assigned", self._assignment_event(state, assignment))
        return state

    async def _synthesize(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        orchestration: MultiAgentState,
    ) -> Checkpoint:
        span = await runtime._start_span(
            SpanType.AGENT,
            "multi_agent.synthesis",
            span_id=_stable_span_id(state.run_id, "synthesis"),
            reopen=True,
            attributes={"assignments": len(orchestration.assignments)},
        )
        await runtime._emit("synthesis.started", self._event(state, orchestration))
        orchestration.synthesis_result = _build_final_result(orchestration)
        orchestration.status = MultiAgentStatus.SYNTHESIZED
        self.store_state(state, orchestration)
        await runtime._save_checkpoint(
            state,
            operation="synthesis.completed",
            parent_span_id=_span_id(span),
        )
        await runtime._finish_span(span, SpanStatus.SUCCEEDED)
        await runtime._emit("synthesis.completed", self._event(state, orchestration))
        return state

    async def _complete(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        orchestration: MultiAgentState,
    ) -> Checkpoint:
        orchestration.status = MultiAgentStatus.COMPLETED
        state.status = RunStatus.COMPLETED
        state.output_text = orchestration.synthesis_result
        state.messages.append(Message(role="assistant", content=state.output_text))
        self.store_state(state, orchestration)
        await runtime._save_checkpoint(state, operation="multi_agent.completed")
        await runtime._emit("multi_agent.completed", self._event(state, orchestration))
        await runtime._finish_run_trace(state.status)
        await runtime._emit(
            "run.completed",
            {"run_id": state.run_id, "total_tokens": state.total_tokens},
        )
        return state

    async def _role_call(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        *,
        role: str,
        name: str,
        content: str,
        parent_span_id: str | None = None,
    ) -> str | None:
        step_span = await runtime._start_span(
            SpanType.AGENT,
            name,
            parent_span_id=parent_span_id,
            attributes={"role": role},
        )
        llm_span = await runtime._start_span(
            SpanType.LLM,
            f"llm.{role}",
            parent_span_id=_span_id(step_span),
            attributes={
                "provider": runtime.llm_client.provider_name,
                "model": runtime.llm_client.model_name,
                "temperature": runtime.config.llm.temperature,
                "retry_count": 0,
            },
        )
        await runtime._emit(
            "llm.started",
            {"run_id": state.run_id, "role": role, "span_id": _span_id(llm_span)},
        )
        started = time.perf_counter()
        first_token_at: str | None = None
        text = ""
        prompt_tokens = 0
        completion_tokens = 0
        finish_reason = "end_turn"
        try:
            async for event in runtime.llm_client.chat(
                [Message(role="user", content=content)],
                [],
                system_prompt=_role_system_prompt(runtime, role),
            ):
                event_type = event.get("type")
                if event_type == "text_delta":
                    if first_token_at is None:
                        first_token_at = now()
                    text += str(event.get("text") or "")
                elif event_type == "usage":
                    usage = event.get("usage") or {}
                    if isinstance(usage, dict):
                        prompt_tokens += int(usage.get("input_tokens") or 0)
                        completion_tokens += int(usage.get("output_tokens") or 0)
                elif event_type == "message_end":
                    finish_reason = str(event.get("stop_reason") or "end_turn")
                elif event_type == "error":
                    raise RuntimeError(str(event.get("error") or f"{role} failed"))
        except Exception as exc:  # noqa: BLE001 - durable strategy failure boundary
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            await runtime._finish_span(
                llm_span,
                SpanStatus.FAILED,
                attributes={"error": str(exc), "latency_ms": latency_ms},
            )
            await runtime._finish_span(step_span, SpanStatus.FAILED, attributes={"error": str(exc)})
            orchestration = self.load_state(state)
            if orchestration is not None:
                orchestration.status = MultiAgentStatus.FAILED
                self.store_state(state, orchestration)
            await runtime._fail(state, exc, step=name)
            await runtime._emit(
                "multi_agent.failed",
                {"run_id": state.run_id, "step": name, "error": str(exc)},
            )
            return None

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        state.total_tokens += prompt_tokens + completion_tokens
        state.agent_turn += 1
        state.step_index += 1
        await runtime._finish_span(
            llm_span,
            SpanStatus.SUCCEEDED,
            attributes={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "first_token_at": first_token_at,
                "latency_ms": latency_ms,
                "finish_reason": finish_reason,
            },
        )
        await runtime._finish_span(step_span, SpanStatus.SUCCEEDED)
        await runtime._emit(
            "llm.completed",
            {
                "run_id": state.run_id,
                "role": role,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "latency_ms": latency_ms,
                "finish_reason": finish_reason,
            },
        )
        return text.strip()

    def _child_runtime(
        self,
        runtime: DurableAgentRuntime,
        assignment: WorkerAssignment,
    ) -> DurableAgentRuntime:
        from axiom.runtime.durable import DurableAgentRuntime

        return DurableAgentRuntime(
            llm_client=runtime.llm_client,
            tool_registry=runtime.tool_registry,
            system_prompt=_worker_system_prompt(runtime, assignment),
            cwd=runtime.cwd,
            config=runtime.config,
            store=runtime.store,
            retry_policy=runtime.retry_policy,
            event_sink=runtime.event_sink,
            tracer=RunTracer(runtime.tracer.store) if runtime.tracer is not None else None,
            permission_policy=runtime.permission_policy,
            execution_backend=runtime.execution_backend,
            execution_strategy="react",
            max_turns=self.child_max_turns,
        )

    async def _worker_span(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
        assignment: WorkerAssignment,
        *,
        reopen: bool,
    ) -> Any:
        return await runtime._start_span(
            SpanType.AGENT,
            "multi_agent.worker",
            span_id=worker_span_id(state.run_id, assignment.assignment_id, assignment.attempt),
            reopen=reopen,
            attributes={
                "assignment_id": assignment.assignment_id,
                "worker_role": assignment.worker_role,
                "attempt": assignment.attempt,
                "child_run_id": assignment.child_run_id,
            },
        )

    def _next_executable(self, orchestration: MultiAgentState) -> WorkerAssignment | None:
        status = {item.assignment_id: item.status for item in orchestration.assignments}
        return next(
            (
                item
                for item in orchestration.assignments
                if item.status == AssignmentStatus.PENDING
                and all(status.get(dep) == AssignmentStatus.COMPLETED for dep in item.dependencies)
            ),
            None,
        )

    def _event(self, state: Checkpoint, orchestration: MultiAgentState) -> dict[str, Any]:
        return {
            "run_id": state.run_id,
            "thread_id": state.thread_id,
            "turn_id": state.turn_id,
            "status": orchestration.status.value,
        }

    def _assignment_event(
        self,
        state: Checkpoint,
        assignment: WorkerAssignment,
    ) -> dict[str, Any]:
        return {
            "run_id": state.run_id,
            "assignment_id": assignment.assignment_id,
            "child_run_id": assignment.child_run_id,
            "worker_role": assignment.worker_role,
            "attempt": assignment.attempt,
            "status": assignment.status.value,
            "error": assignment.error or None,
        }


def child_run_id(parent_run_id: str, assignment_id: str, attempt: int) -> str:
    raw = f"{parent_run_id}:{assignment_id}:{attempt}".encode()
    return f"run_child_{hashlib.sha256(raw).hexdigest()[:32]}"


def worker_span_id(parent_run_id: str, assignment_id: str, attempt: int) -> str:
    return _stable_span_id(parent_run_id, f"worker:{assignment_id}:{attempt}")


def _stable_span_id(run_id: str, scope: str) -> str:
    digest = hashlib.sha256(f"{run_id}:{scope}".encode()).hexdigest()[:32]
    return f"span_multi_{digest}"


def _parse_assignments(text: str) -> list[WorkerAssignment]:
    cleaned = re.sub(r"```(?:json)?\s*", "", text or "").replace("```", "").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    nodes = data.get("steps") or data.get("tasks") or []
    if not isinstance(nodes, list):
        return []
    id_mapping: dict[str, str] = {}
    assignments: list[WorkerAssignment] = []
    valid_nodes = [node for node in nodes if isinstance(node, dict)]
    for index, node in enumerate(valid_nodes, start=1):
        original_id = str(node.get("id") or f"assignment_{index}")
        assignment_id = f"assignment_{index}"
        id_mapping[original_id] = assignment_id
        assignments.append(
            WorkerAssignment(
                assignment_id=assignment_id,
                worker_role=str(node.get("worker_role") or node.get("type") or "worker"),
                task=str(node.get("description") or node.get("task") or original_id),
            )
        )
    for assignment, node in zip(assignments, valid_nodes, strict=True):
        raw_dependencies = node.get("dependencies") or []
        if isinstance(raw_dependencies, list):
            assignment.dependencies = [
                id_mapping.get(str(dep), str(dep)) for dep in raw_dependencies if str(dep)
            ]
    return assignments


def _parse_review_approval(text: str) -> bool:
    try:
        data = json.loads(re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip())
    except json.JSONDecodeError:
        lower = text.lower()
        if any(item in lower for item in ("未通过", "不通过", '"approved": false')):
            return False
        return any(item in lower for item in ("通过", "合格", '"approved": true'))
    return bool(data.get("approved")) if isinstance(data, dict) else False


def _parse_review_issues(text: str) -> str:
    try:
        data = json.loads(re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip())
    except json.JSONDecodeError:
        return "review rejected the result"
    if not isinstance(data, dict):
        return "review rejected the result"
    issues = data.get("issues")
    if isinstance(issues, list) and issues:
        return "\n".join(f"- {item}" for item in issues)
    return str(data.get("summary") or "review rejected the result")


def _worker_input(orchestration: MultiAgentState, assignment: WorkerAssignment) -> str:
    lines = [f"Overall goal: {orchestration.orchestration_goal}"]
    for dependency in assignment.dependencies:
        prior = orchestration.assignment(dependency)
        if prior is not None and prior.result:
            lines.append(f"Dependency [{dependency}] result: {_preview(prior.result, 800)}")
    if assignment.review_issues:
        lines.append(f"Reviewer feedback: {assignment.review_issues}")
    lines.append(f"Current task:\n{assignment.task}")
    return "\n\n".join(lines)


def _role_system_prompt(runtime: DurableAgentRuntime, role: str) -> str:
    instructions = {
        "planner": (
            "You are the Planner in a multi-agent workflow. Return only JSON with a "
            "steps array. Each step needs id, description, type, and dependencies."
        ),
        "reviewer": (
            'You are the Reviewer. Return JSON only: {"approved": true|false, '
            '"summary": "...", "issues": []}.'
        ),
    }[role]
    return f"{runtime.system_prompt}\n\n{instructions}"


def _worker_system_prompt(
    runtime: DurableAgentRuntime,
    assignment: WorkerAssignment,
) -> str:
    return (
        f"{runtime.system_prompt}\n\n"
        "You are a tool-capable Worker running as a durable child Run. "
        "Execute only the assigned task, use tools when needed, and return a concrete result.\n"
        f"Worker role: {assignment.worker_role}\nAssignment: {assignment.assignment_id}\n"
        f"Attempt: {assignment.attempt}"
    )


def _build_final_result(orchestration: MultiAgentState) -> str:
    all_completed = all(
        assignment.status == AssignmentStatus.COMPLETED for assignment in orchestration.assignments
    )
    header = (
        "Multi-Agent task completed."
        if all_completed
        else "Multi-Agent task did not fully complete; failed or blocked assignments remain."
    )
    lines = [header, "", "Execution summary:"]
    for assignment in orchestration.assignments:
        lines.append(f"- [{assignment.assignment_id}] {assignment.status.value}: {assignment.task}")
        if assignment.result:
            lines.append(f"  Result: {_preview(assignment.result)}")
        elif assignment.error:
            lines.append(f"  Error: {_preview(assignment.error)}")
    return "\n".join(lines) + "\n"


def _preview(text: str, max_len: int = 160) -> str:
    value = (text or "").replace("\r\n", "\n").strip()
    return value if len(value) <= max_len else value[: max_len - 3] + "..."


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _span_id(span: Any) -> str | None:
    return str(span.span_id) if span is not None else None
