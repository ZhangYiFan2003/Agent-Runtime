from __future__ import annotations

import asyncio
import glob as glob_module
import os
import re
from pathlib import Path
from typing import Any

from axiom.lsp import diagnose_file
from axiom.memory import MemoryManager
from axiom.policy import Capability, CommandGuard, PathGuard
from axiom.rag.code_index_factory import create_code_index
from axiom.rag.context import serialized_item_text
from axiom.rag.embeddings import EmbeddingError
from axiom.skill import SkillRegistry
from axiom.snapshot import SnapshotService
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema
from axiom.web import fetch_url, search_web


def get_builtin_tools() -> list[Tool]:
    tools = [
        Tool(
            name="read_file",
            description="Read a text file from the current workspace.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Path to read"},
                    "offset": {"type": "number", "description": "Start line, 1-based"},
                    "limit": {"type": "number", "description": "Maximum number of lines"},
                },
                ["path"],
            ),
            required_keys=["path"],
            handler=read_file,
            capabilities=(Capability.FILESYSTEM_READ.value,),
            path_argument_names=("path",),
        ),
        Tool(
            name="write_file",
            description="Write a UTF-8 text file inside the current workspace.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Path to write"},
                    "content": {"type": "string", "description": "File content"},
                    "append": {"type": "boolean", "description": "Append instead of overwrite"},
                },
                ["path", "content"],
            ),
            required_keys=["path", "content"],
            handler=write_file,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            capabilities=(Capability.FILESYSTEM_WRITE.value,),
            path_argument_names=("path",),
        ),
        Tool(
            name="list_dir",
            description="List entries in a directory inside the current workspace.",
            parameters=object_schema(
                {"path": {"type": "string", "description": "Directory path"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=list_dir,
            capabilities=(Capability.FILESYSTEM_READ.value,),
            path_argument_names=("path",),
        ),
        Tool(
            name="glob",
            description="Find files by glob pattern inside the current workspace.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "limit": {"type": "number", "description": "Maximum results"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=glob_files,
            capabilities=(Capability.FILESYSTEM_READ.value,),
            path_argument_names=("pattern",),
        ),
        Tool(
            name="glob_files",
            description="Alias of glob. Find files by glob pattern inside the current workspace.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "limit": {"type": "number", "description": "Maximum results"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=glob_files,
            capabilities=(Capability.FILESYSTEM_READ.value,),
            path_argument_names=("pattern",),
        ),
        Tool(
            name="grep",
            description="Search text in workspace files.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Regex or plain text pattern"},
                    "path": {"type": "string", "description": "Optional path to search"},
                    "regex": {"type": "boolean", "description": "Treat pattern as regex"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=grep,
            capabilities=(Capability.FILESYSTEM_READ.value,),
            path_argument_names=("path",),
        ),
        Tool(
            name="grep_code",
            description="Alias of grep. Search text in workspace files.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Regex or plain text pattern"},
                    "path": {"type": "string", "description": "Optional path to search"},
                    "regex": {"type": "boolean", "description": "Treat pattern as regex"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=grep,
            capabilities=(Capability.FILESYSTEM_READ.value,),
            path_argument_names=("path",),
        ),
        Tool(
            name="bash",
            description="Execute a shell command in the current workspace.",
            parameters=object_schema(
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "timeout": {"type": "number", "description": "Timeout seconds"},
                },
                ["command"],
            ),
            required_keys=["command"],
            handler=bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=(Capability.SHELL_EXECUTE.value,),
        ),
        Tool(
            name="execute_command",
            description="Alias of bash. Execute a shell command in the current workspace.",
            parameters=object_schema(
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "timeout": {"type": "number", "description": "Timeout seconds"},
                },
                ["command"],
            ),
            required_keys=["command"],
            handler=bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=(Capability.SHELL_EXECUTE.value,),
        ),
        Tool(
            name="web_search",
            description=(
                "Search the web for current information. Returns titles, URLs, and snippets."
            ),
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "number", "description": "Maximum result count"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=web_search,
            capabilities=(Capability.NETWORK_READ.value,),
        ),
        Tool(
            name="web_fetch",
            description="Fetch a public HTTP/HTTPS page and return readable text.",
            parameters=object_schema(
                {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_length": {"type": "number", "description": "Maximum returned characters"},
                },
                ["url"],
            ),
            required_keys=["url"],
            handler=web_fetch,
            capabilities=(Capability.NETWORK_READ.value,),
        ),
        Tool(
            name="save_memory",
            description="Save a stable fact to long-term project memory.",
            parameters=object_schema(
                {"content": {"type": "string", "description": "Fact to remember"}},
                ["content"],
            ),
            required_keys=["content"],
            handler=save_memory,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            capabilities=(Capability.EXTERNAL_SIDE_EFFECT.value,),
        ),
        Tool(
            name="load_skill",
            description=(
                "Load a named Axiom Agent Runtime skill manual from user/project skill directories."
            ),
            parameters=object_schema(
                {"name": {"type": "string", "description": "Skill name"}},
                ["name"],
            ),
            required_keys=["name"],
            handler=load_skill,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="search_code",
            description=(
                "FTS5/BM25 lexical search over AST code chunks with identifier "
                "and Chinese tokenization; falls back to tokenized LIKE when FTS5 is unavailable."
            ),
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=search_code,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="get_code_context",
            description=(
                "Assemble graph-aware code context from search seeds, symbol definitions, "
                "references, callers, callees, and bounded call paths."
            ),
            parameters=object_schema(
                {
                    "query": {
                        "type": "string",
                        "description": "Natural language query or unique symbol",
                    },
                    "mode": {"type": "string", "description": "auto, lexical, vector, or hybrid"},
                    "max_graph_depth": {"type": "number", "description": "Maximum call path depth"},
                    "max_context_chars": {
                        "type": "number",
                        "description": "Maximum context characters",
                    },
                    "max_items": {"type": "number", "description": "Maximum context items"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=get_code_context,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="find_symbol",
            description="Find static symbol definitions in the current workspace code index.",
            parameters=object_schema(
                {
                    "name": {"type": "string", "description": "Symbol name"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["name"],
            ),
            required_keys=["name"],
            handler=find_symbol,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="find_references",
            description=(
                "Find indexed static symbol references by symbol id or name. "
                "Does not perform call graph or call-chain analysis."
            ),
            parameters=object_schema(
                {
                    "symbol": {"type": "string", "description": "Symbol id or name"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["symbol"],
            ),
            required_keys=["symbol"],
            handler=find_references,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="find_callers",
            description=(
                "Find direct callers from the static high-confidence call graph. "
                "This is not a complete runtime call graph."
            ),
            parameters=object_schema(
                {
                    "symbol": {"type": "string", "description": "Symbol id or unique name"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["symbol"],
            ),
            required_keys=["symbol"],
            handler=find_callers,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="find_callees",
            description=(
                "Find direct callees from the static high-confidence call graph. "
                "This is not a complete runtime call graph."
            ),
            parameters=object_schema(
                {
                    "symbol": {"type": "string", "description": "Symbol id or unique name"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["symbol"],
            ),
            required_keys=["symbol"],
            handler=find_callees,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="trace_call_chain",
            description=(
                "Trace bounded paths in the static high-confidence call graph. "
                "Dynamic and ambiguous calls are excluded."
            ),
            parameters=object_schema(
                {
                    "symbol": {"type": "string", "description": "Symbol id or unique name"},
                    "direction": {"type": "string", "description": "outgoing or incoming"},
                    "max_depth": {"type": "number", "description": "Maximum depth"},
                    "max_paths": {"type": "number", "description": "Maximum paths"},
                },
                ["symbol"],
            ),
            required_keys=["symbol"],
            handler=trace_call_chain,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="find_recursive_components",
            description="Find recursive components in the static high-confidence call graph.",
            parameters=object_schema(
                {"limit": {"type": "number", "description": "Maximum components"}},
                [],
            ),
            required_keys=[],
            handler=find_recursive_components,
            capabilities=(Capability.FILESYSTEM_READ.value,),
        ),
        Tool(
            name="revert_turn",
            description=(
                "Restore the workspace to a previous Axiom Agent Runtime side-history snapshot."
            ),
            parameters=object_schema(
                {"snapshot": {"type": "string", "description": "Snapshot id or 1-based index"}},
                ["snapshot"],
            ),
            required_keys=["snapshot"],
            handler=revert_turn,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            capabilities=(Capability.FILESYSTEM_WRITE.value,),
        ),
    ]
    return tools


async def read_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _resolve_path(context, str(payload["path"]))
    offset = max(int(payload.get("offset") or 1), 1)
    limit = int(payload.get("limit") or 500)
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    selected = content[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(f"{idx + offset}: {line}" for idx, line in enumerate(selected))
    return ToolResult(numbered, display_summary=f"Read {path.relative_to(context.cwd)}")


async def write_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _resolve_path(context, str(payload["path"]))
    content = str(payload["content"])
    if len(content.encode("utf-8")) > 5 * 1024 * 1024:
        return ToolResult("write_file rejected: content exceeds 5MB", is_error=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if payload.get("append") else "w"
    with path.open(mode, encoding="utf-8") as handle:
        handle.write(content)
    rel = path.relative_to(context.cwd)
    diagnostics = diagnose_file(path)
    suffix = ""
    if diagnostics:
        suffix = "\n\nDiagnostics:\n" + "\n".join(diagnostics)
    return ToolResult(f"Wrote {rel}{suffix}", display_summary=f"Wrote {rel}")


async def list_dir(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    path = _resolve_path(context, str(payload["path"]))
    if not path.is_dir():
        return ToolResult(f"Not a directory: {path}", is_error=True)
    rows = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        marker = "/" if child.is_dir() else ""
        rows.append(f"{child.name}{marker}")
    return ToolResult("\n".join(rows) or "(empty directory)")


async def glob_files(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    root = Path(context.cwd).resolve()
    pattern = str(payload["pattern"])
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        return ToolResult("glob pattern must stay inside workspace", is_error=True)
    limit = int(payload.get("limit") or 100)
    matches = glob_module.glob(str(root / pattern), recursive=True)
    rels = []
    for match in sorted(matches):
        path = Path(match).resolve()
        try:
            rels.append(str(path.relative_to(root)))
        except ValueError:
            continue
        if len(rels) >= limit:
            break
    return ToolResult("\n".join(rels) or "(no matches)")


async def grep(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    root = Path(context.cwd).resolve()
    start = _resolve_path(context, str(payload.get("path") or "."))
    pattern = str(payload["pattern"])
    limit = int(payload.get("limit") or 100)
    use_regex = bool(payload.get("regex", True))
    try:
        compiled = re.compile(pattern) if use_regex else None
    except re.error as exc:
        return ToolResult(f"invalid regex: {exc}", is_error=True)

    matches: list[str] = []
    files = [start] if start.is_file() else [p for p in start.rglob("*") if p.is_file()]
    for file_path in files:
        if _skip_file(file_path):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            found = bool(compiled.search(line)) if compiled else pattern in line
            if found:
                matches.append(f"{file_path.relative_to(root)}:{line_number}: {line.strip()}")
                if len(matches) >= limit:
                    return ToolResult("\n".join(matches))
    return ToolResult("\n".join(matches) or "(no matches)")


async def bash(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    command = str(payload["command"])
    CommandGuard(context.config.policy.command_blacklist).validate(command)
    timeout = float(payload.get("timeout") or context.config.tools.timeout)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=context.cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ToolResult(f"Command timed out after {timeout:.0f}s", is_error=True)
    output = (stdout + stderr).decode("utf-8", errors="replace")
    if len(output) > 20_000:
        output = output[:20_000] + "\n... [truncated]"
    return ToolResult(
        output or f"(exit {proc.returncode}, no output)",
        is_error=proc.returncode != 0,
    )


async def web_search(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
    max_results = int(payload.get("max_results") or payload.get("maxResults") or 5)
    try:
        results = await search_web(str(payload["query"]), max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Search error: {exc}", is_error=True)
    if not results:
        return ToolResult(f'No search results found for "{payload["query"]}".')
    content = "\n\n".join(
        f"{index}. {result.title}\n{result.url}\n{result.snippet}"
        for index, result in enumerate(results, start=1)
    )
    return ToolResult(content, display_summary=f"Search: {len(results)} results")


async def web_fetch(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
    max_length = int(payload.get("max_length") or payload.get("maxLength") or 10_000)
    try:
        content = await fetch_url(str(payload["url"]), max_length=max_length)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Fetch error: {exc}", is_error=True)
    return ToolResult(content, display_summary=f"Fetched {payload['url']}")


async def save_memory(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.config.features.memory or not context.config.memory.long_term_enabled:
        return ToolResult("Long-term memory is disabled.", is_error=True)
    manager = MemoryManager(context.config.memory.long_term_db_path, scope=context.cwd)
    memory_id = manager.save(str(payload["content"]))
    return ToolResult(f"Saved memory #{memory_id}")


async def load_skill(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    skill = SkillRegistry(context.cwd).load(str(payload["name"]))
    if not skill:
        return ToolResult(f'Skill "{payload["name"]}" not found or disabled.', is_error=True)
    content = skill.body or skill.content
    if len(content) > 5_000:
        content = content[:5_000] + "\n... [truncated; use /skill show for the full skill]"
    if context.skill_context_buffer:
        context.skill_context_buffer.push(skill.name, content)
        return ToolResult(
            f'Loaded skill "{skill.name}" instructions for the next model turn.',
            display_summary=f"Loaded skill {skill.name}",
        )
    return ToolResult(content, display_summary=f"Loaded skill {skill.name}")


async def search_code(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    try:
        results = await asyncio.to_thread(
            index.search,
            str(payload["query"]),
            int(payload.get("limit") or 20),
            mode=context.config.embedding.search_mode,
        )
    except EmbeddingError as exc:
        return ToolResult(f"Embedding search unavailable: {exc}", is_error=True)
    if not results:
        return ToolResult("(no indexed matches; run /index first)")
    backend = results[0].backend or "lexical"
    return ToolResult(
        "\n".join(f"{item.path}:{item.line}: {item.snippet}" for item in results),
        display_summary=f"code search: {backend}",
    )


async def get_code_context(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    try:
        result = await asyncio.to_thread(
            index.build_code_context,
            str(payload["query"]),
            mode=str(payload.get("mode") or "auto"),
            max_graph_depth=int(payload.get("max_graph_depth") or 2),
            max_context_chars=int(payload.get("max_context_chars") or 24_000),
            max_items=int(payload.get("max_items") or 30),
        )
    except EmbeddingError as exc:
        return ToolResult(f"Embedding search unavailable: {exc}", is_error=True)
    if not result.items:
        return ToolResult("(no context items; run /index first)")
    rows = [
        (
            f"query: {result.query}\n"
            f"budget: {result.estimated_tokens}/{result.max_estimated_tokens} estimated tokens, "
            f"{result.estimated_chars}/{result.max_context_chars} chars, "
            f"truncated={str(result.truncated).lower()}, "
            f"seeds={result.seed_count}, expanded_symbols={result.expanded_symbol_count}"
        )
    ]
    for item in result.items:
        rows.append("---\n" + serialized_item_text(item).rstrip())
    return ToolResult("\n\n".join(rows), display_summary=f"context: {len(result.items)} items")


async def find_symbol(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    definitions = await asyncio.to_thread(
        index.find_definitions,
        str(payload["name"]),
        limit=int(payload.get("limit") or 20),
    )
    if not definitions:
        return ToolResult("(no symbols; run /index first)")
    return ToolResult(
        "\n".join(
            f"{item.file_path}:{item.start_line}: {item.symbol_kind} {item.qualified_name}"
            for item in definitions
        ),
        display_summary=f"symbols: {len(definitions)}",
    )


async def find_references(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    references = await asyncio.to_thread(
        index.find_references,
        str(payload["symbol"]),
        limit=int(payload.get("limit") or 100),
    )
    if not references:
        return ToolResult("(no references; run /index first)")
    return ToolResult(
        "\n".join(
            f"{item.file_path}:{item.start_line}: {item.reference_kind} "
            f"{item.qualifier + '.' if item.qualifier else ''}{item.name} "
            f"[{item.resolution_status}]"
            for item in references
        ),
        display_summary=f"references: {len(references)}",
    )


async def find_callers(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    try:
        edges = await asyncio.to_thread(
            index.find_callers,
            str(payload["symbol"]),
            limit=int(payload.get("limit") or 20),
        )
    except ValueError as exc:
        return ToolResult(_format_lookup_error(exc), is_error=True)
    return _call_edge_result(index, edges, "callers")


async def find_callees(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    try:
        edges = await asyncio.to_thread(
            index.find_callees,
            str(payload["symbol"]),
            limit=int(payload.get("limit") or 20),
        )
    except ValueError as exc:
        return ToolResult(_format_lookup_error(exc), is_error=True)
    return _call_edge_result(index, edges, "callees")


async def trace_call_chain(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    try:
        result = await asyncio.to_thread(
            index.trace_call_paths,
            str(payload["symbol"]),
            direction=str(payload.get("direction") or "outgoing"),
            max_depth=int(payload.get("max_depth") or 3),
            max_paths=int(payload.get("max_paths") or 50),
        )
    except ValueError as exc:
        return ToolResult(_format_lookup_error(exc), is_error=True)
    definitions = {item.id: item for item in index.store.list_symbol_definitions()}
    rows = []
    for path in result.paths:
        labels = [
            definitions[symbol_id].qualified_name if symbol_id in definitions else symbol_id[:12]
            for symbol_id in path.symbol_ids
        ]
        rows.append(" -> ".join(labels) + (" [cycle]" if path.cycle else ""))
    if result.truncated:
        rows.append("[truncated]")
    return ToolResult("\n".join(rows) or "(no call paths)", display_summary="call chain")


async def find_recursive_components(
    payload: dict[str, Any],
    context: ToolContext,
) -> ToolResult:
    index = create_code_index(context.cwd, context.config)
    limit = int(payload.get("limit") or 20)
    components = await asyncio.to_thread(index.find_recursive_components)
    definitions = {item.id: item for item in index.store.list_symbol_definitions()}
    rows = []
    for component in components[:limit]:
        names = [
            definitions[symbol_id].qualified_name if symbol_id in definitions else symbol_id[:12]
            for symbol_id in component.symbol_ids
        ]
        rows.append(f"{component.recursion_kind}: " + " <-> ".join(names))
    return ToolResult(
        "\n".join(rows) or "(no recursive components)",
        display_summary=f"recursive components: {len(rows)}",
    )


def _call_edge_result(index, edges, label: str) -> ToolResult:
    definitions = {item.id: item for item in index.store.list_symbol_definitions()}
    rows = []
    for edge in edges:
        caller = definitions.get(edge.caller_symbol_id)
        callee = definitions.get(edge.callee_symbol_id)
        if not caller or not callee:
            continue
        rows.append(
            f"{edge.file_path}:{edge.start_line}: "
            f"{caller.qualified_name} -> {callee.qualified_name}"
        )
    return ToolResult("\n".join(rows) or f"(no {label})", display_summary=f"{label}: {len(rows)}")


def _format_lookup_error(exc: ValueError) -> str:
    candidates = getattr(exc, "candidates", ())
    if not candidates:
        return str(exc)
    rows = [str(exc), "Candidates:"]
    rows.extend(
        f"{item.file_path}:{item.start_line}: {item.qualified_name} [{item.id[:12]}]"
        for item in candidates
    )
    return "\n".join(rows)


async def revert_turn(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    record = SnapshotService(context.cwd).restore(str(payload["snapshot"]))
    return ToolResult(f"Restored snapshot {record.id}")


def _resolve_path(context: ToolContext, value: str) -> Path:
    if context.config.policy.path_guard_enabled:
        return PathGuard(context.cwd).validate(value)
    path = Path(value)
    return path if path.is_absolute() else Path(context.cwd).resolve() / path


def _skip_file(path: Path) -> bool:
    skip_dirs = {".git", ".venv", "node_modules", "dist", "build", "target"}
    if any(part in skip_dirs for part in path.parts):
        return True
    return path.stat().st_size > 1_000_000
