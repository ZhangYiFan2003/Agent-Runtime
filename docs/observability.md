# Observability Layer v1

Axiom records an execution trace while the durable Runtime advances a Run. The first version is a
local, async-compatible SQLite implementation. It does not require OpenTelemetry, Prometheus, or an
external tracing backend.

## Data model

```text
Trace (one per Run)
└── root Run span
    ├── agent.step
    │   ├── llm.chat or tool.<name>
    │   └── checkpoint.save
    ├── interrupt
    └── resume
```

`Trace` stores the stable `trace_id`, Run/Thread/Turn identities, wall-clock lifecycle, status, and
schema version. The trace ID is deterministic from `run_id`, so process recovery reopens the same
trace instead of creating a second one.

`Span` stores a parent relationship, type (`agent`, `llm`, `tool`, `checkpoint`, or `interrupt`),
name, timestamps, status, schema version, and JSON attributes. Attributes are intentionally small
and extensible.

`Event` remains the append-only Runtime history used by SSE replay. Trace/Span data is written
directly at execution boundaries and is the source for metrics; metrics are not reconstructed by
scanning Events.

## LLM spans

Each real call to `LlmClient.chat()` creates one `llm.chat` span. Retries create separate call spans.
Attributes include:

- provider, model, and temperature;
- prompt, completion, and total tokens;
- first-token timestamp and TTFT;
- total call latency and finish reason;
- error and retry count when applicable.

TTFT starts immediately before entering the model stream and ends on the first text, thinking, or
tool-call delta. If a provider does not return usage, token fields remain zero rather than being
estimated.

## Tool spans

Tool spans reuse the durable ToolExecution identity. One stable span is created per
`invocation_id`; the ToolExecution table remains the source of tool result/idempotency state.
Attributes include tool name, invocation and call IDs, latency, attempt/retry counts, approval
requirement, result reuse, and ambiguous-execution state.

A persisted successful ToolExecution updates the same span with `reused_result=true`. A crash with
a still-`RUNNING` non-idempotent ToolExecution keeps the tool span and annotates it with
`ambiguous_execution=true` when the Runtime creates the recovery interrupt.

## Run metrics

`RunMetrics` is computed from the persisted Trace and Spans rather than stored as a second mutable
summary table. It reports:

- status and total duration;
- Agent step, LLM call, and Tool call counts;
- prompt/completion/total tokens;
- tool successes, failures, and success rate;
- successful checkpoint, interrupt, and resume counts;
- aggregate retry count.

Because the inputs are persisted, a new Runtime process can compute the same summary from the same
SQLite file.

## SQLite schema

```text
observability_schema(component, version)
traces(trace_id, run_id, thread_id, turn_id, started_at, ended_at,
       status, schema_version)
spans(span_id, trace_id, parent_span_id, span_type, name, started_at,
      ended_at, status, attributes_json, schema_version)
```

The schema has an explicit component version and per-row schema versions. SQLite uses WAL and a
busy timeout, matching the durable Runtime stores. `MemoryObservabilityStore` provides the same
async interface for tests and embedded use.

## Querying

CLI:

```text
axiom runs show <run_id> [--data-dir PATH]
axiom runs trace <run_id> [--data-dir PATH]
```

HTTP:

```text
GET /v1/runs/{run_id}/metrics
GET /v1/runs/{run_id}/trace
```

The Runtime also persists lifecycle Events such as `agent.step.started`, `llm.started`,
`llm.completed`, `llm.failed`, `interrupt.created`, `resume.started`, and `resume.completed` for SSE
replay. These Events complement spans but do not replace them.

## Recovery behavior

The root Run span and Trace remain open while a Run is interrupted. Resume reuses them and adds a
`resume` span. On recovery of a `RUNNING` checkpoint, stale in-process Agent/LLM/checkpoint spans
are marked `INTERRUPTED` with `recovered_after_crash=true`; Tool spans are preserved because their
durable ToolExecution record decides whether reuse, retry, or an ambiguity interrupt is correct.

## Current limits

- Only the default durable ReAct Runtime has step-level LLM and Tool instrumentation. Opaque custom
  engines receive a coarse `legacy.engine` span.
- Plan-Execute and Multi-Agent internal steps are not instrumented in v1.
- No distributed trace context, cross-process clock correction, sampling, retention, or export.
- No OpenTelemetry/Prometheus integration or external dashboard.
- Provider-reported tokens are recorded as supplied; the Runtime does not estimate missing usage.
