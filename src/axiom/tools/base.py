from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from axiom.config import AxiomConfig
from axiom.policy.permissions import PermissionRequest

if TYPE_CHECKING:
    from axiom.policy.permissions import PermissionDecision, PermissionPolicy

DangerLevel = Literal["safe", "medium", "high"]
ToolDecision = Literal["approve", "deny", "skip"]


@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    display_summary: str | None = None
    tool_use_id: str | None = None


@dataclass(slots=True)
class ToolContext:
    cwd: str
    config: AxiomConfig
    approval_callback: Callable[[dict[str, Any]], Awaitable[ToolDecision] | ToolDecision] | None = (
        None
    )
    skill_context_buffer: Any | None = None
    invocation_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    workspace: str | None = None
    permission_policy: PermissionPolicy | None = None
    preauthorized_invocation_id: str | None = None
    permission_event_sink: (
        Callable[[PermissionRequest, PermissionDecision], Awaitable[None] | None] | None
    ) = None


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]
    is_read_only: bool = True
    is_concurrency_safe: bool = True
    danger_level: DangerLevel = "safe"
    requires_approval: bool = False
    timeout: float = 60.0
    required_keys: list[str] = field(default_factory=list)
    idempotency_key_parameter: str | None = None
    capabilities: tuple[str, ...] = ()
    path_argument_names: tuple[str, ...] = ()

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(f'tool "{self.name}" input must be an object')
        for key in self.required_keys:
            if key not in payload:
                raise ValueError(f'tool "{self.name}" missing required input: {key}')
        return payload

    async def execute(self, payload: dict[str, Any], context: ToolContext) -> ToolResult:
        data = self.validate(payload)
        return await asyncio.wait_for(self.handler(data, context), timeout=self.timeout)

    def permission_request(
        self,
        payload: dict[str, Any],
        context: ToolContext,
        *,
        invocation_id: str,
    ) -> PermissionRequest:
        paths = tuple(
            str(payload[name])
            for name in self.path_argument_names
            if name in payload and payload[name] is not None
        )
        return PermissionRequest(
            run_id=context.run_id,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            invocation_id=invocation_id,
            tool_name=self.name,
            capabilities=self.capabilities,
            arguments=payload,
            workspace=context.workspace or context.cwd,
            cwd=context.cwd,
            resource_paths=paths,
            legacy_requires_approval=self.requires_approval,
        )


def object_schema(
    properties: dict[str, dict[str, Any]],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }
