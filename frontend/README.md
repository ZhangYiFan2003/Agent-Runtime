# Axiom Frontend

Developer console for the local Axiom Runtime. Phase 1: scaffold, design
system, app shell, API contract layer (client + zod DTOs + adapters),
Settings with `/health` connection check. Architecture source of truth:
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
**Test Connection**. A successful check is persisted to `sessionStorage`
(survives reload, cleared with the tab); **Clear credentials** wipes it.
The key is never written to `.env`, `VITE_*` variables, or this repo —
Vite inlines those into the browser bundle.

`/health` is unauthenticated; authenticated `/v1` calls send the key as
`Authorization: Bearer <key>`.
