from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class Capability(StrEnum):
    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    SHELL_EXECUTE = "shell.execute"
    NETWORK_READ = "network.read"
    NETWORK_WRITE = "network.write"
    EXTERNAL_SIDE_EFFECT = "external.side_effect"


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    run_id: str | None
    thread_id: str | None
    turn_id: str | None
    invocation_id: str
    tool_name: str
    capabilities: tuple[str, ...]
    arguments: dict[str, Any]
    workspace: str
    cwd: str
    resource_paths: tuple[str, ...] = ()
    legacy_requires_approval: bool = False


@dataclass(frozen=True, slots=True)
class PermissionDecision:
    action: PermissionAction
    reason: str
    matched_rule: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "matched_rule": self.matched_rule,
        }


class PermissionPolicy(Protocol):
    async def evaluate(self, request: PermissionRequest) -> PermissionDecision: ...


class DefaultPermissionPolicy:
    """Capability policy for a single workspace.

    Path checks are an authorization guard, not an OS filesystem sandbox.
    """

    def __init__(self, workspace: str | Path, *, hitl_mode: str = "auto") -> None:
        self.workspace = Path(workspace).resolve()
        self.hitl_mode = hitl_mode

    async def evaluate(self, request: PermissionRequest) -> PermissionDecision:
        capabilities = set(request.capabilities)
        known = {capability.value for capability in Capability}
        unknown = sorted(capabilities - known)
        if unknown:
            return PermissionDecision(
                PermissionAction.DENY,
                f"unknown capability: {', '.join(unknown)}",
                "capability.unknown",
            )

        filesystem = {
            Capability.FILESYSTEM_READ.value,
            Capability.FILESYSTEM_WRITE.value,
        }
        if capabilities & filesystem:
            outside = self._outside_workspace(request.resource_paths)
            if outside is not None:
                return PermissionDecision(
                    PermissionAction.DENY,
                    f"filesystem path is outside workspace: {outside}",
                    "filesystem.outside_workspace",
                )

        if self.hitl_mode == "always":
            return PermissionDecision(
                PermissionAction.REQUIRE_APPROVAL,
                "all tool calls require approval by configuration",
                "hitl.always",
            )

        risky = capabilities & {
            Capability.FILESYSTEM_WRITE.value,
            Capability.SHELL_EXECUTE.value,
            Capability.NETWORK_WRITE.value,
            Capability.EXTERNAL_SIDE_EFFECT.value,
        }
        if risky or request.legacy_requires_approval:
            if self.hitl_mode == "never":
                return PermissionDecision(
                    PermissionAction.ALLOW,
                    "approval disabled by configuration",
                    "hitl.never",
                )
            label = ", ".join(sorted(risky)) or "legacy tool metadata"
            return PermissionDecision(
                PermissionAction.REQUIRE_APPROVAL,
                f"sensitive capability requires approval: {label}",
                "capability.sensitive",
            )

        if capabilities:
            return PermissionDecision(
                PermissionAction.ALLOW,
                "read-only capability allowed",
                "capability.read_only",
            )
        return PermissionDecision(
            PermissionAction.ALLOW,
            "legacy tool has no sensitive security metadata",
            "legacy.compatible",
        )

    def _outside_workspace(self, paths: tuple[str, ...]) -> str | None:
        for value in paths:
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace / candidate
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self.workspace)
            except ValueError:
                return value
        return None
