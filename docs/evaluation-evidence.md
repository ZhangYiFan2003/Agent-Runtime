# Evaluation Evidence and Resume Claim Mapping

This document maps public claims to source, tests, and reproducible artifacts.
It deliberately separates functional support from quantitative evidence.

| Resume Claim | Code Evidence | Test Evidence | Benchmark Evidence | Safe Wording | Unsupported Wording |
| --- | --- | --- | --- | --- | --- |
| ReAct / Plan / Multi-Agent + bounded DAG | `runtime/durable.py`, `plan_strategy.py`, `multi_agent_strategy.py` | durable Runtime and parallelism suites | recovery matrix covers all three strategies | Built durable ReAct, Plan-and-Execute, and Multi-Agent execution with bounded local DAG/Child scheduling | Distributed scheduler or exactly-once arbitrary side effects |
| Four read-only Tool concurrency | `tools/executor.py` | `test_tool_concurrency.py` | `benchmarks/results/tool-concurrency-windows-py312.json` | Four synthetic read-only I/O tools improved `ToolExecutor.execute_all` mean latency by 74.99% vs serial on one machine | 75% Agent end-to-end latency improvement |
| MCP stdio / Streamable HTTP | `mcp/client.py`, `mcp/server.py` | MCP client/server tests | fixed-task dataset includes repository inspection only | Supports MCP stdio and Streamable HTTP integration | External MCP reliability or latency percentage |
| Code Intelligence retrieval | `rag/code_index.py`, `hybrid.py`, `context.py` | lexical/vector/context suites | `benchmarks/retrieval/results/retrieval-offline-baseline.json` | On 80 repository-specific offline queries, hybrid Recall@5 was 0.5125; deterministic embedding boundary disclosed | Production semantic recall or cross-repository accuracy |
| Thread/Turn/Event + Memory | `runtime/api.py`, `memory/` | Runtime API, replay, and memory suites | fixed-task dataset is prepared; no real-provider result | Implements Thread/Turn/Event persistence, SSE replay, and bounded memory context | Quantified memory answer-quality improvement |
| Run/Checkpoint + ToolExecution recovery | `runtime/models.py`, `checkpoints.py`, `durable.py` | durable recovery suites | `benchmarks/recovery/results/recovery-baseline.json` | 42/42 deterministic fault executions reached recovered or expected-safe outcomes with asserted duplicates/losses at zero | Exactly-once arbitrary external side effects or distributed recovery |
| Trace/Span + fixed-task Evaluation | `runtime/observability.py`, `evaluation/` | observability and evaluation suites | 40-case dataset validated; real provider not executed | Implements Trace/Span metrics and deterministic fixed-task scorers; 40-case provider benchmark is reproducible | Task Success Rate without a real-provider artifact |

## Quantitative support status

- Fully supported quantitative artifacts: synthetic four-tool executor timing,
  repository-specific offline retrieval, deterministic recovery matrix.
- Supported without quantitative provider evidence: MCP transport support,
  Thread/Turn/Event and Memory, Trace/Span, 40-case Agent dataset.
- Unsupported quantitative claim: any real-provider Task Success Rate in this
  evidence pack, because no provider run was executed.
