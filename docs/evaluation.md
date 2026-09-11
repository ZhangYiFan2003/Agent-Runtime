# Agent Evaluation Feedback Loop v2

Axiom Evaluation extends the v1 fixed-dataset runner with reviewed badcases, repeated trials,
attribution fingerprints, and a CI-compatible regression gate. It still runs through the real
durable Agent Runtime; it does not call `llm.chat()` directly or require a Runtime HTTP server.

```text
Runtime / Evaluation
        ↓
Failure
        ↓
BadCase Collector
        ↓
Failure Taxonomy
        ↓
Human Review
        ↓
Regression Dataset
        ↓
Repeated Trials
        ↓
Baseline vs Candidate
        ↓
Regression Gate
```

Three mechanisms remain separate. Runtime self-correction returns a tool/model error to the LLM
inside the current Run so it can recover. Badcase collection happens after a completed or terminal
execution and persists compact review evidence; it never appends data to the current conversation.
Regression evaluation runs explicitly promoted, stable `EvaluationCase` records in future Runs.

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

Every trial receives independent Run, Turn, Thread, and Trace identity. No Checkpoint, message
history, tool execution, or strategy state is shared across trials.

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
code. A case with no scorer receives the safe default `RunStatusScorer(COMPLETED)`. If it also has
a non-empty `completion_contract`, the default scoring additionally requires
`CompletionVerificationScorer(VERIFIED)`.

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
- `completion_verification`: accepted verification states, defaulting to `VERIFIED`.
- `metric_threshold`: `max_steps`, `max_tokens`, `max_latency_ms`, and `max_tool_calls`.

Every scorer has `required` (default `true`) and a numeric score reserved for later weighting. A case
passes when all required scorers pass. Optional scorer failures remain visible without failing the
case. Evaluation does not require an exact tool-call sequence because model execution is naturally
nondeterministic.

The public `Scorer` protocol and injectable scorer factory allow future RAG metrics or an optional
LLM judge without making them core dependencies.

## Completion contracts

An `EvaluationCase` may carry a serializable `completion_contract`. Its checks run inside the real
durable Runtime when the strategy proposes completion. Supported deterministic checks are Run
status, required/forbidden Tool names, successful persisted Tool execution (optionally matching a
bounded expected result substring), output contains/exact match, workspace artifact existence, and
Plan task/Child Run completion.

Evaluation results preserve the distinction between merely terminal and objectively verified with
`completion_verified`, `verification_status`, `verification_attempts`, and
`failed_verification_checks`. Verification failures can be classified as
`completion_verification_failed`. A Run with no contract remains backward compatible and reports
`NOT_APPLICABLE`; this is not treated as objective verification.

The verifier does not execute commands outside Runtime controls. If a contract requires command or
test success, the Agent must invoke the normal Tool path and the verifier checks its durable
`ToolExecutionRecord`. ReAct allows one normal-budget corrective continuation by default; repeated
failure terminates with `COMPLETION_NOT_VERIFIED`.

## Context retention evaluation

The focused offline dataset
[`benchmarks/datasets/context-retention-v1.json`](../benchmarks/datasets/context-retention-v1.json)
contains 10 designed long-context cases: an old objective, multiple constraints, completed versus
open work, earlier failure evidence, an oversized Tool result, protocol pairing, a previous summary
plus new history, removable noise, pinned recent state, and a pending Tool call.

`ContextRetentionEvaluator` runs the existing `ContextManager` directly and reports, per case:

- input and output estimated tokens plus `after / before` compression ratio;
- retained/required counts and `required_state_retention_rate` across objective, constraints, open
  tasks, decisions, artifact references, critical evidence, and pending protocol state;
- Tool call/result protocol integrity;
- optional deterministic full-context versus compressed-context task outcome.

`compare_context_retention()` treats any per-case retention decrease, protocol regression, or
configured deterministic outcome regression as hard. A worse compression ratio is a warning by
default. Better compression never cancels a retention failure. Irrelevant `must_drop` noise is
reported but does not affect required-state score. Because `ContextManager` does not expose a full
stage-by-stage provenance graph, an unavailable exact cause is reported as
`lost_after_compaction`; the benchmark does not add heavy instrumentation to production context.

This evidence proves declared state retention and protocol behavior for a fixed offline set. It
does not prove universal semantic equivalence or that every open-ended Agent remains equally
capable after summarization. No LLM judge is used.

Current deterministic fixture evidence uses a 1,200-token model window, 200-token output reserve,
35% high watermark, 20% target, two recent messages, and a 1,000-token hard limit. The 10 cases
retained 22/22 declared items (`1.0000` retention), preserved protocol integrity in 10/10 cases,
and produced a mean `after / before` ratio of `0.5348` (about 46.5% estimated-token removal). These
numbers describe this fixed synthetic dataset and policy only; they are not a universal Agent
quality claim.

## Running an evaluation

```bash
axiom eval run benchmarks/datasets/agent-core.json \
  --cwd . \
  --data-dir .tmp/eval-runtime \
  --output .tmp/agent-core-result.json

axiom eval run benchmarks/datasets/agent-core.json --trials 5 --verbose
```

