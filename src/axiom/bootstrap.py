from __future__ import annotations

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
    manager: McpClientManager | None = None
    if config.features.mcp:
        manager = McpClientManager(cwd, execution_config=config.execution)
        registry.register_all(await manager.load_tools())
    return registry, manager
