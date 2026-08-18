# Durable Plan-Execute

Axiom implements Plan-Execute as an execution strategy hosted by the same
`DurableAgentRuntime` used by ReAct. It is not a second Runtime implementation.

```text
DurableAgentRuntime
├── Run lifecycle and Run lock
├── CheckpointStore
├── ToolExecution and retry
├── PermissionPolicy and durable approval
├── ExecutionBackend
├── Trace / Span / Event
└── RuntimeExecutionStrategy
    ├── ReactExecutionStrategy
    └── PlanExecuteStrategy
```

## Lifecycle

```text
Run started
↓
plan.create
↓ checkpoint: plan.created
plan.step.started
↓ LLM / Tool loop
↓ checkpoint after every LLM and Tool boundary
plan.step.completed or plan.step.failed
↓ checkpoint
optional plan.replan
↓ checkpoint
plan.completed or plan.failed
↓ terminal Run checkpoint
```

Planning that crashes before `plan.created` is persisted may be repeated. Once the Plan checkpoint
exists, recovery loads it and does not call the Planner again.

## State model

The generic Checkpoint records `execution_strategy="plan_execute"` and a JSON-compatible
`strategy_state`. The active Plan contains:

```text
schema_version
id
goal
version
replan_count
status
summary
tasks[]
history[]
```

Each task records:

```text
id
description
type
dependencies / dependents
status: PENDING | RUNNING | COMPLETED | FAILED | SKIPPED
attempt
result
error
start_time / end_time
```

Plan history contains explicit `PlanVersion` snapshots. Coroutines, tasks, locks, clients, Tool
functions, callbacks, and other live Python objects never enter the checkpoint.

## Durable boundaries and recovery

- **Plan creation:** the generated Plan is saved before any Plan task starts.
- **Task start:** status, attempt, current task identity, task message offset, and turn offset are
  saved before the worker LLM loop starts.
- **LLM completion:** messages, token usage, pending Tool calls, and task completion marker are saved.
- **Tool completion:** the normal ToolExecution record is saved before the Tool result is applied to
  the Plan checkpoint.
- **Task completion/failure:** output or error and the active Plan snapshot are saved.
- **Replan:** the old Plan becomes a history revision and the new active version is saved before it
  executes.
- **Interrupt:** `WAITING_APPROVAL`, invocation identity, and Plan worker state share one checkpoint.

Completed tasks are not selected again after recovery. If a ToolExecution is already `SUCCEEDED`
but the Plan checkpoint still contains the pending call, the normal durable Tool path reuses the
stored result and completes the state transition without repeating the Tool.

An LLM stream itself is not resumable. A crash before an LLM completion checkpoint repeats that LLM
call; no completed Tool boundary is lost by doing so.

## Stable Tool invocation identity

Plan Tool call IDs are scoped by active Plan version, task ID, and durable Agent turn:

```text
plan_v{version}:{task_id}:turn_{turn}:{provider_call_id}
```

The normal Runtime then prefixes this with `run_id` for `invocation_id`. This prevents providers that
reuse Tool call IDs across Plan tasks from colliding in the ToolExecution store. Pending Tool calls
are checkpointed, so restart uses the same identity.

## Replan semantics

Plan v1 is not overwritten. On replan:

1. v1 is appended to `history` with the failure reason and full task states;
2. the new Plan becomes v2;
3. `replan_count` increments;
4. exactly matching completed task descriptions are reused rather than executed again;
5. earlier completed outputs remain available as context to v2 tasks.

v1 uses one automatic replan by default. Tool retry remains the shared Runtime Tool retry policy;
Plan task attempts and Tool attempts are separate counters.

## Permission and execution isolation

Plan workers call the same internal durable LLM/Tool transitions as ReAct:

```text
Plan Step
↓
ToolExecution lookup
↓
PermissionPolicy
↓
ALLOW / DENY / WAITING_APPROVAL
↓
ExecutionBackend
```

Approval is bound to the same stable invocation ID. A restart while waiting resumes the current
Plan task. Shell calls continue through `RestrictedExecutionBackend`, including environment
filtering, timeout, bounded output, workspace cwd, and process cleanup.

## Observability

Plan execution uses the existing Run trace and Event pipeline:

```text
plan.created
plan.step.started
plan.step.completed
plan.step.failed
plan.replanned
plan.completed
plan.failed
plan.cancelled
```

`plan.create`, `plan.replan`, and `plan.step` are Agent spans. Planner LLM spans are children of the
planning/replan span. Worker `agent.step` spans are children of the Plan step, with LLM, Tool,
Permission, checkpoint, and interrupt descendants below them.

## Cancellation

A persisted cancelled Run does not schedule another Plan task. Runtime cancellation also marks the
active Plan `CANCELLED`. Cancellation of an active asyncio Tool task continues to use ExecutionBackend
process cleanup. The threaded HTTP cancellation endpoint still cannot inject cancellation into an
already running coroutine in another thread.

## Current limitations

- Plan tasks execute sequentially at durable boundaries; parallel DAG scheduling is deferred.
- There is no distributed scheduler or distributed Run lock.
- LLM streams resume from their previous durable boundary, not from an individual token.
- Plan schema version 1 is strict and has no migration framework yet.
- Automatic replan uses exact normalized task descriptions to recognize completed work; semantic
  equivalence is not inferred.
- Multi-Agent orchestration has not yet converged on this strategy contract.