Cases and trials run sequentially to reduce provider rate-limit interference and SQLite contention.
`--trials` defaults to `1`, preserving v1 behavior. Runtime checkpoints and traces are written to
`<data-dir>/runtime.db`. The report contains Run/Trace references and derived summaries, not copied
Traces.

## Result format

```json
{
  "schema_version": 2,
  "dataset": "agent-core",
  "dataset_version": "1.0.0",
  "cases_total": 12,
  "cases_passed": 10,
  "cases_failed": 2,
  "pass_rate": 0.8333,
  "trials_per_case": 5,
  "trial_count": 60,
  "trial_success_rate": 0.85,
  "avg_latency_ms": 4800.0,
  "avg_tokens": 6240.0,
  "avg_steps": 5.4,
  "case_aggregates": [],
  "attribution": {},
  "results": []
}
```

Each individual trial result remains available and includes `trial_index`, `run_id`, `thread_id`,
`turn_id`, `trace_id`, status, final assistant output, score explanations, token counts, duration,
tool names, steps, and bounded runtime error metadata. Per-case aggregates include trial count,
success/failure count, `trial_success_rate`, and average/minimum/maximum tokens, steps, and latency.
This is deliberately not called Pass@k because it is a direct observed success rate, not the formal
Pass@k estimator.

Result schema v2 readers accept existing schema v1 reports. Dataset schema remains v1.

## Attribution and fingerprints

Reports record the package/build runtime version, model provider/name, a whitelisted model
configuration plus its SHA-256 fingerprint, and SHA-256 fingerprints for the assembled system
prompt, tool definitions, policy configuration, Context policy, and dataset version. Stable sorted
JSON is hashed. API keys, credentials, base URLs, environment variables, Tool arguments, and raw
tool payloads are neither included nor hashed. The installed package version is the explicit
runtime version; Git is not required at Runtime.

## Badcases and review

```bash
axiom eval badcase collect --result candidate.json
axiom eval badcase collect --run-id run_123 --data-dir .tmp/eval-runtime
axiom eval badcase list
axiom eval badcase show badcase_abc
axiom eval badcase approve badcase_abc --note "confirmed runtime defect"
axiom eval badcase ignore badcase_xyz --note "provider outage"
axiom eval badcase promote badcase_abc --dataset benchmarks/datasets/regression.json
```

The local JSON store defaults to `~/.axiom/runtime/evaluation-badcases.json`; `--store` overrides it.
Writes use atomic replacement and deterministic identities, so processing one result repeatedly is
idempotent and restart-safe. A record keeps stable Run/Thread/Turn/Trace references plus compact,
bounded derived evidence. It does not copy a Trace and does not persist Tool arguments.

Deterministic classification supports multiple labels: `run_failed`, `timeout`, `wrong_answer`,
`wrong_tool`, `forbidden_tool`, `tool_failure`, `tool_argument_failure`,
`step_budget_exceeded`, `token_budget_exceeded`, `context_budget_exceeded`, `policy_denied`,
`recovery_failure`, `no_progress`, and `unknown_failure`. Evidence comes from failed scorers,
terminal status,
structured Checkpoint errors, ToolExecution state, and safe Trace/Span attributes. No LLM
classifier is required.

Review states are `PENDING`, `APPROVED`, `IGNORED`, and `PROMOTED`. Collection starts at `PENDING`.
Human review may approve or ignore it; promoted records are terminal. Only `APPROVED` records may
be promoted. Promotion uses a stable case ID, preserves `source_badcase_id`, appends without
overwriting unrelated cases, persists the dataset before marking the record promoted, and is
idempotent. If deterministic expected/scorer information is absent, promotion fails with
`BADCASE_PROMOTION_INSUFFICIENT_EXPECTATION`; explicit `--expected-json` or `--scorers-json`
overrides can supply reviewed evidence.

## Regression comparison

```bash
axiom eval compare baseline.json candidate.json
```

The comparison separates:

- hard functional regressions: a shared case changed from 100% success to 0% success;
- stochastic quality changes: intermediate `trial_success_rate` increased or decreased;
- improvements: a shared case changed from FAIL to PASS;
- aggregate changes: strict case pass rate, trial success rate, average tokens, latency, and steps;
- performance warnings: tokens `+20%`, latency `+30%`, or steps `+2` by default.

Thresholds are configurable CLI options. Performance changes are warnings rather than functional
failures because a single model call's latency is not statistically stable.

## Regression gate

```bash
axiom eval gate baseline.json candidate.json \
  --max-success-rate-drop 0.10 \
  --max-token-increase-ratio 0.20 \
  --max-step-increase 2
```

The gate exits `1` for a hard PASS-to-FAIL regression, a newly introduced required case failure,
or a per-case trial success-rate drop beyond the configured tolerance. Token, step, and latency
increases fail only when their corresponding hard threshold is explicitly configured. Default
comparison thresholds remain warnings; latency is warning-only by default. Improvements are
reported but never hide failures. Invalid input exits `2`.

## Interview-oriented explanation

