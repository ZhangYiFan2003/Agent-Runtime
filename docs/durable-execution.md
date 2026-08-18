# Durable Execution

Axiom checkpoints ReAct execution state after durable boundaries and can resume interrupted
runs after process restart. The first implementation is intentionally single-machine and uses
either an in-memory store or SQLite; it does not require an external service.

## State model

```text
Thread
└── Turn
    └── Run
        ├── Checkpoint snapshots
        ├── append-only Runtime events
        └── ToolExecution records
```

- A **Thread** is the long-lived conversation identity.
- A **Turn** is one user input and its eventual assistant response.
- A **Run** is the resumable execution of that Turn. The first version creates one Run per Turn;
  the separate identity leaves room for a later explicit re-run without changing Turn semantics.
- An **Event** is append-only execution history for replay and observability. It is not the source
  of truth for recovery.
- A **Checkpoint** is a versioned JSON snapshot containing the minimum state required to resume.

Run status is one of `RUNNING`, `INTERRUPTED`, `WAITING_APPROVAL`, `COMPLETED`, `FAILED`, or
`CANCELLED`.

## Persistence and serialization

`MemoryCheckpointStore` is useful for tests and embedded use. `SQLiteCheckpointStore` stores
append-only checkpoint versions and the latest ToolExecution state in local SQLite tables:

```text
checkpoints(run_id, sequence, schema_version, thread_id, turn_id, status, state_json, created_at)
tool_executions(invocation_id, run_id, tool_call_id, tool_name, arguments_hash,
                status, attempt, result, error, started_at, completed_at, updated_at)
```

Checkpoint JSON contains messages, pending tool calls, current tool index, agent turn and step
indices, token totals, output text, run status, interrupt data, decisions, and bounded error
metadata. It does not contain clients, tool functions, file handles, coroutines, tasks, callbacks,
or locks. Those runtime objects are reconstructed after restart through dependency injection.

Each save checks the expected sequence and appends the next sequence. A stale worker receives a
`CheckpointConflictError`. The Runtime API also uses a per-run process lock, so two resume requests
cannot concurrently advance the same Run in one server process.

## Durable boundaries and recovery

The durable ReAct loop writes a checkpoint:

1. when a Run is created;
2. after a complete LLM response has been converted into an assistant message and pending tool
   calls;
3. after each tool result has been added to messages;
4. before returning an interrupt;
5. on terminal completion, failure, or cancellation.

If the process stops after a completed boundary, `resume()` loads the latest sequence and continues
from the pending step instead of re-running the Turn. A process stop during an LLM stream can only
restart that in-flight LLM step because provider streams are not resumable.

The default retry policy is deliberately small: maximum attempts, a retryable-error predicate,
and fixed backoff. Automatic tool retry is limited to read-only tools or tools that explicitly
declare an idempotency-key parameter. Exhausted tool errors are persisted and returned to the model
as tool results; unrecoverable Runtime/LLM failures set the Run to `FAILED`.

## Interrupt, approval, resume, and cancel

A tool with `requires_approval=True` checkpoints the pending call and moves the Run to
`WAITING_APPROVAL` before the tool executes. Approval and rejection survive process restart.

Runtime API endpoints are:

```text
GET  /v1/runs/{run_id}
POST /v1/runs/{run_id}/resume       {"decision": "approve" | "reject"}
POST /v1/runs/{run_id}/interrupt    {"reason": "..."}
POST /v1/runs/{run_id}/cancel
```

Rejecting a pending tool writes a rejected tool result into the message sequence so the Agent can
continue or terminate normally. A cancelled Run is terminal and ordinary resume is rejected.
Manual interrupts are observed at durable boundaries; they do not forcibly abort an arbitrary
Python function in the middle of execution.

Persisted Runtime events include `run.started`, `step.started`, `step.completed`, `tool.started`,
`tool.completed`, `run.interrupted`, `run.resumed`, `run.completed`, `run.failed`, and
`run.cancelled`. Existing `tool_call` and `tool_result` events remain available for compatibility.

## Tool idempotency and the exactly-once boundary

Each tool call gets a stable `invocation_id` derived from the Run and model `tool_call_id`. Its
canonical arguments are protected by `arguments_hash`. Before execution, the Runtime writes a
`PENDING` and then `RUNNING` ToolExecution record. A persisted `SUCCEEDED` record is reused during
recovery rather than invoking the tool again.

Runtime-level deduplication does not magically provide exactly-once semantics for arbitrary
external side effects.

Crash behavior is explicit:

- Before a record exists, recovery creates it and executes normally.
- With a record persisted but before tool execution, recovery can execute the pending invocation.
- If an external side effect completed but `SUCCEEDED` was not persisted, the Runtime sees a
  `RUNNING` record. For a non-read-only tool without tool-provided idempotency, it interrupts with
  `ambiguous_tool_execution` and requires an explicit recovery decision.
- Once `SUCCEEDED` is persisted, recovery reuses the stored result.

Tools that support external idempotency can set `Tool.idempotency_key_parameter`. Axiom injects the
stable `invocation_id` into that argument and also exposes it as `ToolContext.invocation_id`. The
external system must enforce the key for end-to-end deduplication.

## Scope limits

The current durable loop covers the default Runtime API ReAct `QueryEngine`. Custom engine
factories keep their legacy execution path and receive terminal Run records, but their internal
steps cannot be recovered unless they adopt the durable interface. Plan-Execute and multi-agent
internal DAG/worker state remain in-memory in this release. Distributed scheduling, distributed
locks, automatic recovery scanning, checkpoint compaction, and exactly-once external effects are
not implemented.
