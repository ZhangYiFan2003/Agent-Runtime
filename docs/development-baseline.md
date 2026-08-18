# Development Baseline

This document records the current functional baseline for the local Axiom Agent Runtime
Python project. It is based on source inspection plus safe local verification.
It does not include credentials, tokens, API keys, or private configuration
values.

## 1. Current runtime environment

- Project path: `<repository-root>`
- OS: Windows NT 10.0.22621.0
- Python in project environment: 3.12.13
- uv: 0.11.21
- Axiom Agent Runtime package version: 0.1.0
- Virtual environment: `.venv`
- Lock file: `uv.lock`
- Package manager: uv
- Test runner: pytest, configured in `pyproject.toml`

Previously verified local commands:

```powershell
uv run axiom --help
uv run axiom doctor --cwd .
uv run axiom --plain -p "Reply with OK"
```

The one-shot AI command above was reported as successful before this baseline
stage. This document does not repeat the model request.

## 2. Feature matrix

| Feature | Code location | Status | Verification |
| --- | --- | --- | --- |
| CLI command | `pyproject.toml`, `src/axiom/entrypoints/cli.py` | Verified | `uv run axiom --help`, `uv run axiom --version`, and `uv run axiom doctor --cwd .` ran successfully. |
| REPL | `src/axiom/entrypoints/repl.py` | Not verified | Interactive REPL was not started in this baseline stage. |
| One-shot prompt | `src/axiom/entrypoints/cli.py`, `src/axiom/agent/query_engine.py` | Verified | Minimal AI request was reported successful before this stage with `uv run axiom --plain -p "Reply with OK"`. |
| Streaming | `src/axiom/llm/openai_compatible.py`, `src/axiom/render/rich_renderer.py` | Partially verified | One-shot prompt uses the streaming client path; renderer streaming behavior has unit coverage. Raw chunk-level live provider behavior was not separately inspected. |
| Tool calling | `src/axiom/agent/query.py`, `src/axiom/tools/executor.py` | Verified | Fake LLM tests verify tool-call delta merge, built-in `read_file` execution, tool result emission, observation replay into the second LLM turn, and final response completion. |
| Built-in tools | `src/axiom/tools/builtins.py`, `src/axiom/tools/registry.py` | Partially verified | Tool registry and read/write tool tests passed. Not every built-in tool was exercised end to end. |
| Code search, symbols, call graph, and graph-aware context | `src/axiom/rag/*`, `src/axiom/tools/builtins.py`, `src/axiom/entrypoints/repl.py` | Verified | AST chunking, lexical/vector/hybrid search, symbol resolution, conservative static call graph, and graph-aware context assembly have deterministic no-network fixture tests. |
| ReAct loop | `src/axiom/agent/query.py`, `src/axiom/agent/agent.py` | Verified | `tests/test_query.py` uses a Fake LLM to exercise Agent -> LLM response -> tool call -> tool execution -> observation -> final answer without external API calls. |
| Memory | `src/axiom/memory/*` | Verified | Memory tests verify legacy save/list/search/clear compatibility, additive schema migration, typed memory kinds and scopes, fact extraction, duplicate merge, conflict supersession, retraction, scoped preference reuse, bounded tool-result digests, Map-Reduce summary checkpoints, single-active-summary invariants, incremental ranges, failure fallback, and budgeted deterministic memory context with temporary SQLite databases. |
| Skills | `src/axiom/skill/registry.py`, `src/axiom/tools/builtins.py` | Verified | Skill registry, skill context buffer, and `load_skill` context behavior tests passed. |
| Snapshots | `src/axiom/snapshot/service.py` | Verified | `tests/test_snapshot.py` verifies snapshot creation, restore, listing, cleanup, and ignored cache directory behavior under an isolated temporary home. |
| Plan-execute | `src/axiom/agent/plan_execute.py`, `src/axiom/plan/*`, `src/axiom/runtime/plan_strategy.py` | Verified | `tests/test_plan.py` and `tests/test_durable_plan_runtime.py` verify versioned JSON Plan state, sequential durable dependency execution, SQLite recovery, Tool result reuse, approval/restart, isolation, replan history, trace hierarchy, cancellation, Runtime API selection, and Evaluation integration. |
| Multi-agent | `src/axiom/agent/orchestrator.py`, `src/axiom/runtime/multi_agent_strategy.py` | Verified | Parent/Child identity, crash recovery, approval/restart, Tool reuse, reviewer retry, cancellation, linked traces, isolation, events, and Evaluation are covered by `tests/test_durable_multi_agent_runtime.py`. |
| MCP client | `src/axiom/mcp/client.py`, `src/axiom/mcp/config.py` | Verified | Tests passed for stdio MCP tool discovery/call and stderr suppression. |
| MCP server | `src/axiom/mcp/server.py` | Partially verified | Handler-level tests cover initialize, tools/list, safe tools/call, unknown tools, unknown methods, and malformed missing-method requests. Long-running stdio/http transports were not started in this baseline. |
| Runtime API and durable execution | `src/axiom/runtime/*` | Verified | Runtime lifecycle/API tests plus crash recovery tests cover Memory/SQLite checkpoints, restart resume, approval/reject/manual interrupt, parent/child discovery, restart-safe control-operation idempotency, structured transition conflicts, cancellation cascade, bounded retry, successful-tool deduplication, optimistic sequence conflicts, hierarchical SSE replay, and socket release. |
| Runtime observability | `src/axiom/runtime/observability.py`, `src/axiom/runtime/observability_store.py` | Verified | Deterministic tests cover LLM token/TTFT/latency spans, Tool success/failure/retry/reuse and ambiguity attributes, interrupt/resume continuity, crash recovery on one trace, SQLite reload, lifecycle Events, HTTP metrics/trace endpoints, and CLI rendering. |
| Agent evaluation | `src/axiom/evaluation/*`, `benchmarks/datasets/agent-core.json` | Verified | Tests cover JSON dataset validation, five deterministic scorers, composite scoring, real durable Runtime execution, failed Runs, metrics projection, JSON report round-trip, regression comparison, and CLI wiring. |