1. **How do you evaluate a nondeterministic Agent?** Run independent trials, retain every trial,
   and compare per-case observed success rates plus resource averages instead of treating one
   stochastic sample as ground truth.
2. **What differs between an error returned to the LLM and a Badcase?** The former is in-Run
   self-correction context. The latter is an after-the-fact persisted review record and is never
   inserted into the active conversation.
3. **How does a failure become a permanent regression test?** Collect it, classify it, have a human
   approve it, then explicitly promote it into a versioned dataset with Badcase provenance.
4. **Why require human review?** Failures can be bad tests, provider outages, environment problems,
   expected nondeterminism, or real defects. Promotion needs a verified deterministic oracle.
5. **How do you locate the change that caused a regression?** Compare runtime/build, model and
   public model-config, prompt, tool-schema, policy, Context-policy, and dataset fingerprints while
   resolving the stored Run/Trace references for evidence.
6. **What fails the gate?** Hard functional regressions, new required failures, excessive trial
   success-rate drops, and explicitly configured token/step/latency limits. Default performance
   drift is a warning.

## Cost and Cost per Success

Durable evaluation trials read authoritative aggregate Run-ledger usage, including Child Runs.
Every trial records `cost_usd` plus `cost_known`. Reports add `total_cost_usd`, `avg_cost_usd`,
`successful_trial_cost_usd`, `cost_known_trial_count`, and `cost_per_success` at suite and per-case
levels. Unknown prices remain `null`; they are never converted to zero.

Cost per Success is:

```text
total model cost across all trials
----------------------------------
number of successful trials
```

It is emitted only when every trial has known pricing and at least one trial succeeds. This is an
efficiency metric, not a correctness metric: lower tokens can still produce worse Cost per Success
when success rate falls. Comparison displays Cost per Success drift and warns above 20% by default.
It does not fail CI unless `axiom eval gate` explicitly receives
`--max-cost-per-success-increase-ratio`; all existing functional/token/step/latency defaults remain
unchanged.

Evaluation attribution now also fingerprints the public Run-budget policy and configured pricing
table. Fingerprints use deterministic JSON SHA-256 and exclude API keys, credentials, base URLs,
raw prompts/tool arguments, and other secrets.

Context Budget and Run Budget remain independent. Context answers whether one request fits;
Run Budget answers whether the entire durable Run tree may consume more resources. Context
compaction does not mutate accounting, while actual provider usage after compaction does.

## No-progress failures

The durable Runtime may terminate a Run as `NO_PROGRESS` after deterministic identical-action,
repeated-error, short-cycle, or stable-state detection exhausts bounded recovery. Evaluation keeps
the structured error and detector metadata, so offline cases can assert this behavior. Badcase
classification adds `no_progress` alongside `run_failed` and any applicable Tool failure, storing
the detector type rather than raw Tool arguments.

This remains three separate mechanisms:

```text
Runtime recovery signal -> current Run only
NO_PROGRESS terminal Run -> optional Badcase collection
approved Badcase         -> explicit regression-case promotion
```

Every repeated trial still has an independent Run and detector. A no-progress trial remains
inspectable and participates in existing functional and success-rate gates. Detection adds no LLM
judge, automatic prompt rewriting, or automatic dataset promotion.

## Current limitations

- Sequential local execution only; no `--concurrency` or distributed benchmark workers.
- Repeated trials are sequential and use explicit thresholds; no statistical significance test is
  implemented.
- No default LLM-as-a-Judge, pairwise ranking, or judge voting.
- No Recall@K, MRR, nDCG, faithfulness, or answer-relevance RAG scorers yet.
- No sandbox or per-case workspace cloning. Dataset authors must avoid unsafe mutation tasks.
- No web dashboard, leaderboard, server database, automatic monitoring, or automatic promotion.
- Plan/Multi-Agent internal spans remain coarse, so their evaluation metrics are less detailed than
  the durable ReAct path.
- Hidden transport-layer retries below the observable LLM client cannot be counted separately;
  Runtime-owned retries are counted. Pricing is local configuration and is never fetched live.

## Evaluation reuse for RL rollout

Evaluation and RL rollout share execution infrastructure but not purpose or lifecycle. Evaluation
compares quality and gates regressions. `RLRolloutRunner` reuses the same reviewed
`EvaluationDataset`, repeated trials, durable Run evidence, completion verifier, and scorers to
collect policy interaction trajectories with reward. It then exports validated JSONL rather than
an `EvaluationSuiteResult` report.

Badcases are never converted automatically. Training splits and reward configuration are explicit
provenance, and a held-out split remains marked in every episode. See
[Agentic RL Bridge v1](agentic-rl.md).

## Evidence benchmarks

The maintained evidence pack lives under `benchmarks/`: repository-grounded
Code Intelligence retrieval, 40 read-only Agent Runtime fixed tasks, and
deterministic durable recovery fault injection.

Offline hash embeddings are pipeline fixtures, not production semantic models.
Fake LLM results are execution-correctness evidence, not Task Success Rate.
Real-provider runs must identify the exact provider/model and may vary. Recovery
metrics come from deterministic crash hooks and separate recovered outcomes
from expected-safe ambiguous outcomes.
