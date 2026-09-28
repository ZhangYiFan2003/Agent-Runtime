# Deployment reliability evidence

This harness exercises the real Docker Compose deployment through its Web/Nginx entrypoint. It
uses the real Runtime API, PostgreSQL queue and ownership implementation, Worker replicas,
providerd, sandboxd, per-Run Sandboxes, MinIO, and persisted SSE replay. The only fake component is
a deterministic local OpenAI-compatible provider; no external provider or credential is used.

This is machine-specific evidence, not a CI threshold or universal SLO. The deterministic
crash-window correctness matrix in [`../recovery/`](../recovery/) remains authoritative for
checkpoint, deduplication, fencing, and ambiguous-side-effect semantics.

## Safety boundary

- Compose projects must start with `axiom-stage15`; each run gets isolated networks and volumes.
- Only an explicit service allowlist can be stopped, restarted, or recreated.
- The harness never runs Docker prune or removes resources outside its own Compose project.
- Credentials are fixed fake process-local values. It does not read `.env`.
- Generated JSON and Markdown reports go under `.tmp/stage15/`, which is gitignored.
- Interrupting the runner triggers `docker compose down --volumes --remove-orphans` in `finally`.

The Stage 15 overlay publishes PostgreSQL on a loopback-only disposable port so the existing
PostgreSQL recovery matrix can use the same isolated authority. Only the normal Web port and this
test-only loopback port are exposed; MinIO, Runtime API, providerd, and sandboxd remain internal.

## Load profiles

Build once, then reuse the images across the bounded profiles:

```powershell
uv run python -m benchmarks.deployment.run_load `
  --profiles smoke moderate-1-worker moderate-2-workers saturation `
  --build
```

The report separates admission latency, queue wait, execution, server end-to-end, and client
end-to-end latency when the corresponding persisted Runtime timestamps exist. It also records
accepted/rejected/completed/failed counts, rejection codes, throughput, cursor exclusivity,
duplicate event IDs, queue drain, and providerd active/pending state.

`saturation` intentionally constrains queue, active execution, provider concurrency/pending, and
submission rate. Structured 429/503 backpressure is recorded as rejection evidence, not silently
counted as an execution failure. `soak-2-workers` is an optional bounded local soak:

```powershell
uv run python -m benchmarks.deployment.run_load --profiles soak-2-workers --build
```

## Service-level failure injection

```powershell
uv run python -m benchmarks.deployment.run_faults --build
```

The maintained scenarios cover owning-Worker loss, providerd outage, upstream 500, sandboxd
outage/restart, MinIO outage, PostgreSQL authority outage, Runtime API restart, Web restart, and
providerd process-local governance reset. Each scenario is bounded and restores the affected
service before continuing. A final Artifact and Claim sample checks post-fault consistency.

Run a subset with `--scenarios worker-loss runtime-api-restart`. Use `--leave-running` only when a
manual diagnostic or the separate recovery matrix needs the isolated stack afterward; clean it
with the same project and overlay files, never with a global Docker command.

## Interpreting `/health`

`/health.workers` is the legacy count of API-local task worker threads. The Compose Runtime API
runs with `--task-workers 0`, so that field is not distributed Worker membership. Distributed
execution truth remains PostgreSQL leases/fencing plus persisted `run.claimed`, `run.taken_over`,
and ownership-loss events. The harness reports the configured replica count separately and does
not present `/health.workers` as live replica presence.
