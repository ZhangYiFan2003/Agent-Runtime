# Runtime Control Plane API

Axiom's `/v1` Runtime API is the external control plane for durable Runs. Clients
operate on stable Run identities and state transitions; checkpoint JSON, Python tasks,
locks, and execution-strategy internals are not part of the HTTP contract.

## Thread, Turn, and Run hierarchy

```text
Thread
└── Turn
    └── Parent Run
        ├── Child Run A
        └── Child Run B
```

Child Runs share the parent's `thread_id` and `turn_id`, but have their own `run_id`,
checkpoint, status, interrupt, tools, and trace. `parent_run_id` and `parent_step_id`
provide durable lineage. Children are always represented as arrays; the API does not
assume that a parent has only one active child.

## Run queries

```http
GET /v1/runs
GET /v1/runs/{run_id}
GET /v1/runs/{run_id}/children
GET /v1/runs/{run_id}/interrupts
GET /v1/runs/{run_id}/trace
GET /v1/runs/{run_id}/metrics
GET /v1/runtime/active-runs
```

The public Run representation contains identity, strategy, status, parent linkage,
timestamps, bounded output/error summaries, waiting and recovery details, allowed
operations, child counts, active child IDs, and pending interrupts. Full checkpoint
state is intentionally not returned.

`GET /children` is stably ordered by creation time and Run ID. A Run without children
returns an empty array. Each child includes its assignment ID, worker role, attempt,
status, interrupt summary, and parent linkage when available.

`pending_interrupts` aggregates the target Run and its direct children. Every entry
identifies the exact `run_id` and `invocation_id`; Tool arguments are omitted. Clients
approve or reject the specific child Run rather than asking the parent to guess which
child is intended.

`GET /v1/runtime/active-runs` is an authenticated, process-local diagnostic view. It
returns safe identity, strategy, registration, owner-thread, Task completion, and cancellation
flag fields. It does not expose checkpoint bodies, messages, prompts, Tool arguments, event-loop
or Task representations, or environment data. A durable non-terminal Run is not guaranteed to
appear: waiting Runs and `RUNNING` checkpoints from an earlier process normally have no handle.

## Control operations and idempotency

```http
POST /v1/runs/{run_id}/resume
POST /v1/runs/{run_id}/cancel
POST /v1/runs/{run_id}/interrupt
```

Approval and rejection use the existing resume endpoint:

```json
{
  "decision": "approve",
  "invocation_id": "invocation-id"
}
```

For `resume`, `approve`, `reject`, and `cancel`, clients should send an
`Idempotency-Key` header. A body `request_id` is also accepted. The durable operation
identity is `run_id + idempotency_key`.

The first completed result or structured error is persisted in the Runtime SQLite
database. Repeating the same request returns the stored outcome, including after Runtime
restart. Reusing the same key with a different operation or payload returns
`idempotency_key_conflict` and HTTP 409. Requests without a key still use Run locking
and checkpoint compare-and-set, but do not have network-retry replay semantics.

Cancel is state-idempotent: cancelling an already `CANCELLED` Run returns its current
representation. Cancelling a terminal `COMPLETED` or `FAILED` Run is a conflict.
Cancelling a parent requests cancellation for every non-terminal direct child and stops
further child scheduling; cancelling a child does not automatically cancel its parent.
For an execution active in this process, cancellation first commits the durable `CANCELLED`
checkpoint and then uses the handle's owner loop to signal its `asyncio.Task`. A missing handle
is normal for waiting or restarted Runs and does not make durable cancellation fail.

## State transitions

| Status | Allowed control operations | Notes |
| --- | --- | --- |
| `RUNNING` | resume, cancel | Resume is used for explicit crash recovery. |
| `INTERRUPTED` | resume, cancel | A manual interrupt requires an explicit resume. |
| `WAITING_APPROVAL` | approve, reject, cancel | Decision targets the pending invocation. |
| `WAITING_CHILD` | resume, cancel | Multi-Agent resume reconciles all Children and may schedule independent ready work. |
| `COMPLETED` | none | Mutations return HTTP 409. |
| `FAILED` | none | Mutations return HTTP 409. |
| `CANCELLED` | cancel | Repeated cancel is a no-op; resume/approval conflict. |

Same-process per-Run operation locks and active registration conflicts prevent duplicate local
advancement. Cancel has a separate local control lock so it can commit while an execution owns
the advancement lock. SQLite checkpoint CAS remains the durable conflict detector. A competing operation returns
`operation_in_progress` or `checkpoint_conflict` rather than advancing twice.

## Error model

Run control endpoints return structured errors:

```json
{
  "error": {
    "code": "invalid_run_transition",
    "message": "resume is not allowed while run is COMPLETED",
    "run_id": "run-id",
    "status": "COMPLETED",
    "operation": "resume"
  }
}
```

| HTTP | Codes |
| --- | --- |
| 400 | `invalid_request` |
| 404 | `run_not_found`, `interrupt_not_found`, `child_run_not_found` |
| 409 | `invalid_run_transition`, `checkpoint_conflict`, `idempotency_key_conflict`, `interrupt_already_resolved`, `operation_in_progress` |
| 422 | Semantically valid JSON that the configured Runtime cannot execute. |
| 500 | Unexpected server failure. |

## Restart and recovery

At startup the local Runtime discovers persisted Runs through the configured thread
repository and checkpoint store:

- `WAITING_APPROVAL` remains waiting; no approval decision is inferred.
- `INTERRUPTED` remains waiting for client resume.
- `RUNNING` is exposed with `recovery_action=client_resume`.
- `WAITING_CHILD` Multi-Agent Parents reconcile every persisted Child and may start independent
  ready assignments within the configured concurrency bound. Other strategies resume after their
  direct Children become terminal.

This is local recovery discovery, not a distributed scheduler. An operation record that
was persisted as `IN_PROGRESS` but never completed is reported as
`operation_in_progress`; v1 does not lease or automatically steal abandoned operations.

## SSE hierarchy and replay

```http
GET /v1/threads/{thread_id}/events?after_id=123
GET /v1/threads/{thread_id}/events?after_id=123&run_id={run_id}
```

Every stored SSE envelope exposes `event_id`, `thread_id`, `turn_id`, `run_id`,
`parent_run_id`, `parent_step_id`, `assignment_id`, `event_type`, `timestamp`, and
`payload` as stable top-level fields.

Thread-wide replay remains the default. The optional `run_id` filter provides a single
Run view without changing replay ordering. `after_id` is exclusive, so reconnecting with
the last received Event ID does not repeat that Event. Parent and child Events share the
thread stream but retain explicit lineage.

## Parallel-child readiness

The control-plane model represents a parent with multiple children in different
states, aggregate multiple pending interrupts, approve one child, cancel another, and
cascade a parent cancellation to all non-terminal children. Multi-Agent Workers now use local
bounded durable scheduling; no distributed scheduler is implied.

## Current limitations

- No distributed scheduler or distributed execution.
- Active cancellation is supported only inside the current Runtime process. There is no
  cross-process signal, lease, heartbeat, fencing token, or distributed owner.
- No automatic startup recovery scanner or abandoned `RUNNING` ownership takeover is implemented.
- SSE is a persisted replay stream, not a distributed live event bus.
- Operation retention and compaction are not implemented.
- SQLite schema evolution is additive and has no general migration manager yet.
- Authentication remains the existing single Runtime API key model; there is no RBAC or
  multi-tenant authorization layer.