Status definitions:

- Verified: exercised by a successful command or passing test.
- Partially verified: code path or subcomponent was exercised, but important
  end-to-end behavior remains unverified or currently blocked.
- Not verified: implementation exists, but no successful local verification was
  recorded in this baseline.
- Not implemented: no implementation was found.

## 3. Minimal verification cases

These are safe baseline cases for future repeatable verification. They avoid
secrets and external services unless explicitly noted.

| Feature | Minimal case | Cost/network | Data modification |
| --- | --- | --- | --- |
| CLI command | `uv run axiom --help` | None expected | No |
| Doctor | `uv run axiom doctor --cwd .` | None expected | No |
| One-shot prompt | `uv run axiom --plain -p "Reply with OK"` | May call paid API | Creates snapshots and uses configured provider |
| Built-in tools | Run existing `tests/test_tools.py` | None expected | Temp files only |
| Code search/context | Run existing `tests/test_code_index_ast.py`, `tests/test_code_search_lexical.py`, `tests/test_code_search_vector.py`, `tests/test_code_symbol_index.py`, `tests/test_code_call_graph.py`, and `tests/test_code_context.py` | None expected | Temp files and temp SQLite code indexes only |
| Tool calling/ReAct | Run existing `tests/test_query.py` | None expected | Temp files plus snapshot state |
| Skills | Run existing `tests/test_skill.py` | None expected | Temp files and temp home state |
| Snapshots | Run existing `tests/test_snapshot.py` | None expected | Temp project and snapshot state |
| Plan-execute | Run existing `tests/test_plan.py` | None expected | Temp files plus snapshot state |
| Multi-agent | Run existing `tests/test_multi_agent.py` | None expected | Temp files plus snapshot state |
| MCP client/server handler | Run existing `tests/test_mcp.py` | None expected | Temp MCP server files |
| Runtime task/API live localhost | Run existing `tests/test_runtime.py` | Localhost only | Temp SQLite files under isolated `data_dir` |

## 4. Test baseline

Pytest configuration:

- Location: `pyproject.toml`
- `testpaths = ["tests"]`
- `addopts = "-q"`

Test files:

- `tests/test_code_call_graph.py`
- `tests/test_code_context.py`
- `tests/test_code_index_ast.py`
- `tests/test_code_search_lexical.py`
- `tests/test_code_search_vector.py`
- `tests/test_code_symbol_index.py`
- `tests/test_config.py`
- `tests/test_image.py`
- `tests/test_lsp.py`
- `tests/test_mcp.py`
- `tests/test_memory.py`
- `tests/test_memory_facts.py`
- `tests/test_memory_summary.py`
- `tests/test_multi_agent.py`
- `tests/test_plan.py`
- `tests/test_policy.py`
- `tests/test_query.py`
- `tests/test_render.py`
- `tests/test_runtime.py`
- `tests/test_skill.py`
- `tests/test_snapshot.py`
- `tests/test_tools.py`

Command executed:

```powershell
uv run pytest
```

Result:

- Passed: 145
- Failed: 0
- Skipped: 0
- Total executed: 145

The pytest baseline is currently green after test home-directory isolation was
added for Windows.

## 5. Current risks

- The one-shot prompt path can call a paid provider when API configuration is
  present. It should not be included in default local test runs.
- Interactive REPL behavior is less extensively covered than non-interactive paths, though code context slash-command dispatch is now covered without launching a terminal.
- Runtime API public deployment, load testing, distributed queues, real-provider CI, and unlimited live streaming are not covered by the current automated baseline.
- Runtime thread events, typed memory records, Map-Reduce summary checkpoints, and conservative scoped fact/preference extraction are persisted locally, but semantic memory retrieval, production LLM extraction evaluation, remote summarization/extraction CI, and cross-project preference sharing are not implemented yet.
- MCP long-running stdio/http server behavior is only partially covered through
  request handler and client tests.
- Broader end-to-end coverage for every built-in tool is still incomplete, though code search, symbol, call graph, and graph-aware context tools now have deterministic fixture coverage.

## 6. Recommended next-stage tasks

1. Add a capability-based permission policy before expanding evaluation tasks that mutate workspaces or external systems.
2. Add MCP stdio/http transport lifecycle tests with deterministic process cleanup.
3. Add optional LLM-backed memory extraction evaluation with strict schemas and no default network calls.
4. Broaden no-network tests for high-risk built-in tools beyond the current read/write, code search, Runtime API, and ReAct `read_file` paths.
5. Keep paid model verification as an explicit manual smoke command, never as a default automated test.
