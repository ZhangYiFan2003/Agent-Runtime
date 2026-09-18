# Axiom Frontend Plan

Status: analysis and design only. No frontend code exists yet.
This document is the long-term context for all future frontend sessions.

Product position: AI Agent Runtime / developer tool / runtime control plane.
Not an admin dashboard, not an AI SaaS chat product. The `/v1` HTTP API is the
only boundary. Checkpoint JSON, asyncio tasks, locks, database tables, and
Runtime internals must never become UI dependencies.

---

## A. Runtime architecture summary (frontend-relevant)

- **Domain hierarchy**: `Thread → Turn → Parent Run → Child Runs`. A Turn is one
  user input; a Run is its durable, resumable execution. Child Runs (Plan tasks,
  Multi-Agent workers) share the parent's `thread_id`/`turn_id` but have their
  own `run_id`, checkpoint, status, interrupt, and trace. Lineage fields:
  `parent_run_id`, `parent_step_id`, `assignment_id`.
- **Durable state model**: append-only Events (history/replay) and versioned
  Checkpoints (recovery state) are separate. The public Run view
  (`run_view` in `src/axiom/runtime/control_plane.py`) is already a stable
  projection: identity, strategy, status, `waiting_reason`, `recovery_action`,
  lineage, timestamps, bounded `output` (4 KB) and `error`, child counts,
  `active_child_run_ids`, aggregated `pending_interrupts`, and
  `allowed_operations`. The UI can be driven entirely by this projection.
- **Status machine**: `RUNNING`, `INTERRUPTED`, `WAITING_APPROVAL`,
  `WAITING_CHILD`, `COMPLETED`, `FAILED`, `CANCELLED`. `allowed_operations`
  per status is computed server-side and returned in every Run view — the UI
  must render control buttons from this field, never from local status logic.
- **Control operations**: `resume` / `approve` / `reject` / `cancel`, all via
  `POST /v1/runs/{id}/resume|cancel` with restart-safe idempotency
  (`Idempotency-Key` header or body `request_id`; durable identity is
  `run_id + key`; stored outcome replayed; key reuse with different payload →
  409 `idempotency_key_conflict`). Approvals target a specific child Run +
  `invocation_id`, never the parent by proxy.
- **Execution strategies**: `react`, `plan_execute`, `multi_agent`
  (`execution_strategy` on the Run view; `run_kind` = `agent` / `plan_task` /
  `worker`). Plan and Multi-Agent parents run bounded local child schedulers;
  waiting children release compute slots.
- **Observability**: one Trace per Run (`trace_id` deterministic from
  `run_id`) with hierarchical spans (`agent`, `llm`, `tool`, `checkpoint`,
  `interrupt`, `policy`, `verification`). LLM spans carry provider, model,
  tokens, TTFT, latency, retry attributes. `RunMetrics` is computed from
  persisted spans: steps, LLM/tool calls, tokens, success rate, retry/cost/
  budget/progress/verification fields. Only the default durable ReAct path
  has step-level instrumentation; Plan/Multi-Agent parent internals and legacy
  engines are coarsely instrumented (v1).
- **Process model**: stdlib `ThreadingHTTPServer`, one request thread per
  connection, `asyncio.run` per request. `ActiveRunSupervisor` tracks
  process-local handles only; waiting runs and post-crash `RUNNING`
  checkpoints have no handle. Auth is a single static API key
  (`Authorization: Bearer` or `x-api-key`); `/health` is unauthenticated.
- **Storage**: SQLite (default, `~/.axiom/runtime`) or optional PostgreSQL,
  behind the same repository contracts. Irrelevant to the frontend except via
  `/health.storage_backend`.

---

## B. Current API contract inventory

Base URL `http://127.0.0.1:<port>`, all `/v1` routes require the API key.

### B.1 Stable, directly usable by the frontend

