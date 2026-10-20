# Runtime Control Plane API

Axiom's `/v1` Runtime API is the external control plane for durable Runs. Clients
operate on stable Run identities and state transitions; checkpoint JSON, Python tasks,
locks, and execution-strategy internals are not part of the HTTP contract.

## Authentication and service health

Protected endpoints accept the configured Runtime API key through `X-API-Key` or
`Authorization: Bearer ...`. `GET /v1/auth/check` uses this same authentication and
returns `200 {"authenticated": true}` for a valid key, or the existing
`401 {"error": "unauthorized"}` for an incorrect, empty, or missing key. This read-only
probe creates no Thread, Run, or Task and makes no model request.

`GET /health` remains public. It reports service and dependency information, not
credential validity or execution readiness. Its `workers` count is API-local legacy
task threads; `0` does not imply that distributed Workers are absent. A successful
auth check and `database=error` mean credentials are verified but the database is
unhealthy. The Runtime API key is separate from model provider credentials.

## Legacy tasks

`POST /v1/tasks` uses the SQLite legacy task queue and requires API-local task
workers (`--task-workers` greater than `0`). When disabled, authenticated submissions
return HTTP 503 with error code `TASK_WORKERS_DISABLED` before adding a task, with
no `Retry-After` header. Use the Thread/Run submission endpoints instead. Distributed
Workers consume the PostgreSQL Run queue and do not consume these legacy tasks.

The default Compose deployment keeps `--task-workers 0`. Listing, viewing, and
cancelling existing tasks remain available through `GET /v1/tasks`,
`GET /v1/tasks/{task_id}`, and `POST /v1/tasks/{task_id}/cancel`. Enabling local task
workers retains the existing queued/running/completed/failed/canceled lifecycle.

## Root submissions and replay

Submit a user message through `POST /v1/threads/{thread_id}/turns`, with
`Idempotency-Key` identifying one logical submission. The existing body `request_id`
form remains supported. Both SQLite local execution and PostgreSQL distributed
execution accept keys scoped to the Thread. Explicit clients may omit the key, but
then cannot recover a lost response through request deduplication.

Initial Run/Checkpoint, optional key mapping, and identified `turn.started`,
`user.message`, and admission events commit on one database connection in one
transaction. The history snapshot is built from confirmed completed root Runs inside
that same transaction. Engine initialization happens before acceptance; model, Tool,
memory, and trace work does not run inside the admission transaction.

Same-key, same-message/strategy replay returns the original Run and its current
status before new-submission capacity or rate checks. It never starts execution or
implicitly resumes a Run, including after completion. A different request with the
same key returns structured 409 `IDEMPOTENCY_KEY_CONFLICT` without changing the
original Run. Rejected submissions do not reserve a key or append start/message facts.

The database admits at most one occupying root Run per Thread. `RUNNING`,
`WAITING_APPROVAL`, `WAITING_CHILD`, and `INTERRUPTED` occupy the Thread until an
explicit transition ends them; `COMPLETED`, `FAILED`, and `CANCELLED` release it.
Child Runs retain their existing scheduling and concurrency. New submissions to an
occupied Thread return 409 `THREAD_BUSY` with `error.details.active_run_id`.
Reactivation and requeue must obey the same rule. Locks coordinate short database
operations and are not held across execution or across different Threads' model calls.

Every successful submission/replay response includes nonempty `thread_id`, `run_id`,
`turn_id`, and the existing uppercase `status` enum. Distributed admission normally
returns 202 with `status: "RUNNING"` and `queued: true`; local execution normally
returns 200 after completion with `text`. Active replay and waiting/interrupted results
return 202 with the original identity. A terminal replay returns 200, including a
persisted `FAILED` Run. `queued` is a distributed scheduling indication, not a new status.

Validation, initialization, busy-Thread, rate, or capacity rejection leaves no newly
accepted Run, key mapping, or user start events. Initialization rejection returns 503
`SUBMISSION_INITIALIZATION_FAILED`. An execution failure after acceptance retains the
Run; a lost response or an unexpected server error must be confirmed with the same
key and original request rather than sent again as a new submission.

Web keeps only unresolved submission key/message/Thread information in sessionStorage,
scoped to the Runtime address and Thread, without credentials. Network errors and
malformed success responses retain this information for the explicit **Confirm
submission** action; confirmed results and definite rejections clear it. Abandoning
confirmation does not cancel a possibly accepted Run. The local blocking-request
timeout remains unchanged; there are no automatic submission retries.

Completed root Checkpoints are the authority for final text. Their final
`assistant.message` and `turn.completed` projection is written with the terminal
checkpoint and can be filled idempotently from persisted state if an older process
left a gap. Repeated callbacks do not create another final answer. New conversation
events carry consistent Thread/Run/Turn identities. History and memory associate
completed root pairs by these identities, excluding Child output and incomplete or
ambiguous legacy records; original events remain available for inspection.

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

## Durable conversation pages

`GET /v1/threads/{thread_id}/messages` reads identified root `user.message` and
`assistant.message` facts from existing persisted Events, filtering in SQL before
limiting. It does not create another message table or change model-context history.
Unfinished user messages remain visible; Child output and ambiguous legacy records
remain excluded. Original records remain accessible through raw Events.

The default `limit` is 50, maximum 200. With no cursor, the latest page is returned
in ascending message-ID order. `before={older_cursor}` reads an exclusive older
page; `after={after_cursor}` reads exclusive newer messages in ascending order.
Do not combine `before` and `after`. The response contains:

