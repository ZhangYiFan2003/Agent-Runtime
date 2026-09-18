# Tool Concurrency Benchmark

This benchmark measures only `ToolExecutor.execute_all` for fixed synthetic I/O
read tools. It does not call an LLM, external APIs, external MCP servers, or the
network.

The fixture registers four independent tools named `io_task_1` through
`io_task_4`. Each tool is read-only, concurrency-safe, and sleeps for a fixed
duration before returning a deterministic result.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe benchmarks\benchmark_tool_concurrency.py `
  --warmups 5 `
  --runs 30 `
  --delay 0.2 `
  --output benchmarks\results\tool-concurrency-windows-py312.json
```

The JSON output records environment details, benchmark settings, and timing
statistics for `max_concurrent_read` values of 1, 2, and 4. A Markdown summary
is written next to the JSON file.

Machine-specific result files under `benchmarks/results/` are evidence artifacts.
Review them before deciding whether to commit them.

## Code Search Benchmark

`benchmark_code_search.py` measures local lexical, vector-only, and hybrid code
search over generated synthetic Python chunks. It uses a deterministic local
embedding provider and does not call an LLM, a remote embedding endpoint, the
network, or a real repository index.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe benchmarks\benchmark_code_search.py `
  --chunks 100 1000 5000 `
  --dimensions 64 `
  --warmups 5 `
  --runs 30 `
  --output benchmarks\results\code-search-local.json
```

The benchmark separates query embedding time, vector scan time, and end-to-end
local search timings for lexical, vector-only, and hybrid modes. Result files
are machine-specific evidence artifacts and should be reviewed before commit.

## Call Graph Benchmark

`benchmark_call_graph.py` measures local static call graph operations over
generated synthetic edges. It does not parse source files, read a real
repository index, call an LLM, use embeddings, or access the network.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe benchmarks\benchmark_call_graph.py `
  --edges 100 1000 10000 `
  --warmups 5 `
  --runs 30 `
  --output benchmarks\results\call-graph-local.json
```

The benchmark reports direct callers, direct callees, depth-3 traversal, and SCC
timings. Result files are machine-specific evidence artifacts and should be
reviewed before commit.

## Agent Evaluation Dataset

`datasets/agent-core.json` is a maintained task-success benchmark that runs through the real Axiom
durable Runtime. Unlike the synthetic performance benchmarks above, it calls the configured model
provider and may incur cost.

```powershell
uv run axiom eval run benchmarks/datasets/agent-core.json `
  --cwd . `
  --data-dir .tmp/eval-runtime `
  --output .tmp/agent-core-result.json
```

See `docs/evaluation.md` for scorer semantics and regression comparison.

## Evaluation Evidence Pack

Three maintained evidence suites keep different claims separate:

- `retrieval/`: 80 repository-grounded queries across lexical, deterministic
  vector, hybrid RRF, and separately reported graph-context coverage.
- `agent-runtime/`: 40 read-only fixed tasks. A real provider is required before
  reporting Task Success Rate.
- `recovery/`: 25 deterministic fault scenarios with two repetitions by
  default. No model provider is required; four distributed-redelivery cases
  require a real PostgreSQL test DSN.

Committed artifacts are evidence snapshots, not CI performance thresholds.
Synthetic Tool concurrency remains `ToolExecutor.execute_all` only and is not
Agent end-to-end latency evidence.

Retrieval v2 keeps that 80-query artifact immutable, adds independent 40-query
development and 20-query holdout splits, and reports symbol-aware three-source
RRF, candidate recall, per-category quality, graph coverage, and local latency
in `retrieval/results/`.
