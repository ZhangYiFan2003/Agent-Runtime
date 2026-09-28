# Production reliability and failure model

This document describes the current single-host Compose reliability boundary and the Stage 15
validation procedure. It does not claim high availability or define a universal SLO.

## Correctness evidence versus deployment evidence

Two complementary suites cover different questions:

- `benchmarks/recovery/` maps 25 deterministic crash-window scenarios across ReAct, Plan,
  Multi-Agent, and distributed redelivery. Two repetitions are the default. Those tests remain the
  source of truth for checkpoint recovery, tool deduplication, lease takeover, bounded delivery,
  and expected-safe handling of ambiguous external side effects.
- `benchmarks/deployment/` operates the real Compose services over HTTP under bounded load and
  service-level failures. It measures observed capacity and recovery on one machine; it does not
  prove every internal crash window.

Generated deployment evidence is kept in `.tmp/stage15/` and should not be committed by default.

## Operational truth

PostgreSQL is the distributed Run, queue, event, Artifact metadata, and Claim metadata authority.
Worker ownership is proven by expiring leases and fencing tokens. `run.claimed`, `run.taken_over`,
`run.released`, and `run.ownership_lost` provide persisted operational evidence.

The `workers` value in Runtime `/health` counts legacy API-local task threads. It is normally zero
in Compose and must not be used as distributed replica membership. The current v1 baseline has no
membership registry; operators use Compose state together with PostgreSQL capacity and ownership
evidence. This preserves the existing lease/fencing model rather than adding a competing truth.

providerd governance (active/pending admission, circuit state, rate-limit cooldown, and counters)
is process-local. Restarting providerd resets that state by design. Durable Runtime retry, budget,
and tool safety remain authoritative across that reset.

## Expected fault behavior

| Fault | Expected behavior |
| --- | --- |
| Owning Worker stops | Another Worker may claim after release/lease expiry; fencing rejects stale ownership. |
| Runtime API or Web restarts | Admitted Worker execution continues; control/replay returns after the service recovers. |
| providerd unavailable/upstream fails | Run stops failed or expected-safe under existing bounded Runtime retry; no hidden gateway retry. |
| sandboxd unavailable | Shell fails closed; Workers never gain Docker socket access. |
| sandboxd restarts | Labeled managed containers are rediscovered; terminal cleanup still removes the Run container. |
| MinIO unavailable | Artifact publication fails without inventing metadata/content; control-plane data remains in PostgreSQL. |
| PostgreSQL unavailable | Admission/control fail rather than falling back to a local authority. |

## Diagnostics

Start with the same-origin `/health`, then inspect the affected Run and persisted events. Capacity
reports queue/active counts and delivery attempts. providerd internal `/v1/providers` reports
target admission and circuit state without credentials or base URLs. Sandbox diagnosis should use
the `axiom.managed`, `axiom.run_id`, and `axiom.run_id_hash` labels; never grant a Worker or Sandbox
the Docker socket to simplify inspection.

Useful bounded commands are documented in [`../deploy/README.md`](../deploy/README.md). Avoid
global Docker cleanup, database mutation, or synthetic production-only chaos endpoints.

## Known single-host limits

- PostgreSQL, MinIO, providerd, sandboxd, and the Docker daemon are single points of failure.
- Web and Runtime API restart recovery is not multi-host failover.
- providerd admission/circuit state is not durable or replicated.
- Compose replica state is external operational state, not a Runtime membership protocol.
- Artifact blob and PostgreSQL metadata backups must be coordinated.
- The trusted single-user boundary, one Runtime API key, and Docker-host authority of sandboxd are
  unchanged.
