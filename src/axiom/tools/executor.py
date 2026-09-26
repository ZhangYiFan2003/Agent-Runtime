from __future__ import annotations

import asyncio
import inspect
from typing import Any

from axiom.execution import ExecutionBackend, create_execution_backend
from axiom.policy import (
    AuditLog,
    DefaultPermissionPolicy,
    PermissionAction,
    PermissionDecision,
)
from axiom.tools.base import Tool, ToolContext, ToolDecision, ToolResult
from axiom.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        execution_backend: ExecutionBackend | None = None,
    ):
        self.registry = registry
        self.execution_backend = execution_backend

    async def execute_all(
        self,
        calls: list[dict[str, Any]],
        context: ToolContext,
    ) -> list[ToolResult]:
        self._ensure_execution_backend(context)
        read_calls: list[tuple[dict[str, Any], Tool]] = []
        sequential_calls: list[tuple[dict[str, Any], Tool | None]] = []

        for call in calls:
            name = _tool_call_name(call)
            tool = self.registry.get(name)
            if tool and tool.is_read_only and tool.is_concurrency_safe:
                read_calls.append((call, tool))
            else:
                sequential_calls.append((call, tool))

        results: list[ToolResult] = []
        if read_calls:
            semaphore = asyncio.Semaphore(context.config.tools.max_concurrent_read)

            async def run_read(call: dict[str, Any], tool: Tool) -> ToolResult:
                async with semaphore:
                    return await self._execute_single(call, tool, context)

            results.extend(
                await asyncio.gather(*(run_read(call, tool) for call, tool in read_calls))
            )

        for call, tool in sequential_calls:
            results.append(await self._execute_single(call, tool, context))

        return results

    async def execute_one(
        self,
        call: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        """Execute one call while preserving the normal validation and policy path."""
        self._ensure_execution_backend(context)
        return await self._execute_single(call, self.registry.get(_tool_call_name(call)), context)

    def _ensure_execution_backend(self, context: ToolContext) -> None:
        if context.execution_backend is None:
            context.execution_backend = self.execution_backend or create_execution_backend(
                context.config,
                context.workspace or context.cwd,
            )

    async def _execute_single(
        self,
        call: dict[str, Any],
        tool: Tool | None,
        context: ToolContext,
    ) -> ToolResult:
        tool_call_id = str(call.get("id") or "")
        name = _tool_call_name(call)
        payload = _tool_call_arguments(call)

        if not tool:
            return ToolResult(
                tool_use_id=tool_call_id,
                content=(
                    f'Tool "{name}" not found. Available tools: '
                    f"{', '.join(self.registry.list_names())}"
                ),
                is_error=True,
                metadata={
                    "error_type": "ToolNotFoundError",
                    "failure_category": "validation_error",
                },
            )

        audit = AuditLog(context.config.policy.audit_log_path)
        approver = "none"
        try:
            data = tool.validate(payload)
            permission = await self._permission_decision(
                tool,
                data,
                context,
                invocation_id=context.invocation_id or tool_call_id or f"tool:{tool.name}",
            )
            if permission.action == PermissionAction.DENY:
                audit.record(
                    tool_name=tool.name,
                    input_data=data,
                    outcome="deny",
                    approver=approver,
                    cwd=context.cwd,
                )
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=(
                        f'Tool "{tool.name}" execution denied by permission policy: '
                        f"{permission.reason}"
                    ),
                    is_error=True,
                    metadata={
                        "error_type": "PermissionDenied",
                        "failure_category": "policy_denied",
                    },
                )
            if permission.action == PermissionAction.REQUIRE_APPROVAL:
                approver = "hitl"
                approval = await self._approval_decision(tool, data, context)
                if approval in {"deny", "skip"}:
                    audit.record(
                        tool_name=tool.name,
                        input_data=data,
                        outcome=approval,
                        approver=approver,
                        cwd=context.cwd,
                    )
                    return ToolResult(
                        tool_use_id=tool_call_id,
                        content=f'Tool "{tool.name}" was rejected by approval policy.',
                        is_error=True,
                        metadata={
                            "error_type": "ApprovalRejected",
                            "failure_category": "policy_denied",
                        },
                    )

            result = await tool.execute(data, context)
            result.tool_use_id = tool_call_id
            if not tool.is_read_only and context.config.features.audit_log:
                audit.record(
                    tool_name=tool.name,
                    input_data=data,
                    outcome="allow" if not result.is_error else "error",
                    approver=approver,
                    cwd=context.cwd,
                )
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - tool errors must flow back to the model
            error_message = str(exc).strip()
            if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) and not error_message:
                error_message = "timed out"
            elif not error_message:
                error_message = type(exc).__name__
            if context.config.features.audit_log and tool and not tool.is_read_only:
                audit.record(
                    tool_name=tool.name,
                    input_data=payload,
                    outcome="error",
                    approver=approver,
                    cwd=context.cwd,
                )
            return ToolResult(
                tool_use_id=tool_call_id,
                content=f'Tool "{name}" execution error: {error_message}',
                is_error=True,
                metadata={
                    "error_type": type(exc).__name__,
                    "dependency_timeout": isinstance(exc, (asyncio.TimeoutError, TimeoutError)),
                    **_safe_exception_metadata(exc),
                },
            )

    async def _approval_decision(
        self,
        tool: Tool,
        payload: dict[str, Any],
        context: ToolContext,
    ) -> ToolDecision:
        if not context.approval_callback:
            return "deny"
        result = context.approval_callback(
            {
                "tool_name": tool.name,
                "input": payload,
                "danger_level": tool.danger_level,
                "description": tool.description,
            }
        )
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def _permission_decision(
        self,
        tool: Tool,
        payload: dict[str, Any],
        context: ToolContext,
        *,
        invocation_id: str,
    ) -> PermissionDecision:
        request = tool.permission_request(payload, context, invocation_id=invocation_id)
        if context.preauthorized_invocation_id == invocation_id:
            decision = PermissionDecision(
                PermissionAction.ALLOW,
                "invocation was authorized by the durable runtime",
                "invocation.preauthorized",
            )
        else:
            policy = context.permission_policy or DefaultPermissionPolicy.from_config(
                request.workspace, context.config
            )
            decision = await policy.evaluate(request)
        if context.permission_event_sink is not None:
            emitted = context.permission_event_sink(request, decision)
            if inspect.isawaitable(emitted):
                await emitted
        return decision


def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(function.get("name") or call.get("name") or "")


def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    arguments = function.get("arguments", call.get("arguments", {}))
    if isinstance(arguments, str):
        import json

        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": arguments}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return arguments if isinstance(arguments, dict) else {}


def _safe_exception_metadata(exc: Exception) -> dict[str, int | float | str]:
    response = getattr(exc, "response", None)
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(response, "status_code", None)
    metadata: dict[str, int | float | str] = {}
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code.startswith("SANDBOX_"):
        metadata["failure_code"] = code
    try:
        parsed_status = int(status)
    except (TypeError, ValueError):
        parsed_status = 0
    if 100 <= parsed_status <= 599:
        metadata["status_code"] = parsed_status
    headers = getattr(response, "headers", None)
    if headers is not None:
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        try:
            parsed_retry_after = float(retry_after)
        except (TypeError, ValueError):
            parsed_retry_after = -1
        if parsed_retry_after >= 0:
            metadata["retry_after_seconds"] = parsed_retry_after
    return metadata
