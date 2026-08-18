from __future__ import annotations

import inspect
import json
import re
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any
from uuid import uuid4

from axiom.agent.query import query
from axiom.config import AxiomConfig
from axiom.execution import ExecutionBackend
from axiom.llm.base import LlmClient
from axiom.prompt import PromptAssembler
from axiom.skill import SkillContextBuffer
from axiom.snapshot import SnapshotService
from axiom.tools.registry import ToolRegistry
from axiom.types import Message


class AgentRole(StrEnum):
    PLANNER = "PLANNER"
    WORKER = "WORKER"
    REVIEWER = "REVIEWER"


class AgentMessageType(StrEnum):
    TASK = "TASK"
    RESULT = "RESULT"
    FEEDBACK = "FEEDBACK"
    APPROVAL = "APPROVAL"
    REJECTION = "REJECTION"
    ERROR = "ERROR"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(slots=True)
class AgentMessage:
    from_agent: str
    from_role: AgentRole | None
    content: str
    type: AgentMessageType

    @classmethod
    def task(cls, from_agent: str, content: str) -> AgentMessage:
        return cls(from_agent, None, content, AgentMessageType.TASK)

    @classmethod
    def result(cls, from_agent: str, role: AgentRole, content: str) -> AgentMessage:
        return cls(from_agent, role, content, AgentMessageType.RESULT)

    @classmethod
    def error(cls, from_agent: str, role: AgentRole, content: str) -> AgentMessage:
        return cls(from_agent, role, content, AgentMessageType.ERROR)


@dataclass(slots=True)
class ExecutionStep:
    id: str
    description: str
    type: str
    dependencies: list[str]
    result: str = ""
    status: StepStatus = StepStatus.PENDING

    def with_result(self, result: str) -> ExecutionStep:
        return replace(self, result=result, status=StepStatus.COMPLETED)

    def with_failed(self, result: str) -> ExecutionStep:
        return replace(self, result=result, status=StepStatus.FAILED)

    def started(self) -> ExecutionStep:
        return replace(self, status=StepStatus.RUNNING)


class SubAgent:
    def __init__(
        self,
        *,
        name: str,
        role: AgentRole,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: AxiomConfig,
        cwd: str,
        approval_callback=None,
        skill_context_buffer: SkillContextBuffer | None = None,
    ):
        self.name = name
        self.role = role
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        self.skill_context_buffer = skill_context_buffer or SkillContextBuffer()
        self.history: list[Message] = []

    async def execute(self, task: AgentMessage, context: str = "") -> AgentMessage:
        content = f"{context}\n\nCurrent task:\n{task.content}".strip() if context else task.content
        if self.role == AgentRole.WORKER:
            return await self._execute_worker(content)
        return await self._execute_without_tools(content)

    async def review(self, original_task: str, execution_result: str) -> AgentMessage:
        return await self.execute(
            AgentMessage.task(
                "orchestrator",
                f"Original task:\n{original_task}\n\nExecution result:\n{execution_result}",
            )
        )

    def clear_history(self) -> None:
        self.history = []

    async def _execute_worker(self, content: str) -> AgentMessage:
        text = ""
        tool_results: list[str] = []
        try:
            async for event in query(
                llm_client=self.llm_client,
                tool_registry=self.tool_registry,
                system_prompt=self._system_prompt(),
                user_message=content,
                history=self.history,
                cwd=self.cwd,
                config=self.config,
                approval_callback=self.approval_callback,
                skill_context_buffer=self.skill_context_buffer,
                max_turns=8,
            ):
                if event.get("type") == "text_delta":
                    text += str(event.get("text") or "")
                elif event.get("type") == "tool_result":
                    tool_results.append(str(event.get("result") or ""))
                elif event.get("type") == "done":
                    self.history = list(event.get("messages") or [])
                elif event.get("type") == "error":
                    raise event["error"]
        except Exception as exc:  # noqa: BLE001
            return AgentMessage.error(self.name, self.role, str(exc))
        result = text.strip() or "\n".join(item for item in tool_results if item).strip()
        return AgentMessage.result(self.name, self.role, result)

    async def _execute_without_tools(self, content: str) -> AgentMessage:
        text = ""
        messages = [*self.history, Message(role="user", content=content)]
        try:
            async for event in self.llm_client.chat(
                messages,
                [],
                system_prompt=self._system_prompt(),
            ):
                if event.get("type") == "text_delta":
                    text += str(event.get("text") or "")
                elif event.get("type") == "error":
                    raise event["error"]
        except Exception as exc:  # noqa: BLE001
            return AgentMessage.error(self.name, self.role, str(exc))
        self.history = [*messages, Message(role="assistant", content=text)]
        return AgentMessage.result(self.name, self.role, text)

    def _system_prompt(self) -> str:
        base = PromptAssembler(
            config=self.config,
            cwd=self.cwd,
            tool_names=self.tool_registry.list_names(),
            model=self.llm_client.model_name,
            provider=self.llm_client.provider_name,
        ).build()
        role_prompt = {
            AgentRole.PLANNER: (
                "You are the Planner in a multi-agent workflow. Return only JSON with a "
                "steps array. Each step needs id, description, type, and dependencies."
            ),
            AgentRole.WORKER: (
                "You are the Worker in a multi-agent workflow. Execute only the assigned "
                "step. Use tools when needed and return the concrete result."
            ),
            AgentRole.REVIEWER: (
                "You are the Reviewer in a multi-agent workflow. Return JSON only: "
                '{"approved": true|false, "summary": "...", "issues": []}.'
            ),
        }[self.role]
        return f"{base}\n\n{role_prompt}\nAgent name: {self.name}"


