# Agent Evaluation Framework v1

Axiom Evaluation runs fixed tasks through the real durable Agent Runtime, scores persisted Run
outcomes with deterministic rules, and writes portable JSON reports for regression comparison. It
does not call `llm.chat()` directly and does not send HTTP requests to a local Runtime server.

## Execution model

```text
EvaluationCase
    ↓
DurableEvaluationExecutor
    ↓
Thread ID → Turn ID → DurableAgentRuntime Run
    ↓
Checkpoint + ToolExecution + Trace/Span
    ↓
EvaluationRunResult
    ↓
Scorers
    ↓
EvaluationSuiteResult JSON
```

Every case execution receives an independent Run, Turn, and Thread identity. The same case may
therefore produce multiple Runs in later repetition/model-comparison workflows without merging the
case specification with Runtime state.

The executor obtains the final answer and status from the latest Checkpoint, aggregate usage from
`RunMetrics`, and tool names from persisted Tool spans. Evaluation datasets and reports are files;
no evaluation-specific SQLite tables are added.

## Dataset format

Evaluation v1 intentionally supports JSON only. JSON matches the repository's explicit persistence
formats, requires no new YAML dependency, and produces directly diffable benchmark files.

```json
{
  "schema_version": 1,
  "name": "agent-core",
  "version": "1.0.0",
  "metadata": {"description": "Core repository tasks"},
  "cases": [
    {
      "id": "locate_checkpoint_store",
      "name": "Locate checkpoint persistence",
      "prompt": "Find where CheckpointStore is implemented.",
      "tags": ["runtime", "tool-use"],
      "timeout_seconds": 120,
      "scorers": [
        {"type": "run_status", "expected": "COMPLETED"},
        {"type": "contains", "expected": "checkpoints.py"},
        {
          "type": "tool_usage",
          "required_tools": ["grep"],
          "forbidden_tools": ["write_file"]
        },
        {"type": "metric_threshold", "max_steps": 10, "max_tokens": 10000}
      ]
    }
  ]
}
```

`metadata`, `setup`, and `expected` are JSON extension fields. v1 does not execute arbitrary setup
code. A case with no scorer receives the safe default `RunStatusScorer(COMPLETED)`.

The maintained starter dataset is
[`benchmarks/datasets/agent-core.json`](../benchmarks/datasets/agent-core.json). It contains a small
set of repository-grounded reasoning, tool-selection, multi-step, failure-recovery, durable, and
observability tasks. Running it uses the configured model provider and may incur provider cost.

## Scorers

All built-in scorers are async-compatible, deterministic, and return an explainable `ScoreResult`:

- `contains`: case-insensitive by default; one or multiple required substrings; supports `match_all`.
- `exact_match`: optional trimming and case sensitivity for deterministic answers.
- `tool_usage`: required and forbidden tool sets; duplicates and ordering are ignored for success.
- `run_status`: accepted Runtime statuses, defaulting to `COMPLETED`.
- `metric_threshold`: `max_steps`, `max_tokens`, `max_latency_ms`, and `max_tool_calls`.

Every scorer has `required` (default `true`) and a numeric score reserved for later weighting. A case
passes when all required scorers pass. Optional scorer failures remain visible without failing the
case. Evaluation does not require an exact tool-call sequence because model execution is naturally
nondeterministic.

The public `Scorer` protocol and injectable scorer factory allow future RAG metrics or an optional
LLM judge without making them core dependencies.

## Running an evaluation

```bash
axiom eval run benchmarks/datasets/agent-core.json \
  --cwd . \
  --data-dir .tmp/eval-runtime \
  --output .tmp/agent-core-result.json

axiom eval run benchmarks/datasets/agent-core.json --verbose
```

Cases run sequentially to reduce provider rate-limit interference, SQLite contention, and benchmark
nondeterminism. Runtime checkpoints and traces are written to `<data-dir>/runtime.db`. The report
contains only Run/Trace references and derived summaries, not a copy of each Trace.

## Result format

```json
{
  "schema_version": 1,
  "dataset": "agent-core",
  "dataset_version": "1.0.0",
  "cases_total": 12,
  "cases_passed": 10,
  "cases_failed": 2,
  "pass_rate": 0.8333,
  "avg_latency_ms": 4800.0,
  "avg_tokens": 6240.0,
  "avg_steps": 5.4,
  "results": []
}
```

Each case result includes `run_id`, `thread_id`, `turn_id`, `trace_id`, status, final assistant
output, score explanations, token counts, duration, tool names, steps, and error metadata.

## Regression comparison

```bash
axiom eval compare baseline.json candidate.json
```

The comparison separates:

- functional regressions: a shared case changed from PASS to FAIL;
- improvements: a shared case changed from FAIL to PASS;
- aggregate changes: pass rate, average tokens, latency, and steps;
- performance warnings: tokens `+20%`, latency `+30%`, or steps `+2` by default.

Thresholds are configurable CLI options. Performance changes are warnings rather than functional
failures because a single model call's latency is not statistically stable.

## Current limitations

- Sequential local execution only; no `--concurrency` or distributed benchmark workers.
- One execution per case; repeated trials and statistical significance are not implemented.
- No default LLM-as-a-Judge, pairwise ranking, or judge voting.
- No Recall@K, MRR, nDCG, faithfulness, or answer-relevance RAG scorers yet.
- No sandbox or per-case workspace cloning. Dataset authors must avoid unsafe mutation tasks.
- No web dashboard, leaderboard, evaluation database, or historical result service.
- Plan/Multi-Agent internal spans remain coarse, so their evaluation metrics are less detailed than
  the durable ReAct path.
