from __future__ import annotations

from axiom.artifacts import artifact_tools
from axiom.config import AxiomConfig
from axiom.mcp import McpClientManager
from axiom.tools import ToolRegistry, get_builtin_tools


async def build_tool_registry(
    *,
    config: AxiomConfig,
    cwd: str,
) -> tuple[ToolRegistry, McpClientManager | None]:
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    if config.artifacts.enabled:
        registry.register_all(artifact_tools())
    if config.provenance.enabled:
        from axiom.provenance import provenance_tools

        registry.register_all(provenance_tools())
    manager: McpClientManager | None = None
    if config.features.mcp:
        manager = McpClientManager(cwd, execution_config=config.execution)
        registry.register_all(await manager.load_tools())
    return registry, manager
