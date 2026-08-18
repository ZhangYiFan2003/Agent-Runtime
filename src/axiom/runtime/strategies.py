from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from axiom.runtime.models import Checkpoint

if TYPE_CHECKING:
    from axiom.llm.base import LlmClient
    from axiom.runtime.durable import DurableAgentRuntime


class RuntimeExecutionStrategy(Protocol):
    name: str

    async def advance(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> Checkpoint: ...

    async def on_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None: ...

    async def after_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None: ...


class ReactExecutionStrategy:
    name = "react"

    async def advance(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> Checkpoint:
        return await runtime._advance_react(state)

    async def on_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None:
        del runtime, state

    async def after_cancel(
        self,
        runtime: DurableAgentRuntime,
        state: Checkpoint,
    ) -> None:
        del runtime, state


def execution_strategy_from_name(
    name: str,
    *,
    llm_client: LlmClient,
) -> RuntimeExecutionStrategy:
    normalized = name.strip().lower().replace("-", "_")
    if normalized == "react":
        return ReactExecutionStrategy()
    if normalized in {"plan", "plan_execute"}:
        from axiom.runtime.plan_strategy import PlanExecuteStrategy

        return PlanExecuteStrategy.for_llm(llm_client)
    if normalized in {"team", "multi_agent"}:
        from axiom.runtime.multi_agent_strategy import MultiAgentExecutionStrategy

        return MultiAgentExecutionStrategy()
    raise ValueError(f"unknown execution strategy: {name}")