| Endpoint | Returns | Notes |
| --- | --- | --- |
| `GET /health` | status, workers, database, storage_backend, capacity | Unauthenticated; use for connection check |
| `POST /v1/threads` | `{id}` | Creates thread |
| `GET /v1/runs` | `{runs: RunView[]}` | All runs across all threads, creation-ordered. No pagination/filter params |
| `GET /v1/runs/{run_id}` | `RunView` | Stable projection incl. `allowed_operations`, `pending_interrupts` |
| `GET /v1/runs/{run_id}/children` | `{parent_run_id, children: ChildView[]}` | Stable ordering; child includes assignment_id, worker_role, attempt, interrupt |
| `GET /v1/runs/{run_id}/interrupts` | `{run_id, pending_interrupts[]}` | Aggregates run + direct children |
| `GET /v1/runs/{run_id}/trace` | `{trace, spans[]}` | 404 when no trace |
| `GET /v1/runs/{run_id}/metrics` | `RunMetrics` | 404 when no trace |
| `POST /v1/runs/{run_id}/resume` | RunView-like result | Body `{decision?: "approve"\|"reject", invocation_id?, request_id?}`; 202 when waiting/queued |
| `POST /v1/runs/{run_id}/cancel` | RunView | State-idempotent; cascades to non-terminal children |
| `POST /v1/runs/{run_id}/interrupt` | `public_dict` | Body `{reason}` |
| `GET /v1/threads/{thread_id}/events?after_id=&run_id=` | `text/event-stream` | Stored replay; envelope has stable top-level `event_id, thread_id, turn_id, run_id, parent_run_id, parent_step_id, assignment_id, event_type, timestamp, payload` |
| `GET /v1/runtime/active-runs` | `{active_runs[]}` | Process-local diagnostic: identity, strategy, registered_at, owner_thread_id, task_done, cancellation_requested |

Structured errors on run-control endpoints: `{error: {code, message, run_id?,
status?, operation?, details?}}` with codes `invalid_request` (400),
`run_not_found` / `interrupt_not_found` / `child_run_not_found` (404),
`invalid_run_transition` / `checkpoint_conflict` / `idempotency_key_conflict`
/ `interrupt_already_resolved` / `operation_in_progress` (409), 422, 500,
plus 503 admission rejection with queue details.

Event types persisted (non-exhaustive, must be treated as open):
`thread.created`, `turn.started`, `user.message`, `assistant.message`,
`turn.completed`, `run.started/resumed/completed/failed/cancelled/interrupted`,
`step.started/completed`, `agent.step.started`, `llm.started/completed/failed`,
`tool.started/completed`, `tool_call`, `tool_result`, `interrupt.created`,
`resume.started/completed`, `error`, `plan.*`, `multi_agent.*`, `worker.*`,
`review.*`, `synthesis.*`.

### B.2 Exists, but contract not stable enough to build on directly

| Endpoint | Issue |
| --- | --- |
| `POST /v1/threads/{id}/turns` | **Synchronous and blocking**: the HTTP request is held for the entire turn (`asyncio.run` of the durable run). Returns 200 `{thread_id, text}` for legacy engines, 200/202 run results for durable runs, 409 when a turn is already running. Response shape is engine-dependent. Usable with a long client timeout, but it is not an async "create run → poll" API |
| `POST/GET /v1/tasks`, `GET /v1/tasks/{id}`, `POST /v1/tasks/{id}/cancel` | Background queue path (prompt → deterministic durable Run `run_<task_id>`). Works as a non-blocking execution entry, but the task record model is minimal and separate from the Thread/Turn model |
| Error shape on non-run-control routes | 401/404/400/500 on threads/events/tasks return `{"error": "<string>"}`, not the structured error object. The client must handle both shapes |

### B.3 Needed by the frontend, currently missing

1. **CORS**: no `Access-Control-*` headers and no `OPTIONS` handling. A browser
   app cannot call the API cross-origin. Dev workaround: Vite dev-server
   proxy. Long-term: minimal additive backend change (opt-in CORS).
2. **Thread listing / history**: no `GET /v1/threads` (the repository has
   `list_threads` internally but it is not exposed) and no JSON thread-history
   endpoint. The Workbench cannot enumerate past conversations; events are
   only available as an SSE-format replay per known thread ID.
3. **Async turn/run creation**: no `POST … → 202 {run_id}` + poll pattern for
   turns in the default (non-distributed) mode. The blocking turns endpoint or
   the `/v1/tasks` queue are the only entries.
4. **Live SSE**: the events endpoint sends the full stored body with
   `Content-Length` and **closes** — it is replay, not a held-open stream. No
   heartbeat, no `limit`/`since` paging (unbounded body on long threads). The
   frontend must implement fetch-based reconnect-with-`after_id` polling;
   `EventSource` is also unusable because it cannot send the auth header.
