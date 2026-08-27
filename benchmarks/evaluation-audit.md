# Existing Evaluation Asset Audit

Audit baseline: `2516518260894f350aa4e3df9ed937ff2d382d0f`.

## Retrieval assets

- `tests/fixtures/code_search_queries.json` contains 15 deterministic lexical
  queries over the small synthetic `tests/fixtures/code_index_project` fixture.
- `tests/fixtures/code_search_vector_queries.json` contains 10 deterministic
  paraphrase-oriented queries over the same fixture. The embedding provider is
  a hand-authored concept fixture; it is not a production semantic model.
- The combined offline search fixture therefore has 25 queries. It is not
  grounded in the Axiom repository itself.
- `tests/fixtures/code_context_queries.json` contains 6 deterministic context
  cases over another synthetic project. Its metrics compare search seed symbol
  hits with graph-expanded symbol/file coverage; they are context coverage
  metrics, not ranked retrieval Recall.
- `benchmarks/benchmark_code_search.py` is a synthetic latency microbenchmark.
  It generates code and reports timings; it does not evaluate repository
  retrieval quality.

## Agent evaluation assets

- `benchmarks/datasets/agent-core.json` contains 12 repository-oriented cases.
- The scorer registry supports `exact_match`, `contains`, `run_status`,
  `tool_usage`, and `metric_threshold`.
- `DurableEvaluationExecutor` runs cases through the durable Runtime and records
  status, output, trace identity, tool calls, steps, tokens, and latency.
- The CLI evaluation path loads normal configuration and refuses to run without
  a configured LLM API key. Consequently, a real `agent-core` execution is a
  real-provider benchmark and may be variable and billable.
- Fake/scripted LLM clients are used by tests to prove execution correctness,
  failure isolation, metric capture, and Plan/Multi-Agent aggregation. They do
  not establish model task-success quality.

## Durable recovery assets

Existing deterministic tests cover these recovery boundaries:

- ReAct: completed tool checkpoint reuse; `SUCCEEDED` ToolExecution reuse when
  the parent checkpoint is missing; SQLite approval restart; explicit ambiguous
  `RUNNING` side-effect handling; persisted manual interrupt; concurrent resume;
  and HTTP resume after Runtime recreation.
- Plan: plan creation persistence; crash before plan persistence; completed Child
  observation; Child `SUCCEEDED` tool reuse; approval restart; replan version
  persistence; Child identity persistence before spawn; parallel terminal
  observation with CAS conflict; mixed-state reconciliation; and cancellation.
- Multi-Agent: Worker identity persistence before spawn; completed Child reuse
  before Parent observation; Child `SUCCEEDED` tool reuse; approval restart;
  review/synthesis checkpoint reuse; parallel terminal observation with CAS
  conflict; mixed-state reconciliation; and Parent cancellation.

The tests are currently dispersed across five modules. There is no maintained
fault matrix, repetition runner, scenario-level classification, or JSON/Markdown
recovery result artifact. Some requested crash labels (notably explicit Run
creation, in-flight LLM, and pre-terminal checkpoint windows) have no standalone
named benchmark scenario even though adjacent checkpoint behavior is tested.

## Blocking issue found during benchmark construction

Repository-wide CodeIndex construction exposed duplicate stable definition and
reference IDs when the static extractor encountered repeated nested definitions
or the same nested call more than once on one source line. SQLite correctly
rejected duplicate primary keys, so the retrieval benchmark could not index the
repository. The evidence branch applies a minimal order-preserving
de-duplication at the extractor/store boundary and adds regression tests. This
is a blocking correctness fix, not a ranking or benchmark-score optimization.

The first repository-wide run also exposed a native Tree-sitter access
violation in larger Python modules. The failure reproduces with binding
`0.26.0` and disappears with `0.25.2` against the same grammar wheels and
source. The evidence branch therefore constrains the binding to
`>=0.25.2,<0.26`. This is a benchmark-blocking dependency compatibility fix; it
does not change ranking, embeddings, RRF, or context expansion.


## Existing result schemas

- Evaluation results use schema version 1 and contain dataset metadata,
  aggregate pass/latency/token/step metrics, and per-case scorer details.
- Tool concurrency artifacts use top-level `environment`, `benchmark`, and
  `results` objects. The committed reference measures four synthetic read-only
  I/O tools and `ToolExecutor.execute_all` only. Its approximately 75% four-way
  improvement is not Agent end-to-end latency evidence.
- No committed repository-grounded retrieval result, real-provider fixed-task
  result, or unified recovery fault-injection result exists at this baseline.
