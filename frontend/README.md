# Axiom Frontend

Developer console for the local Axiom Runtime: Agent Workbench (threads,
blocking turns, live execution feed, approvals), Runs explorer, Run
Inspector (trace waterfall, events, metrics, children, controls), and
Settings. Architecture source of truth:
`../docs/frontend/FRONTEND_PLAN.md`.

## Requirements

- Node.js ≥ 22 (developed on Node 24)
- A local Axiom Runtime for live data (optional for `npm run dev`)

## Setup

```bash
npm install
```

## Dev

```bash
npm run dev          # http://localhost:5173
```

`/v1` and `/health` are proxied to the Runtime. Default target:
`http://127.0.0.1:8080`. Override with the `AXIOM_RUNTIME_TARGET`
environment variable (dev-server config, never bundled).

## Build / test / typecheck

```bash
npm run build
npm run test
npm run typecheck
```

## Runtime connection & API key

Open **Settings**, leave Base URL empty to use the dev proxy (or enter a
full URL, e.g. `http://127.0.0.1:8080`), paste the Runtime API key, then
**Test Connection**. Only a protected `/v1/auth/check` response containing
`authenticated: true` verifies and persists the credentials to `sessionStorage`
(survives reload, cleared with the tab); **Clear credentials** wipes it.
The key is never written to `.env`, `VITE_*` variables, or this repo —
Vite inlines those into the browser bundle.

`/health` is unauthenticated; authenticated `/v1` calls send the key as
`Authorization: Bearer <key>`. A missing verification endpoint is reported as
unsupported; public health is never used as an authentication fallback. Health
and database failures are shown separately after credentials are verified.
The sidebar reports only service reachability. Health's `workers` count means
API-local legacy task threads, excluding distributed Workers; the default
Compose deployment reports zero. The Runtime key is separate from the model
provider key.

## Turn identity and confirmation

Each new message sends one `Idempotency-Key`. A successful response must
contain nonempty `thread_id`, `run_id`, `turn_id`, and a known Run `status`;
the Thread must match the request. An empty or malformed 2xx response is a
protocol error with an unconfirmed outcome. Optional `queued` is a boolean
indicating queue admission; optional `text` is a string containing available
assistant output. Missing text does not imply completion.

Network failures, interrupted requests, protocol errors, and unknown server
failures keep the original message and key in `sessionStorage`, scoped to
the normalized Runtime address and Thread. This record contains no API key
or conversation history. **Confirm submission** explicitly replays that
same message/key; refresh and Thread switching never resend it silently.
**Forget submission** clears the record but does not cancel an accepted Run.
The local synchronous turn timeout remains approximately 24.8 days.

Explicit 4xx rejections and the Runtime's structured pre-admission capacity
or initialization 503 errors restore the draft and clear the unconfirmed
record. Editing and sending again creates a new key. `THREAD_BUSY` displays
the actual active Run identity. Repeated Run lookup 404s show an inconsistency
notice and permit another submission to check the Thread's availability.
Conversation echoes reconcile by Thread/Run/Turn identity, while Child Run
output remains separate from root assistant answers. Event retention remains
at the existing 1,500-event window; pagination is deferred.
