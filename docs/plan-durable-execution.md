# Durable Plan-Execute

Axiom hosts Plan-Execute in `DurableAgentRuntime`; it is an execution strategy, not a second
Runtime. Planning, replan, DAG join, and finalize remain durable Parent Run transitions. Each
tool-capable Plan task executes as an independent durable React Child Run.

```text
Parent Plan Run
├── plan.create
├── Plan Task A Child Run ─┐
├── Plan Task B Child Run ─┤ bounded parallel wave
├── plan.join              ┘
├── optional plan.replan
└── finalize
```

Parent and Child Runs share `thread_id` and `turn_id`. Each Child has its own `run_id`,
`parent_run_id`, stable `parent_step_id`, `run_kind="plan_task"`, checkpoint, messages, trace, and
React execution state.

## State model

The Parent checkpoint records `execution_strategy="plan_execute"` and a JSON-compatible Plan
schema version 2:

```text
Plan
├── id / goal / version / replan_count / status / summary
├── tasks[]
│   ├── id / description / type
│   ├── dependencies / dependents
│   ├── status / attempt / execution_state
│   ├── child_run_id / reused_from
│   ├── result / error
│   └── start_time / end_time
└── history[]: PlanVersion
```

Ready, active, waiting, and terminal sets are derived from persisted Task state and Child Run
checkpoints. Coroutines, asyncio Tasks, Futures, semaphores, locks, clients, Tool objects, and
callbacks never enter the checkpoint.

Schema version 1 checkpoints are migrated on load. Completed work is preserved. A version 1 task
that was in flight without a Child identity is reset to pending and may be replayed because the old
schema cannot identify an independently durable task attempt.

## Stable identity and durable spawn

A logical task attempt has a deterministic Child identity derived from:

```text
parent_run_id + plan_version + task_id + attempt
```

Before any Child starts, the Parent persists the Task status, attempt, and `child_run_id` with
checkpoint CAS. Starting then uses start-or-find semantics. A crash after identity persistence
therefore reconnects to the same Child instead of creating a duplicate.

## Bounded DAG scheduling

`plan.max_parallel_tasks` controls local concurrency and defaults to `2`. The environment override
is `AXIOM_PLAN_MAX_PARALLEL_TASKS`. Setting the limit to `1` preserves sequential semantics.

A pending Task is ready only when every required dependency is `COMPLETED`. Ready Tasks are chosen
in stable Plan order. Child execution can overlap, but correctness comes from persisted Child Run
status rather than in-memory Task/Future state.

Slot accounting is:

- `RUNNING` occupies a compute slot.
- `WAITING_APPROVAL` and `INTERRUPTED` do not occupy a slot.
- terminal Children do not occupy a slot.

Consequently, one Task waiting for approval does not freeze independent ready Tasks. The Parent may
remain `WAITING_CHILD` while the scheduler continues reconciling runnable work.

## Concurrent completion

Child terminal observation updates the Parent by `task_id`:

```text
load latest Parent checkpoint
→ apply idempotent Task transition
→ checkpoint CAS
→ on conflict, reload and re-apply
```

Observing an already terminal Task is a no-op, so simultaneous Child completions cannot overwrite
one another. The Parent stores only each Task's result/error and references. Child message histories
remain isolated and are never interleaved back into the Parent conversation.

## Durable boundaries and recovery

- The generated Plan is saved before scheduling.
- Stable Child identities are saved before Child start.
- Child LLM and Tool boundaries use normal React checkpoints and ToolExecution records.
- Waiting, terminal Task observation, dependency skips, join, replan, and Parent terminal state are
  checkpointed.
- A completed ToolExecution is reused after recovery rather than repeated.
- An abandoned `RUNNING` Child follows the existing Runtime recovery policy; no replacement Child is
  generated.

An LLM stream is not token-resumable. A crash before its completion checkpoint can repeat that LLM
call, while completed Tool boundaries remain durable.

## Replan barrier and completed-work reuse

Plan versions never execute speculatively together. When a v1 failure requires replan, the scheduler
stops launching new v1 Tasks and waits for the already active wave, including unresolved approvals,
to become terminal. It then checkpoints the v1 boundary, archives v1, creates and persists v2, and
only then permits v2 Children to start.

Exact normalized task-description matches can reuse completed v1 work. The v2 Task records
`reused_from` and the prior Child/result provenance; the old Child terminal history is not changed.
Semantic equivalence is not inferred.

## Permission, approval, and isolation

Every Plan Child uses the shared React path:

```text
ToolExecution lookup
→ PermissionPolicy
→ ALLOW / DENY / WAITING_APPROVAL
→ ExecutionBackend
```

Approval is bound to the Child invocation. Multiple Plan Children can wait independently, Parent
`pending_interrupts[]` identifies the exact Child, and approving one does not resume another. Hard
DENY cannot be converted into approval. Shell calls continue through `RestrictedExecutionBackend`
with environment filtering, timeout, bounded output, workspace cwd, and process cleanup.

## Observability and Evaluation

The Parent emits the existing `plan.*` lifecycle events, including scheduled, started, waiting,
completed, failed, cancelled, replan, join/waiting, and terminal transitions. Child Runs keep linked
traces with `parent_run_id`, `parent_step_id`, Plan version, and Task metadata. Runtime API
`children[]` and aggregated `pending_interrupts[]` work unchanged for Plan Parents.

Evaluation aggregates Parent metrics plus de-duplicated active/history Plan Child metrics. This
includes tokens, Tool calls, steps, latency inputs, and terminal status without copying full traces
into evaluation artifacts.

## Cancellation

Cancelling the Parent stops new scheduling, cancels all non-terminal Plan Children (including those
waiting for approval), marks pending work cancelled, and prevents replan/finalize. Cancelling one
Child does not directly cancel siblings; dependency/replan semantics determine the Parent outcome.
Repeated cancellation remains idempotent.

## Current limitations

- Scheduling is local and bounded; there is no distributed DAG scheduler or cross-process worker
  supervisor.
- Existing `RUNNING` Child recovery follows the Runtime's client-recovery semantics.
- There is no speculative overlap across Plan versions.
- LLM streams resume from a durable boundary, not an individual token.
- Schema v1 migration cannot recover a stable identity for an already in-flight legacy task.
- Completed-work reuse is exact-description based, not semantic.
- This remains a Plan strategy, not a generic workflow engine.