class AgentOrchestrator:
    max_retries_per_step = 2

    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: AxiomConfig,
        cwd: str,
        approval_callback=None,
        worker_count: int = 2,
        checkpoint_store=None,
        observability_store=None,
        execution_backend: ExecutionBackend | None = None,
        event_sink=None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        from axiom.runtime.checkpoints import MemoryCheckpointStore
        from axiom.runtime.observability_store import MemoryObservabilityStore

        self.checkpoint_store = checkpoint_store or MemoryCheckpointStore()
        self.observability_store = observability_store or MemoryObservabilityStore()
        self.execution_backend = execution_backend
        self.event_sink = event_sink
        self.last_checkpoint = None
        self.worker_count = max(1, worker_count)
        self.history: list[Message] = []

    async def run(self, message: str) -> AsyncIterator[dict[str, Any]]:
        from axiom.prompt import PromptAssembler
        from axiom.runtime.durable import DurableAgentRuntime
        from axiom.runtime.models import RunStatus
        from axiom.runtime.multi_agent_strategy import MultiAgentExecutionStrategy
        from axiom.runtime.observability_store import RunTracer

        snapshot = SnapshotService(self.cwd)
        with suppress(Exception):
            snapshot.create("pre-turn")
        captured: list[tuple[str, dict[str, Any]]] = []

        async def capture(event_type: str, payload: dict[str, Any]) -> None:
            captured.append((event_type, payload))
            if self.event_sink is not None:
                result = self.event_sink(event_type, payload)
                if inspect.isawaitable(result):
                    await result

        strategy = MultiAgentExecutionStrategy(
            max_retries_per_assignment=self.max_retries_per_step,
        )
        system_prompt = PromptAssembler(
            config=self.config,
            cwd=self.cwd,
            tool_names=self.tool_registry.list_names(),
            model=self.llm_client.model_name,
            provider=self.llm_client.provider_name,
        ).build()
        runtime = DurableAgentRuntime(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=system_prompt,
            cwd=self.cwd,
            config=self.config,
            store=self.checkpoint_store,
            event_sink=capture,
            tracer=RunTracer(self.observability_store),
            execution_backend=self.execution_backend,
            execution_strategy=strategy,
        )
        try:
            state = await runtime.start(
                thread_id=f"thread_{uuid4().hex}",
                turn_id=f"turn_{uuid4().hex}",
                run_id=f"run_{uuid4().hex}",
                input=message,
            )
            while state.status == RunStatus.WAITING_CHILD and self.approval_callback:
                child = await self._approve_waiting_child(strategy, runtime, state)
                if child is None:
                    break
                state = await runtime.resume(state.run_id)
            self.last_checkpoint = state
            if state.status == RunStatus.FAILED:
                raise RuntimeError(state.error.message if state.error else "multi-agent failed")
            if state.output_text:
                yield {"type": "text_delta", "text": state.output_text}
            if state.status == RunStatus.WAITING_CHILD:
                orchestration = strategy.load_state(state)
                assignment = (
                    orchestration.assignment(orchestration.current_assignment_id)
                    if orchestration
                    else None
                )
                yield {
                    "type": "interrupt",
                    "run_id": state.run_id,
                    "child_run_id": assignment.child_run_id if assignment else None,
                    "status": state.status.value,
                }
            self.history = [
                Message(role="user", content=message),
                Message(role="assistant", content=state.output_text),
            ]
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "error": exc}
            return
        finally:
            with suppress(Exception):
                snapshot.create("post-turn")
        yield {
            "type": "done",
            "run_id": state.run_id,
            "status": state.status.value,
            "total_turns": state.agent_turn,
            "total_tokens": state.total_tokens,
            "messages": self.history,
            "events": len(captured),
        }

    async def _approve_waiting_child(self, strategy, runtime, parent):
        from axiom.runtime.models import RunStatus

        orchestration = strategy.load_state(parent)
        assignment = (
            orchestration.assignment(orchestration.current_assignment_id) if orchestration else None
        )
        if assignment is None or assignment.child_run_id is None:
            return None
        child = await self.checkpoint_store.load(assignment.child_run_id)
        if child is None or child.status != RunStatus.WAITING_APPROVAL or child.interrupt is None:
            return None
        tool = self.tool_registry.get(child.interrupt.tool_name or "")
        request = {
            "tool_name": child.interrupt.tool_name or "unknown",
            "input": child.interrupt.arguments,
            "danger_level": tool.danger_level if tool else "high",
            "description": tool.description if tool else "",
        }
        decision = self.approval_callback(request)
        if inspect.isawaitable(decision):
            decision = await decision
        return await strategy.resume_waiting_child(
            runtime,
            parent,
            decision="approve" if str(decision).lower() == "approve" else "reject",
        )

    def parse_plan(self, plan_json: str) -> list[ExecutionStep]:
        try:
            data = _parse_json_object(plan_json)
        except (json.JSONDecodeError, ValueError):
            return []
        nodes = data.get("steps") or data.get("tasks") or []
        if not isinstance(nodes, list) or not nodes:
            return []
        id_mapping: dict[str, str] = {}
        steps: list[ExecutionStep] = []
        for index, node in enumerate(nodes, start=1):
            if not isinstance(node, dict):
                continue
            original_id = str(node.get("id") or f"step_{index}")
            new_id = f"step_{index}"
            id_mapping[original_id] = new_id
            steps.append(
                ExecutionStep(
                    id=new_id,
                    description=str(node.get("description") or original_id),
                    type=str(node.get("type") or "COMMAND"),
                    dependencies=[],
                )
            )
        for index, node in enumerate(nodes, start=1):
            if not isinstance(node, dict) or index > len(steps):
                continue
            raw_deps = node.get("dependencies") or []
            if not isinstance(raw_deps, list):
                continue
            steps[index - 1].dependencies = [
                id_mapping.get(str(dep), str(dep)) for dep in raw_deps if str(dep)
            ]
        return steps

    def get_executable_steps(self, steps: list[ExecutionStep]) -> list[ExecutionStep]:
        status = {step.id: step.status for step in steps}
        return [
            step
            for step in steps
            if step.status == StepStatus.PENDING
            and all(status.get(dep) == StepStatus.COMPLETED for dep in step.dependencies)
        ]

    def parse_review_approval(self, review_content: str | None) -> bool:
        if not review_content:
            return False
        try:
            data = _parse_json_object(review_content)
            if "approved" not in data:
                return False
            return bool(data.get("approved"))
        except (json.JSONDecodeError, ValueError):
            lower = review_content.lower()
            negative = ["未通过", "不通过", "不合格", "有问题", '"approved": false']
            positive = ["通过", "合格", '"approved": true']
            if any(item in lower for item in negative):
                return False
            return any(item in lower for item in positive)

    def parse_review_issues(self, review_content: str | None) -> str:
        if not review_content:
            return ""
        try:
            data = _parse_json_object(review_content)
        except (json.JSONDecodeError, ValueError):
            return "review rejected the result"
        for key in ("issues", "suggestions"):
            value = data.get(key)
            if isinstance(value, list) and value:
                return "\n".join(f"- {item}" for item in value)
        return str(data.get("summary") or "review rejected the result")

    def build_step_context(self, steps: list[ExecutionStep], current_step: ExecutionStep) -> str:
        lines = ["Overall task context:"]
        for step in steps:
            if step.id in current_step.dependencies and step.status == StepStatus.COMPLETED:
                lines.append(f"[{step.id}] {step.description}")
                if step.result:
                    lines.append(f"Result: {_preview(step.result, 500)}")
        return "\n".join(lines)

    def summarize_steps(self, steps: list[ExecutionStep]) -> str:
        lines = ["Execution plan:"]
        for step in steps:
            deps = ", ".join(step.dependencies) if step.dependencies else "none"
            lines.append(f"- [{step.id}] {step.description} ({step.type}, deps: {deps})")
        return "\n".join(lines)

    def build_final_result(self, steps: list[ExecutionStep]) -> str:
        all_completed = all(step.status == StepStatus.COMPLETED for step in steps)
        failed = any(step.status == StepStatus.FAILED for step in steps)
        if all_completed:
            header = "Multi-Agent task completed."
        elif failed:
            header = "Multi-Agent task did not fully complete; failed steps remain."
        else:
            header = "Multi-Agent task partially completed; pending steps remain."
        lines = [header, "", "Execution summary:"]
        for step in steps:
            icon = {
                StepStatus.COMPLETED: "COMPLETED",
                StepStatus.FAILED: "FAILED",
                StepStatus.PENDING: "PENDING",
                StepStatus.RUNNING: "RUNNING",
            }[step.status]
            lines.append(f"- [{step.id}] {icon}: {step.description}")
            if step.result:
                lines.append(f"  Result: {_preview(step.result)}")
        return "\n".join(lines) + "\n"


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"```(?:json)?\s*", "", text or "").replace("```", "").strip()
    if not cleaned:
        raise ValueError("empty JSON")
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object")
    return data


def _preview(text: str, max_len: int = 160) -> str:
    value = (text or "").replace("\r\n", "\n").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."