```text
messages: [{id, thread_id, run_id, turn_id, role, text, created_at}]
older_cursor: string | null
after_cursor: string
has_more: boolean
current_run: {run_id, status} | null
```

`has_more` describes the requested direction; `older_cursor` is present only when
another older page exists. `after_cursor` is based on the last returned message
identity, not a later independent maximum-event read; an empty increment retains
its boundary. Keep the older and live cursors separate and merge pages by `id`.
`current_run` is the occupying root Run, otherwise the latest root Run, queried
from persisted Run state rather than inferred from a bounded debug-event window.
Final text is returned without the Run-detail output-summary truncation.

## Run queries

```http
GET /v1/runs
GET /v1/runs/{run_id}
GET /v1/runs/{run_id}/children
GET /v1/runs/{run_id}/interrupts
GET /v1/runs/{run_id}/trace
GET /v1/runs/{run_id}/metrics
GET /v1/runs/{run_id}/claims
GET /v1/claims/{claim_id}
GET /v1/claims/{claim_id}/provenance
GET /v1/runtime/active-runs
```

`GET /v1/runs` returns `{runs: RunSummary[], next_cursor: string | null,
has_more: boolean}`. The default `limit` is 50, maximum 200; `cursor` is exclusive.
Rows contain Run/Thread/Turn identities, kind, strategy, status, parent linkage,
creation/update/start/completion times and child counts. They omit full checkpoint,
output, trace and control details. Root and Child Runs remain in the same list.
The database applies literal substring `search` across Run/Thread/Turn IDs, kind
and strategy, plus exact `status` and `strategy` filters before limiting.

`sort` supports `newest` (default: `created_at`, `run_id` descending), `oldest`
(both ascending), `status` (active, waiting, failure, idle, success, unknown), and
`duration` (update minus creation time descending). Status and duration ties use
creation time and Run ID descending. Child counts are aggregated for the bounded
page; the API does not construct a full detail view for every list row. There is
no exact global total in this response. Run Detail retains its full read contract.

Message, Event and Run cursors bind their list kind and Thread/filter scope;
they cannot be reused across lists, Threads or Run filters. Invalid cursors,
conflicting boundary parameters, and limits outside `1..maximum` return HTTP 400
`invalid_request`, not an empty page or an unbounded read. End-of-list Run cursors
are `null`. SQLite and PostgreSQL apply filtering, ordering and `limit + 1` in SQL;
internal recovery and complete Child enumeration retain their separate semantics.

The public Run detail representation contains identity, strategy, status, parent linkage,
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

Claim endpoints are authenticated read projections. A Claim list exposes bounded text, citation,
evidence count, and aggregate integrity. The provenance endpoint resolves source-specific bounded
locators without returning Tool result bodies, Artifact bytes, or arbitrary Span attributes. Claim
creation is available only through the durable `record_claim` Tool; there is no HTTP mutation API.

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
GET /v1/threads/{thread_id}/events?cursor={next_cursor}&limit=200
GET /v1/threads/{thread_id}/events?latest=1&limit=200
GET /v1/threads/{thread_id}/events?run_id={run_id}&follow=1
```

Every stored SSE envelope exposes `event_id`, `thread_id`, `turn_id`, `run_id`,
`parent_run_id`, `parent_step_id`, `assignment_id`, `event_type`, `timestamp`, and
`payload` as stable top-level fields.

Thread-wide replay remains the default. The optional `run_id` filter provides a single
Run view without changing replay ordering. `after_id` is exclusive, so reconnecting with
the last received Event ID does not repeat that Event. Parent and child Events share the
thread stream but retain explicit lineage.

Finite replay defaults to 200 Events, maximum 500, in ascending Event-ID order.
Pagination metadata is carried in response headers, never as business Events:

- `X-Axiom-Events-After-Id`: last returned ID, or the unchanged input boundary.
- `X-Axiom-Events-Has-More`: more forward pages exist.
- `X-Axiom-Events-Has-Older`: the latest-window request omitted older Events.
- `X-Axiom-Events-Watermark`: observed replay boundary for terminal draining.
- `X-Axiom-Events-Next-Cursor`: scoped forward cursor, empty when exhausted.

`cursor` and `after_id` are alternatives; `Last-Event-ID` remains supported when
neither supplies a boundary. `latest=1` starts with the latest bounded window in
display order and cannot be combined with a cursor. It does not silently replay
all older logs. `follow=1` requires `run_id`, streams bounded database batches with
keepalives, and drains after observing terminal state instead of stopping after
a fixed number of pages. Finite clients likewise drain `has_more` pages through
the observed watermark before stopping. Missing Threads, auth failures and invalid
protocol responses remain visible errors; clients do not fall back to all-history
reads. Persistent conversation pages are independent of debug-log eviction.

The terminal boundary covers already committed business Events, including the
atomic final message/turn projection and earlier Artifact Events. Cleanup or Trace
debug Events written after this boundary are available through a later replay;
terminal draining does not promise to wait for arbitrary future writes.

## Browser connection scope

The frontend reuses the normalized Runtime URL identity from unresolved submission
storage. Query keys additionally include a non-persisted connection generation,
which changes on every verified save and explicit clear, including same-URL
credential replacement. API keys never enter query keys, URLs or persisted identity.
Old requests/replay loops are cancelled or ignored; late results cannot populate
the replacement connection. Recent Thread IDs and last selection are URL-scoped.
Legacy local records with no Runtime owner are discarded once, while unresolved
submissions retain their existing Runtime/Thread scope and explicit confirmation key.

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
