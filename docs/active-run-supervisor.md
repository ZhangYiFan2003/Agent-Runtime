# Active Run Supervisor v1

## Problem statement

Checkpoints and ToolExecution records describe durable execution state, but they cannot
identify the `asyncio.Task` currently executing a Run in this process. Before v1, a cancel
request could update durable state without stopping the coroutine, and an active HTTP turn
held a per-Run process lock that prevented the cancel control operation from entering at all.

`ActiveRunSupervisor` fills that process-local lifecycle gap. Checkpoints, ToolExecution
records, control-operation records, and Parent/Child state remain the durable source of truth.
The Supervisor is never serialized and is empty after process restart.

## Thread, loop, and Task model

The Runtime API uses `ThreadingHTTPServer`. A request thread calls `asyncio.run`, and each
background task worker also calls `asyncio.run`; concurrent threads can therefore own
different event loops. Plan and Multi-Agent schedulers create Child `asyncio.Task` objects on
their Parent's loop. There is deliberately no global event-loop reference.

```text
Process
├── HTTP thread A -> loop A -> Parent Task -> Plan Child Tasks
├── HTTP thread B -> loop B -> Run Task
└── task worker C -> loop C -> background durable Run Task
```

For the production `QueryEngine`, a claimed background task now creates a deterministic durable
Run (`run_<task_id>`) and a Runtime thread before execution, so its worker-owned coroutine is
registered and shutdown/cancel can reach it. The custom legacy engine compatibility path has no
durable internal Run and therefore has no Run handle.

## ExecutionHandle and registry

Each active durable Run registers an `ExecutionHandle` containing:

- `run_id`, `thread_id`, `turn_id`, `parent_run_id`, and `run_kind`;
- the owner event loop, `asyncio.Task`, and owner thread ID;
- registration time and a process-local cancellation-requested flag;
- small metadata such as `execution_strategy`.

It does not contain checkpoints, messages, prompts, clients, Tool functions, or registries.
An `RLock`-protected registry provides register, unregister, lookup, active listing, single
and batch cancellation, and bounded drain. A second live registration for the same Run is
an explicit conflict. A completed stale handle may be removed and replaced. Unregister is
idempotent and accepts an expected handle so an old `finally` block cannot remove a newer
owner.

## Registration lifecycle

`DurableAgentRuntime.start` first creates the initial durable checkpoint, then registers on
the running loop before strategy execution. `resume` loads the checkpoint, registers before
advancement, and revalidates the transition under the existing per-Run async operation lock.
Both paths unregister in `finally`, covering completion, failure, cancellation, approval or
interrupt return, unexpected exceptions, and CAS cancellation convergence.

`WAITING_APPROVAL`, `INTERRUPTED`, and `WAITING_CHILD` describe durable waiting states. Once
their coroutine returns they have no handle. A `RUNNING` checkpoint left by a crashed process
also has no handle. Durable state and process-local activity are intentionally independent.

## Cross-thread cancellation

A requester never calls a foreign Task's `cancel()` directly. The Supervisor marks the
handle and schedules cancellation through its owner loop:

```text
cancel thread B
  -> handle.event_loop.call_soon_threadsafe(...)
  -> Task.cancel() on loop A
  -> cancellation cleanup in Run/tool/backend coroutine
```

Missing handles, repeated requests, completed Tasks, and closed loops return structured,
best-effort results. Batch cancellation classifies Run IDs as `signalled`, `not_active`,
`already_done`, or `loop_unavailable`; one failure does not stop the remaining requests.

## Durable cancellation ordering and CAS

The control plane performs cancellation in this order:

1. validate the durable transition and idempotency record;
2. let the strategy mark Parent Plan/Assignment state cancelled in memory;
3. append one `CANCELLED` Parent checkpoint using sequence CAS;
4. cancel each selected non-terminal Child durably;
5. signal active Child Tasks and then the active Parent Task;
6. let coroutine, ToolExecution, and execution-backend cleanup finish;
7. unregister each handle in `finally`.

If the active coroutine attempts a stale completion after step 3, checkpoint CAS rejects it.
The supervised wrapper recognizes that the winning durable state is `CANCELLED` and converges
without exposing an internal conflict or writing `COMPLETED`. A failure to signal an absent or
closed-loop Task never rolls back the durable cancellation.

Direct, non-Supervisor `task.cancel()` retains the previous crash-simulation behavior and
re-raises `CancelledError`. A Supervisor-requested cancellation intentionally converges to and
returns the durable terminal checkpoint. `CancelledError` is not converted to `FAILED`.

## Parent and Child cancellation

Plan and Multi-Agent strategies retain all orchestration semantics. They decide which Child
Runs are non-terminal, persist the Parent cancellation, and invoke Child durable cancellation.
Every Child runtime shares the same Supervisor, so each active Child receives a signal through
its own handle. The Supervisor does not understand Plan dependencies, assignments, review,
retry, synthesis, or DAG scheduling.

## Subprocess cleanup

Supervisor cancellation reaches the existing ToolExecutor and
`RestrictedExecutionBackend`. The backend catches `CancelledError`, shields process-tree
termination and output capture cleanup, and re-raises. ToolExecution and Tool spans are marked
cancelled before the durable Run converges. Controlled tests verify that a spawned child process
does not survive this path.

## Graceful shutdown

Runtime server shutdown sets the stop flag and stops HTTP intake, snapshots active handles,
persists cancellation for each non-terminal durable Run within the configured internal timeout,
signals any remaining handles, and performs a bounded drain. Only then does it close the HTTP
server and join the server/background worker threads. Stores remain available throughout Run
cancellation cleanup. Idle shutdown returns immediately.

Cancelling a running background task also cancels its deterministic durable Run when that Run
already exists. A canceled Run is not overwritten by the worker's normal task completion update.

## Inspection

Authenticated `GET /v1/runtime/active-runs` returns only safe process-local fields:
`run_id`, `thread_id`, `turn_id`, `parent_run_id`, `run_kind`, `execution_strategy`,
`registered_at`, `owner_thread_id`, `task_done`, and `cancellation_requested`. It never exposes
loop or Task representations, prompts, messages, Tool arguments, environment data, or secrets.

## Crash boundary and limitations

- The Supervisor is process-local and disappears on crash or restart.
- There is no automatic startup recovery scanner or abandoned-Run takeover.
- There is no cross-process cancellation, distributed ownership, lease, heartbeat, fencing
  token, worker process, distributed queue, or distributed scheduler.
- A `RUNNING` checkpoint after crash can have no active handle.
- `WAITING_APPROVAL`, `INTERRUPTED`, and `WAITING_CHILD` normally have no active handle.
- There is no token-level LLM stream resume.
- Ambiguous external side effects remain governed by durable ToolExecution safety rules and
  cannot be assumed safe to replay automatically.
