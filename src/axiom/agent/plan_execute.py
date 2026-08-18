from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from axiom.config import AxiomConfig
from axiom.execution import ExecutionBackend
from axiom.llm.base import LlmClient
from axiom.plan import Planner
from axiom.snapshot import SnapshotService
from axiom.tools.registry import ToolRegistry
from axiom.types import Message

if TYPE_CHECKING:
    from axiom.runtime.checkpoints import RuntimeStore
    from axiom.runtime.durable import EventSink
    from axiom.runtime.models import Checkpoint
    from axiom.runtime.observability_store import ObservabilityStore


class PlanExecuteAgent:
    """Compatibility facade over the durable Plan execution strategy.

    Process-restart recovery callers inject SQLite stores. CLI callers use in-memory
    stores while still sharing the same Runtime lifecycle and enforcement path.
    """

    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: AxiomConfig,
        cwd: str,
        approval_callback=None,
        planner: Planner | None = None,
        max_task_turns: int = 8,
        checkpoint_store: RuntimeStore | None = None,
        observability_store: ObservabilityStore | None = None,
        execution_backend: ExecutionBackend | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        from axiom.runtime.checkpoints import MemoryCheckpointStore
        from axiom.runtime.observability_store import MemoryObservabilityStore

        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        self.planner = planner or Planner(llm_client)
        self.max_task_turns = max_task_turns
        self.checkpoint_store = checkpoint_store or MemoryCheckpointStore()
        self.observability_store = observability_store or MemoryObservabilityStore()
        self.execution_backend = execution_backend
        self.event_sink = event_sink
        self.history: list[Message] = []
        self.last_checkpoint: Checkpoint | None = None

    async def run(self, message: str) -> AsyncIterator[dict[str, Any]]:
        from axiom.runtime.durable import DurableAgentRuntime
        from axiom.runtime.models import RunStatus
        from axiom.runtime.observability_store import RunTracer
        from axiom.runtime.plan_strategy import PlanExecuteStrategy

        snapshot = SnapshotService(self.cwd)
        with suppress(Exception):
            snapshot.create("pre-turn")
        events: list[tuple[str, dict[str, Any]]] = []

        async def capture(event_type: str, payload: dict[str, Any]) -> None:
            events.append((event_type, payload))
            if self.event_sink is not None:
                result = self.event_sink(event_type, payload)
                if inspect.isawaitable(result):
                    await result

        strategy = PlanExecuteStrategy(
            planner=self.planner,
            max_task_turns=self.max_task_turns,
        )
        runtime = DurableAgentRuntime(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=self._system_prompt(),
            cwd=self.cwd,
            config=self.config,
            store=self.checkpoint_store,
            event_sink=capture,
            tracer=RunTracer(self.observability_store),
            execution_backend=self.execution_backend,
            execution_strategy=strategy,
        )
        run_id = f"run_{uuid4().hex}"
        try:
            state = await runtime.start(
                thread_id=f"thread_{uuid4().hex}",
                turn_id=f"turn_{uuid4().hex}",
                run_id=run_id,
                input=message,
                history=self.history,
            )
            while state.status == RunStatus.WAITING_APPROVAL and self.approval_callback:
                decision = await self._approval_decision(state)
                state = await runtime.resume(
                    state.run_id,
                    decision="approve" if decision == "approve" else "reject",
                )
        except Exception as exc:  # noqa: BLE001 - streaming facade boundary
            yield {"type": "error", "error": exc}
            return
        finally:
            with suppress(Exception):
                snapshot.create("post-turn")

        self.last_checkpoint = state
        for event_type, payload in events:
            rendered = _render_runtime_event(event_type, payload)
            if rendered:
                yield {"type": "text_delta", "text": rendered}

        if state.status in {RunStatus.WAITING_APPROVAL, RunStatus.INTERRUPTED}:
            yield {
                "type": "interrupt",
                "run_id": state.run_id,
                "interrupt": state.interrupt.to_dict() if state.interrupt else None,
            }
        elif state.status == RunStatus.FAILED:
            yield {
                "type": "error",
                "error": RuntimeError(state.error.message if state.error else "plan failed"),
            }
            return
        elif state.output_text:
            yield {"type": "text_delta", "text": state.output_text}

        if state.status == RunStatus.COMPLETED:
            self.history = [
                Message(role="user", content=message),
                Message(role="assistant", content=state.output_text),
            ]
        yield {
            "type": "done",
            "run_id": state.run_id,
            "status": state.status.value,
            "total_turns": state.agent_turn,
            "total_tokens": state.total_tokens,
            "messages": self.history,
        }

    async def _approval_decision(self, state: Checkpoint) -> str:
        interrupt = state.interrupt
        tool = self.tool_registry.get(interrupt.tool_name or "") if interrupt else None
        request = {
            "tool_name": interrupt.tool_name if interrupt else "unknown",
            "input": interrupt.arguments if interrupt else {},
            "danger_level": tool.danger_level if tool else "high",
            "description": tool.description if tool else "",
        }
        result = self.approval_callback(request)
        if inspect.isawaitable(result):
            result = await result
        return "approve" if str(result).lower() == "approve" else "reject"

    def _system_prompt(self) -> str:
        from axiom.prompt import PromptAssembler

        return PromptAssembler(
            config=self.config,
            cwd=self.cwd,
            tool_names=self.tool_registry.list_names(),
            model=self.llm_client.model_name,
            provider=self.llm_client.provider_name,
        ).build()


def _render_runtime_event(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "plan.created":
        return (
            f"Planning complete: {payload.get('steps', 0)} steps "
            f"(v{payload.get('plan_version', 1)}).\n\n"
        )
    if event_type == "plan.step.completed":
        return f"Completed [{payload.get('step_id')}].\n\n"
    if event_type == "plan.step.failed":
        return f"Failed [{payload.get('step_id')}]: {payload.get('error')}\n\n"
    if event_type == "plan.replanned":
        return f"Replanned as version {payload.get('plan_version')}.\n\n"
    return ""
