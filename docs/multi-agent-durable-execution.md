# Durable Multi-Agent Execution

Axiom runs multi-agent orchestration as a `RuntimeExecutionStrategy` inside
`DurableAgentRuntime`. The orchestration is a Parent Run. Each tool-capable Worker is a durable
React Child Run in the same Thread and Turn; Planner and Reviewer calls remain durable Parent
steps, and synthesis remains the existing deterministic final summary.

## Run hierarchy

```text
Thread
└── Turn
    └── Parent Run (multi_agent)
        ├── planning step
        ├── Child Run A (worker, react)
        ├── Child Run B (worker, react)
        ├── review step
        ├── Child Run (worker retry or next assignment)
        └── synthesis step
```

A Child checkpoint stores `parent_run_id`, `parent_step_id`, and `run_kind=worker`. It shares the
Parent's `thread_id` and `turn_id`, but has an independent `run_id`, checkpoint sequence,
ToolExecution records, status, and trace.

## Persisted strategy state

The Parent checkpoint stores a schema-versioned JSON `MultiAgentState` containing:

- orchestration goal and status;
- normalized Worker assignments and dependency edges;
- durable active assignment identities and observed maximum parallelism;
- each assignment's dependencies, status, attempt, stable Child Run ID, result, and error;
- persisted Reviewer output, decision, issues, and review count;
- persisted synthesis result.

The v2 state reader migrates v1's singular `current_assignment_id` into the plural active model.
It never stores Agent objects, Tool functions, LLM clients, coroutines, tasks, futures, callbacks,
or locks. Those dependencies are reconstructed when a Runtime resumes.

## Stable Child identity

Before a Worker starts, the Parent derives and checkpoints its Child Run ID from:

```text
parent_run_id + assignment_id + attempt
```

Child creation is therefore idempotent. A crash after assignment persistence but before Child
creation resumes with the same ID. A Reviewer-requested Worker retry increments `attempt` and
creates a new Child Run rather than rewriting earlier Child history.

## Local bounded scheduler

`MultiAgentExecutionStrategy` composes a `LocalChildRunScheduler`. The scheduler discovers every
ready assignment in stable plan order, persists all deterministic Child identities, then starts at
most `multi_agent.max_parallel_workers` Child Runs concurrently. The default is `2`; setting it to
`1` preserves the previous sequential behavior.

Scheduler state is derived from Parent assignments and Child checkpoints:

- `PENDING` with all dependencies `COMPLETED` is ready;
- a Child currently executing is active and occupies a local slot;
- `WAITING_APPROVAL` and `INTERRUPTED` Children are waiting and do not occupy a compute slot;
- completed, failed, skipped, and cancelled assignments are terminal.

In-memory Tasks only optimize current-process execution. Child checkpoint status is the source of
truth after restart. The scheduler uses `FIRST_COMPLETED` replenishment, so a waiting Child releases
its slot and an independent ready Worker can start while another Child continues running.

## Durable boundaries

The Parent checkpoints after orchestration initialization, planning, assignment identity,
pre-Child batch start, Child terminal observation, review, reassignment, synthesis, cancellation, and
completion. Child React Runs retain the existing LLM, Tool, approval, retry, and ToolExecution
boundaries.

Each ready Child ID is persisted before any Child in that batch starts. If a Child completes but the
Parent crashes before recording its result, the Parent reloads the existing Child checkpoint and
copies its terminal output without rerunning the Worker. If a Child
Tool succeeded before the Child state transition was checkpointed, normal ToolExecution
deduplication reuses the recorded result.

## Approval and resume

```text
Parent WAITING_CHILD
        ↓
Child WAITING_APPROVAL
        ↓ optional process restart
approve/reject Child invocation
        ↓
resume Child
        ↓
resume/observe Parent
```

The Parent deliberately does not claim `WAITING_APPROVAL`; pending invocations belong to exact
Children. Multiple Children can wait simultaneously. The compatibility facade and Runtime API
resume a specifically identified Child and then reconcile the Parent. A waiting approval does not
consume a scheduler compute slot or block unrelated ready assignments. Approval remains scoped to
the Child invocation ID, so it does not create a global grant or an approval loop.

## Permission and restricted execution

Every Child is constructed with the Parent Runtime's Tool registry, Runtime store, retry policy,
PermissionPolicy, and ExecutionBackend. Worker tools therefore use the existing path:

```text
ToolExecution → PermissionPolicy → RestrictedExecutionBackend → Tool handler
```

Multi-agent orchestration does not provide an alternate Tool execution path.

## Cancellation and failure

Cancelling a Parent stops new assignment scheduling, marks pending assignments cancelled, and
requests cancellation of every non-terminal Child.
Cancelling or failing a Child does not automatically cancel the Parent: the strategy records that
terminal status and produces the same partial-result summary semantics as the previous
orchestrator. Reviewer rejection creates a new assignment attempt up to the configured limit.
Tool retry remains separate from Worker-attempt retry.

## Observability and evaluation

Parent events use `multi_agent.*`, `worker.*`, `review.*`, and `synthesis.*`; events from parallel
Workers may interleave and retain `run_id`, `parent_run_id`, `parent_step_id`, and `assignment_id`.
Each Child has its own
trace and a root span linked to the Parent Worker span through `parent_step_id`; root attributes also
include `parent_run_id` and `run_kind`. Evaluation follows Child trace links to include Worker Tool
usage, tokens, and steps in the case result.

## Limitations

- Scheduling is local and bounded; there is no distributed worker lease or queue.
- Child traces are linked traces, not a single cross-Run trace record.
- Active cancellation cannot be guaranteed across processes without a worker supervisor.
- Reviewer calls and Parent state transitions remain serialized even while Child execution is parallel.
- Planner and Reviewer LLM calls may repeat if the process dies before their result checkpoint.
- There is no distributed Worker queue, message broker, or shared mutable blackboard.
- Multi-agent state migrates v1 to v2 in code but has no general migration framework.