5. **Run list pagination/filter**: `GET /v1/runs` returns everything.
6. **Interrupt detail**: `pending_interrupts` deliberately omits tool
   arguments — the approval UI can show `tool_name`, `reason`,
   `invocation_id`, but not the exact command/args being approved.
7. **No APIs for**: tool registry / MCP server listing, memory records,
   evaluation datasets/reports, runtime config, per-turn strategy/model
   selection (server config decides `agent_mode`). These product areas cannot
   be built against `/v1` today.
8. **No aggregate metrics endpoint** (only per-run metrics) — a global
   Metrics page would require client-side fan-out over `GET /v1/runs`.

**Rule**: none of these gaps may be fixed by changing Runtime internals in a
frontend phase. Additive API changes are a separate, explicit backend decision.

---

## C. API gap → frontend strategy mapping

| Gap | v1 strategy |
| --- | --- |
| CORS | Vite proxy `/v1` + `/health` in dev; document the limitation |
| Blocking turns | Call with extended timeout and an explicit "running…" pending state; offer `/v1/tasks` as the background alternative later |
| No live SSE | Custom `EventStreamClient`: `fetch` + reader, parse SSE frames, dedupe by `event_id`, reconnect/poll with `after_id` cursor and backoff |
| No thread list | Workbench keeps a local thread-ID registry (localStorage) with "open by ID" fallback |
| Missing product APIs (Tools/MCP, Evaluation) | Defer those pages; show per-run observability only |
| Two error shapes | Normalize both in the API client into one `ApiError` type |

---

## D. Recommended stack and directory structure

Confirmed stack (matches the owner's leaning; no better alternative found):

- **Vite + React 18 + TypeScript (strict)**
- **Tailwind CSS v4** with a custom design-token layer (CSS variables)
- **shadcn/ui (Radix primitives)** — mature headless components, matches the
  Linear/Vercel aesthetic, full styling control, no admin-template look
- **TanStack Query** as the server-state cache and the *only* run/thread data
  source of truth; no Redux/Zustand for server data
- **TanStack Router** — type-safe routes and search params (selection state
  such as `?span=` and `?event=` lives in the URL, making views shareable)
- **Zod** at the API boundary for DTO parsing and forward-compatible evolution
- No charts library in v1; the trace waterfall is custom SVG/divs. Add
  `recharts` later only if a Metrics page lands.

Layering (the contract isolation the owner asked for):

```text
HTTP/JSON → api/dto (zod schemas, snake_case, mirrors contract exactly)
         → api/adapters (DTO → camelCase view models, tolerance defaults)
         → queries (TanStack Query hooks; the only consumer-facing API)
         → components / routes (never import dto/ or fetch directly)
```

Backend field evolution then touches `dto/` + `adapters/` only. Unknown enum
values (new event types, span types, statuses) must degrade gracefully to
generic rendering, never crash.

```text
frontend/
├── index.html
├── vite.config.ts            # /v1 + /health proxy → 127.0.0.1:8080
├── src/
│   ├── main.tsx, router.tsx
│   ├── api/
│   │   ├── client.ts         # fetch wrapper: auth, base URL, error normalization
│   │   ├── idempotency.ts    # per-action key minting + retention until ack
│   │   ├── dto/              # zod schemas: run.ts, events.ts, trace.ts, metrics.ts, errors.ts
│   │   └── adapters/         # run.ts, timeline.ts, trace.ts, metrics.ts
│   ├── sse/
│   │   └── event-stream.ts   # fetch-based SSE replay client, cursor + dedupe + backoff
│   ├── queries/              # use-runs.ts, use-run.ts, use-trace.ts, use-events.ts,
│   │                         # use-run-actions.ts (resume/approve/reject/cancel/interrupt)
│   ├── routes/
│   │   ├── workbench.tsx
│   │   ├── runs.tsx
│   │   ├── run.$runId.tsx
│   │   └── settings.tsx
│   ├── components/
│   │   ├── ui/               # shadcn primitives
│   │   ├── run/              # status pill, action bar, children panel, approval card
│   │   ├── trace/            # waterfall, span node, latency bar
│   │   ├── events/           # event feed, typed event rows, JSON viewer
│   │   └── inspector/        # selection inspector panels
│   ├── design/tokens.css     # color/space/radius/type tokens (dark only)
│   └── lib/                  # time, format, ids
└── package.json
```

---

## E. Information architecture

v1 scope decision (from real API capability):

| Area | v1 | Reason |
| --- | --- | --- |
| **Workbench** | Yes | Core experience; thread + turn + live event feed exist |
| **Runs** | Yes | `GET /v1/runs` |
| **Run Detail** | Yes — flagship page | run view + children + interrupts + trace + metrics + events all exist |
| **Threads** | Merged into Workbench (no list API) | A standalone Threads page is blocked on gap B.3.2 |
| **Approvals** | Surfaced inside Workbench + Run Detail (pending_interrupts), not a separate page | Data exists per-run only |
| **Tools / MCP** | No | No API |
| **Metrics / Evaluation** | No global page; per-run metrics inside Run Detail | No aggregate/dataset API |
| **Settings** | Minimal (base URL, API key, connection check) | — |

Top-level nav (desktop sidebar, mobile bottom bar):
**Workbench · Runs · Settings** — only three items in v1. Run Detail is a
pushed route, not a nav item.

---

## F. Desktop vs mobile navigation

**Desktop (≥1280 px, wide-screen first)**:

- Fixed left icon+label sidebar (240 px, collapsible to 56 px).
- Runs: full-width dense table.
- Run Detail: three-column layout (see H).
- Workbench: thread rail (280 px) + conversation/event canvas + context
  inspector (320 px).

**Mobile (<768 px)** — not a shrunken flex-col:

- Bottom tab bar: Workbench / Runs / Settings.
- Runs list → full-screen Run Detail page with top tabs
  (`Overview · Timeline · Events · Trace`).
- Inspector becomes a **bottom sheet** opened by tapping an event/span.
- Thread rail becomes a **drawer**; approval cards become modal sheets with
  explicit Approve/Reject.
- All control actions stay reachable: the action bar collapses into a sticky
  footer.

---

## G. Workbench layout

Purpose: create a Thread, send a Turn, watch the Agent execute, handle
approvals — the DeepSeek-Harness-like core loop.

Desktop, three regions:

```text
┌────────────┬──────────────────────────────────┬────────────────┐
│ Thread rail│ Conversation / live event feed     │ Context panel  │
│  (280 px)  │                                  │  (320 px)      │
│ + New      │  user message bubble             │ active run     │
│ thread_…   │  assistant text (streamed at     │ status +       │
│ thread_…   │  event granularity)              │ strategy       │
│ (local     │  tool_call / tool_result rows    │ ──────────     │
│ registry + │  run lifecycle markers           │ pending        │
│ open-by-ID)│                                  │ approval card  │
│            │ ── composer ───────────────────  │ (tool, reason, │
│            │ textarea · Send · Interrupt ·    │ Approve/Reject)│
│            │ Cancel                           │ ──────────     │
│            │                                  │ token / step   │
│            │                                  │ counters       │
└────────────┴──────────────────────────────────┴────────────────┘
```

Behavior decisions:

- Send → `POST /v1/threads/{id}/turns` with long timeout; UI immediately
  enters a pending "turn running" state driven by event polling, not by
  guessing. A 409 "thread turn already running" maps to a disabled composer
  with explanation.
- While a turn runs, the feed is driven by the SSE replay client
  (`after_id` cursor). UI never accumulates its own copy of run state — run
  status comes from TanStack Query refetch of the Run view.
- Approval cards render from `pending_interrupts`; both decision buttons send
  one mutation with a fresh `Idempotency-Key`, disabled after first click.
- Composer offers Interrupt and Cancel only when the active Run's
  `allowed_operations` contains them.

---

## H. Run Detail layout (flagship page)

Desktop three-column:

```text
┌──────────────────────────────────────────────────────────────────┐
│ Header: status pill · strategy · run_kind · duration · tokens ·   │
│ lineage breadcrumb (parent ↓ children) · ActionBar (from          │
│ allowed_operations: Resume / Approve / Reject / Interrupt / Cancel)│
├──────────────┬───────────────────────────────┬───────────────────┤
│ Timeline     │ Center view (tabs)            │ Inspector         │
│ (waterfall,  │                               │ (selection-driven)│
│ 380 px)      │ • Events (typed feed)         │                   │
│ span tree by │ • Trace (span table)          │ span or event:    │
│ started_at;  │ • Metrics (definition list,   │ type, status,     │
│ bar =        │   tokens/TTFT/retry/budget/   │ duration, attrs,  │
│ latency_ms;  │   progress/verification)      │ input/output      │
│ color =      │ • Children (table: role,      │ summary, errors,  │
│ status,      │   attempt, status, interrupt) │ related run links │
│ green accent │ • Output (final text)         │ (parent/child)    │
│ on selection │                               │                   │
├──────────────┴───────────────────────────────┴───────────────────┤
│ Approval banner when pending_interrupts non-empty                 │
└──────────────────────────────────────────────────────────────────┘
```

Decisions:

- **Timeline = spans, not events.** Spans have real durations
  (`started_at/ended_at/latency_ms`) and hierarchy (`parent_span_id`) — a
  waterfall is honest. Events are point-in-time and belong in the feed tab.
  Selecting a span and an event both populate the Inspector; selection lives
  in URL search params.
- **Data only from what exists**: Inspector shows span `attributes` verbatim
  (JSON viewer) plus known promoted fields (provider, model, tokens, TTFT,
  retry); it must not invent fields. Where Plan/Multi-Agent parent internals
  are uninstrumented (v1 limitation), show the sparse trace as-is.
- **Auto-refresh policy**: while `status` is non-terminal, TanStack Query
  refetches run view + trace + metrics on a short interval; terminal status
  stops polling. SSE events arrive via the thread stream filtered by
  `run_id`, deduped by `event_id`.
- **Children**: parent runs show the children table and a mini lineage map;
  child runs show a "parent" link and sibling list via the parent's
  `/children`.
- **Recovery affordance**: a stale `RUNNING` run (no entry in
  `/v1/runtime/active-runs`) is labeled "no active process — recovery via
  Resume" using `recovery_action`.

Mobile: same data, rearranged — tabs `Overview / Timeline / Events / Trace /
Children`, Inspector as bottom sheet, ActionBar as sticky footer.

---

## I. Design system

Direction: **80 % minimal developer tool, 20 % subtle cyberpunk console.**
References: Linear's density + Vercel's restraint + DeepSeek Harness's
technical feel, with a faint phosphor-green terminal accent. Dark theme only.

**Color** (CSS variables, Tailwind tokens):

| Token | Value | Use |
| --- | --- | --- |
| `bg-0` | `#0B0E0C` | app background (near-black w/ green undertone) |
| `bg-1` | `#101411` | panels, sidebar |
| `bg-2` | `#161B17` | raised surfaces, hover |
| `bg-3` | `#1D241F` | active/selected surface |
| `border` | `#232A25` | 1 px hairlines everywhere |
| `fg-0` | `#E8EDE9` | primary text |
| `fg-1` | `#9AA69D` | secondary text |
| `fg-2` | `#5C665E` | muted/metadata |
| `accent` | `#34D399` | emerald — running/active/selected/focus/key action/trace highlight/terminal cursor |
| `accent-dim` | `#34D399 @ 12 %` | selection backgrounds, subtle glow |
| `warn` | `#E8B44A` | waiting_approval, retry, degraded |
| `danger` | `#E4574F` | failed, errors, reject |
| `info` | `#7BA7BC` | interrupted, links (used sparingly) |

Green is rationed: status=RUNNING, selected timeline row, focus rings,
primary button, terminal cursor, trace highlight. No gradients, no
glassmorphism, no neon glow beyond a 1 px `accent-dim` ring.

**Status color map** (single source: `run/status.ts`):
RUNNING → accent (+ slow pulse dot), WAITING_APPROVAL → warn,
WAITING_CHILD → info, INTERRUPTED → info, COMPLETED → fg-1 (+ accent check),
FAILED → danger, CANCELLED → fg-2.

**Typography**: UI — `Inter` (or system stack fallback); data, IDs, JSON,
events, terminal — `JetBrains Mono`. Scale: 12 / 13 / 14 / 16 / 20 px; dense
default 13 px. IDs truncated with mono + copy-on-click.

**Spacing**: 4 px base grid; common paddings 8 / 12 / 16; page gutters 24.
Dense tables: 32 px row height.

**Radius**: 4 px default, 6 px cards/popovers, 2 px tiny chips. Not
everything is a rounded card — hairline-separated regions preferred.

**Border / shadow**: 1 px `border` hairlines are the primary separator.
Shadows nearly absent; one `shadow-pop` (`0 4px 16px rgb(0 0 0 / .5)`) for
menus/sheets only.

**Components**:

- **Button**: primary (accent bg, black text, only for key actions like Send /
  Approve), default (bg-2 + hairline), danger (reject/cancel), ghost. Height
  28 / 32 px, radius 4, mono 12 px labels allowed.
- **Input/textarea**: bg-1, 1 px border, focus = 1 px accent ring, no fill
  change.
- **StatusPill**: 10 px dot + mono uppercase 11 px label; dot pulses only for
  RUNNING.
- **Code/JSON viewer**: mono 12 px, bg-1, syntax-dim; collapsible nodes;
  copy button; secrets never expected here (interrupt args are not exposed).
- **Terminal block**: bg-0, mono 12.5 px, green cursor/caret accent — the one
  place a subtle phosphor feel is allowed.
- **Timeline/waterfall**: left span labels (mono, indented by depth), right
  proportional latency bars (fg-2 fill, accent for selected, danger edge for
  failed), hover crosshair, duration axis in ms/s.
- **Event row**: mono 11 px timestamp + colored type chip + one-line payload
  summary; expandable to JSON.
- **Approval card**: warn hairline, tool name + reason, invocation ID
  (truncated), explicit Approve (primary) / Reject (danger-outline) — never a
  single ambiguous button.

---

## J. Designing for backend evolution

1. **Idempotency**: every mutation hook mints one UUID key per *user
   intention* (not per HTTP attempt) and reuses it across retries until a
   definitive response. 409 `idempotency_key_conflict` surfaces as an error;
   replayed stored results are accepted silently. `Idempotency-Key` header is
   primary; body `request_id` only as fallback.
2. **Retry policy**: GETs auto-retry ≤3 with backoff (idempotent). Mutations
   retry automatically **only** on network-level failure with the same key —
   never on 4xx/5xx responses.
3. **SSE client**: cursor = last `event_id`, reconnect with `after_id`,
   exponential backoff with jitter, dedupe set on `event_id`, per-thread
   cursor registry. Events are grouped by `run_id` / `parent_run_id` /
   `assignment_id` for lineage display; unknown `event_type` renders via a
   generic fallback row. Since the stream is replay-only today, the client
   already supports "poll mode"; if the backend later holds connections open,
   only the transport changes.
4. **UI is never a second source of truth**: run status, allowed operations,
   pending interrupts always come from query refetch of the Run view. Local
   state is limited to selection, composer drafts, and the thread-ID registry.
5. **Forward compatibility**: zod schemas use `.passthrough()` on payloads and
   span attributes; `schema_version` fields are preserved in DTOs; new enum
   members (status, span_type, strategy) fall back to neutral rendering.
6. **Backend evolution hooks**: the adapter layer absorbs renames; the API
   client centralizes error-shape normalization (structured vs plain); base
   URL/auth are config, so a future remote/multi-user deployment only changes
   Settings.

---

## K. Development phases (7)

Each phase ends in a usable, shippable state. No phase modifies Runtime
internals.

1. **Scaffold & contract layer** — Vite/React/TS/Tailwind/shadcn setup, design
   tokens, API client + zod DTOs + adapters, Settings (base URL, API key,
   `/health` check), Vite proxy, app shell + sidebar.
2. **Runs list** — dense table with status pills, strategy, times, client-side
   filter/sort; empty/error states.
3. **Run Detail read-only** — header + ActionBar rendering (actions still
   disabled), trace waterfall, span table, metrics panel, children table,
   inspector, mobile tabs/bottom-sheet.
4. **Event stream** — SSE replay client (cursor, dedupe, backoff), events tab
   in Run Detail, auto-refresh orchestration for non-terminal runs.
5. **Control operations** — resume/approve/reject/cancel/interrupt mutations
   with idempotency keys, approval cards/banners, optimistic-disable +
   refetch flow, structured-error surfacing.
6. **Workbench** — thread rail (local registry + open-by-ID), composer,
   blocking-turn pending UX, live conversation/event feed, inline approvals.
7. **Polish & hardening** — mobile pass, keyboard shortcuts, shareable URLs,
   loading/skeleton states, e2e smoke against a local Runtime, docs.

Deferred until backend APIs exist: global Metrics/Evaluation page, Tools/MCP
page, standalone Threads page, aggregate dashboards.
