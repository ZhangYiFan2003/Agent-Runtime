# Axiom single-host deployment

This Compose baseline runs Axiom for one trusted operator on a Linux server or WSL host.
PostgreSQL is the durable correctness authority; it is not a multi-tenant or hostile-code
deployment boundary.

## Architecture

```text
Browser
  |
  v
Web (static SPA + same-origin reverse proxy)
  |
  v
Runtime API (control and admission plane)
  |                         |
  v                         v
PostgreSQL                MinIO
(Run/Event/Artifact       (Artifact Blob bytes)
 metadata authority)
  ^
  |
Worker replicas (claim, heartbeat, fenced execution)
  |                                  |
  v                                  v
sandboxd                         providerd
(trusted Docker controller)     (trusted Provider Gateway)
  |                                  |
  v                                  v
per-Run Sandbox containers      external LLM Providers
(network none)
```

The API and Workers share a single-host `runtime_data` volume only for the existing SQLite-backed
observability, memory, and local task support. PostgreSQL remains authoritative for distributed
Runs. Only `web` publishes a host port; Runtime API, Workers, and PostgreSQL stay on the Compose
network.

Runtime API and Workers reach MinIO only on the private `artifact-storage` network. PostgreSQL is
the Artifact metadata authority; MinIO stores immutable SHA-256 Blob bytes in `artifact_data`.
MinIO publishes no host port. Web downloads Artifact content through the authenticated Runtime API
and never receives object-store credentials.

Claim/Evidence provenance metadata is stored in PostgreSQL with the other distributed Runtime
truth. Provenance does not add a service, network, or credential boundary; Compose explicitly
enables the `record_claim` Tool and its short prompt guidance for Workers.

Workers reach sandboxd only on the private `sandbox-control` network. Only sandboxd mounts the
Docker socket. Runtime API, Web, PostgreSQL, Workers, and dynamically created Sandbox containers do
not receive Docker daemon access. Sandbox containers join no Compose network.

Workers reach providerd only on the private `provider-control` network. Only providerd receives
external provider credentials. Runtime API, Web, PostgreSQL, sandboxd, and Sandbox containers do not
join that network; providerd and sandboxd publish no host ports. `provider-control` remains
internal. Only providerd also joins the ordinary `provider-egress` bridge for external model
DNS/TLS connections; no other service or per-Run Sandbox joins that egress network.

## Prerequisites

- Docker Engine
- Docker Compose v2

## Configuration

```bash
cp deploy/env.example deploy/.env
```

Replace every `CHANGE_ME`. `POSTGRES_PASSWORD` and the password embedded in
`AXIOM_POSTGRES_DSN` must match. The Runtime keeps its existing single API key model; users enter
`AXIOM_RUNTIME_API_KEY` in Web Console Settings after startup. Provider credentials are optional
for startup and health checks, but real Agent Turns require a configured key in providerd.
`MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` are local deployment credentials used only by MinIO,
the one-shot bucket initializer, Runtime API, and Workers. They are not injected into Web,
providerd, sandboxd, or per-Run Sandbox containers.

The default gateway route contains one DeepSeek-compatible target. `AXIOM_PROVIDER_ROUTES_JSON` can
replace it with an ordered route object using the `provider_gateway.routes` schema documented in
[`docs/provider-gateway.md`](../docs/provider-gateway.md). Targets reference credential environment
variable names; do not place credential values in the route JSON. In gateway mode, Workers receive
only `AXIOM_GATEWAY_ROUTE` and the internal providerd URL.

The default workspace is an isolated named volume. To work on host files, set
`AXIOM_WORKSPACE_MOUNT` to an absolute path containing only a dedicated Agent workspace. Do not
mount `/`, a home directory, or `/var/run/docker.sock`. A workspace mount limits what is presented
to Axiom. When using a bind mount, set `AXIOM_SANDBOX_WORKSPACE_SOURCE` to the same absolute host
path. The default named-volume source is `axiom_workspace_data`.

Sandbox resource defaults are adjustable with the documented `AXIOM_SANDBOX_*` values. The image
is fixed trusted configuration; Agent requests cannot select images, mounts, networks, devices,
capabilities, or privileged mode.

## Start

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile sandbox-build build
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
```

The `sandbox-image` profile is build-only; it does not create a long-running service. At startup,
Workers fail closed if sandboxd, Docker, the configured Sandbox image, or providerd is unavailable.

Open `http://SERVER:8080` by default. Change `AXIOM_WEB_PORT` when another host port is required.

## Scale Workers

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --scale worker=2
```

Worker IDs are generated from the container hostname, process ID, and a UUID. Replicas share the
same PostgreSQL ownership authority and retain lease, heartbeat, fencing, capacity, priority,
bounded-redelivery, and failure-queue semantics.

## Logs

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml logs -f runtime-api
docker compose --env-file deploy/.env -f deploy/compose.yaml logs -f worker
```

Services log to stdout/stderr.

## Reliability validation

The bounded Stage 15 harness starts an isolated Compose project with fake process-local
credentials and a deterministic local provider. It does not read this deployment's `.env` or call
an external LLM. Its fake provider shares `provider-control`, so successful Runs validate the
gateway and execution path, not external DNS/TLS connectivity or a real model request:

```powershell
uv run python -m benchmarks.deployment.run_load --profiles smoke moderate-1-worker moderate-2-workers saturation --build
uv run python -m benchmarks.deployment.run_faults --build
```

Results are written under `.tmp/stage15/`. See
[`benchmarks/deployment/README.md`](../benchmarks/deployment/README.md) for safe project cleanup,
profile bounds, and interpretation, and
[`docs/operations-reliability.md`](../docs/operations-reliability.md) for the current fault model.

Validate Compose without printing interpolated configuration or credentials:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet
```

Check external DNS/TLS/HTTP from inside providerd separately; the
[isolated harness instructions](../benchmarks/deployment/README.md#external-connectivity-check)
provide a request without credentials or model inference. An HTTP 401/403 is connectivity
evidence, not model success. Record Compose/network configuration, container external
connectivity, and a real model response separately. Only run a bounded, low-cost model request
when explicit test credentials and call authorization are already available. Model calls are
never part of health checks or connection verification.

`GET /health` reports API-local task workers, not distributed Worker replica membership. For
distributed diagnosis, use Compose service state plus PostgreSQL capacity/lease evidence and
persisted ownership events. providerd's governance state is process-local and resets on restart.

providerd exposes internal-only `/health` and `/v1/providers` status. These responses include route
count, target identity, circuit state, admission counts, cooldown, and governance counters, but no
credentials, authorization headers, or base URLs.

## Provider routing lifecycle

Every Runtime model attempt reaches providerd once. providerd selects the first ordered target that
is eligible under shared concurrency, bounded pending admission, optional RPM, circuit state, and
rate-limit cooldown. It can choose a secondary only before an upstream request starts. It never
retries or changes providers after an upstream request or partial stream begins; the durable Runtime
remains the retry authority and charges every real attempt to RunBudget.

Gateway streams identify the actual provider/model before substantive output. Runtime cost is
reserved conservatively across route targets and reconciled using actual target pricing and usage.
Provider Gateway governance is process-local to the single providerd replica and resets on restart.

## Shell sandbox lifecycle

Approved Shell calls use one non-root Docker container per Runtime Run. Sequential calls in one Run
reuse the container; different and Child Runs receive separate containers. The root filesystem is
read-only, `/tmp` is a bounded tmpfs, Linux capabilities are dropped, privilege escalation is
disabled, networking is `none`, and CPU, memory, and PID limits are configured.

The deployment workspace is mounted read/write at `/workspace` and is intentionally shared between
Run containers in this trusted single-user version. Container removal does not delete workspace
data. Timeout, cancellation, and terminal Run cleanup force-remove the managed container. Docker
labels allow sandboxd to rediscover an existing Run container after its own restart.

## Stop and restart

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml down
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

Do not add `-v` unless you intentionally want to delete PostgreSQL and Runtime volumes.

## Backup and restore

Use a coordinated **stop-writes** backup. PostgreSQL `pg_dump` is consistent within its database;
it does not create a simultaneous snapshot of MinIO or the workspace. Docker volumes are
persistence, not backups.

| State | Current location and recovery purpose |
| --- | --- |
| Distributed Runtime authority | `postgres_data`: Run heads/checkpoints, Tool executions, budgets, delivery/ownership, Thread events/messages, submission/control idempotency, Artifact metadata and Claim provenance. Restore the database together. |
| Artifact bytes | `artifact_data`, through the dedicated MinIO bucket. Export objects through the S3 API; do not copy MinIO's live internal directory. Metadata alone cannot restore bytes. |
| Auxiliary local state | `runtime_data` at `/var/lib/axiom`: `runtime.db` observability, `memory.db`, `tasks.db`, and any configured local Artifact store. This is shared single-host state even when Runtime authority uses PostgreSQL. |
| Agent files | `workspace_data` or the explicit `AXIOM_WORKSPACE_MOUNT`. Preserve it with the same backup boundary as tool records and artifacts. |
| Local-only Runtime | `<data-dir>/runtime.db` holds the coherent SQLite repositories; the default is `~/.axiom/runtime`. Preserve its sibling memory/tasks databases and local `artifacts/`, or their explicitly configured paths. |
| Other host/container state | Explicit host memory paths and `~/.axiom/snapshots` need separate preservation if used. Compose does not mount `/home/axiom/.axiom/snapshots`; snapshots in a container's writable layer do not survive recreation. |

Query caches, the Web event window, process handles, Worker identities, and providerd circuit/admission
state are rebuildable. Ownership leases are database records, not reusable process handles. Retain
configuration **names**, source SHA, built image identities, schema version, workspace mount mode,
Artifact bucket/endpoint names, and the backup boundary in an access-controlled manifest. Manage
credential values through the existing secret mechanism; do not put them in a manifest, source
control, or command output. Backups contain private user data and need protected storage outside
the checkout.

The following commands are Bash for the supported Linux/WSL host. Select the existing project
explicitly; use the same reviewed Compose overrides as the running deployment. Never run
`docker compose config` without `--quiet` when credentials are interpolated. Before beginning,
inspect active Runs in the Console and decide which may finish or require explicit interruption
or cancellation. Stop scheduled CLI jobs and other host writers too.

```bash
set -euo pipefail
project=axiom
backup_id="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$(pwd)/../axiom-backups/$backup_id"
mkdir -m 700 -p "$backup"
dc() { docker compose -p "$project" --env-file deploy/.env -f deploy/compose.yaml "$@"; }
dc config --quiet
dc stop web
```

Stopping Web closes the published admission/control entrypoint. Block any other direct clients.
Allow safe work to finish, or apply the previously chosen controls through an existing authorized
operator route before stopping the API. Worker shutdown can leave resumable nonterminal Runs;
it is not proof that every external side effect completed or was rolled back.

```bash
dc stop -t 40 runtime-api worker
```

Confirm all Worker replicas and API processes stopped. Also stop every managed per-Run Sandbox
that can still write this workspace: use only container IDs selected by both
`axiom.managed=true` and the recorded `axiom.run_id` labels, never a global Docker stop/prune.
Stop sandboxd and providerd after execution has stopped. Keep PostgreSQL and MinIO available
for logical export.

```bash
dc stop sandboxd providerd
dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup/runtime.dump"
dc run --rm --no-deps -T --user 0:0 -v "$backup:/backup" --entrypoint python runtime-api - <<'PY'
import tarfile
for source, name in [("/var/lib/axiom", "runtime-data"), ("/workspace", "workspace")]:
    with tarfile.open(f"/backup/{name}.tar.gz", "w:gz") as archive:
        archive.add(source, arcname=".")
PY
dc run --rm --no-deps -T --user 0:0 -v "$backup:/backup" --entrypoint python runtime-api - <<'PY'
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit
from minio import Minio

client = Minio(urlsplit(os.environ["AXIOM_ARTIFACT_S3_ENDPOINT"]).netloc,
               access_key=os.environ["AXIOM_ARTIFACT_S3_ACCESS_KEY"],
               secret_key=os.environ["AXIOM_ARTIFACT_S3_SECRET_KEY"], secure=False)
bucket = os.environ["AXIOM_ARTIFACT_S3_BUCKET"]
root = Path("/backup/artifact-objects")
root.mkdir()
for item in client.list_objects(bucket, recursive=True):
    target = root / item.object_name
    target.parent.mkdir(parents=True, exist_ok=True)
    client.fget_object(bucket, item.object_name, str(target))
    with target.open("rb") as content:
        assert hashlib.file_digest(content, "sha256").hexdigest() == target.name
PY
```

The object export assumes this deployment's dedicated content-addressed Artifact bucket; its
object names end with the SHA-256 digest. The scripts use credentials only inside the existing
trusted service environment. Preserve required container-local snapshots before recreation,
using `docker compose cp` against the specific container into the protected backup location.
Keep every writer paused until all exports and a checksum inventory succeed. Resume the existing
deployment only after completing that boundary, or continue with the upgrade below.

For standalone SQLite, stop the API, all local task workers, CLI writers, and workspace tools.
Use the standard backup interface for each database, including committed WAL content:

```bash
uv run python - /absolute/data-dir/runtime.db /absolute/backup/runtime.db <<'PY'
import sqlite3
import sys
from contextlib import closing
with closing(sqlite3.connect(sys.argv[1])) as source:
    with closing(sqlite3.connect(sys.argv[2])) as destination:
        source.backup(destination)
PY
```

Repeat for `memory.db` and `tasks.db` while writers remain stopped, then copy local Blob bytes and
the workspace. Do not copy only an actively written SQLite main file; committed records can still
be in `-wal`. Alternatively copy all stopped files, including their WAL companions, as a unit.

### Isolated restore and nonterminal Runs

Restore first into an empty project with different ports, independent volumes and a disposable
workspace. Use the backed-up source/images and separately supplied **test** configuration. Do not
reuse a production bind mount, database DSN, provider route, or external credentials. Supply the
documented process-local fake provider from the [deployment harness](../benchmarks/deployment/README.md)
before any execution. Restoring backups alone must not trigger real external effects.

With the `dc` function redirected to that isolated project/configuration, start only storage, then
restore the dump into the empty database:

```bash
dc up -d postgres minio
dc exec -T postgres sh -c 'pg_restore --exit-on-error --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$backup/runtime.dump"
dc run --rm --no-deps -T --user 0:0 -v "$backup:/backup:ro" --entrypoint python runtime-api - <<'PY'
import tarfile
for target, name in [("/var/lib/axiom", "runtime-data"), ("/workspace", "workspace")]:
    with tarfile.open(f"/backup/{name}.tar.gz") as archive:
        archive.extractall(target, filter="tar", numeric_owner=True)
PY
dc run --rm --no-deps -T --user 0:0 -v "$backup:/backup:ro" --entrypoint python runtime-api - <<'PY'
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit
from minio import Minio

client = Minio(urlsplit(os.environ["AXIOM_ARTIFACT_S3_ENDPOINT"]).netloc,
               access_key=os.environ["AXIOM_ARTIFACT_S3_ACCESS_KEY"],
               secret_key=os.environ["AXIOM_ARTIFACT_S3_SECRET_KEY"], secure=False)
bucket = os.environ["AXIOM_ARTIFACT_S3_BUCKET"]
if not client.bucket_exists(bucket):
    client.make_bucket(bucket)
root = Path("/backup/artifact-objects")
for source in root.rglob("*"):
    if source.is_file():
        with source.open("rb") as content:
            assert hashlib.file_digest(content, "sha256").hexdigest() == source.name
        client.fput_object(bucket, source.relative_to(root).as_posix(), str(source))
PY
```

Extract only your own trusted backup archives into empty recovery volumes. Numeric ownership
preserves the default image's UID/GID 10001; bind mounts require the host's reviewed ownership
procedure. A mismatched auxiliary memory project scope may require restoring the original
workspace path; keep mount/configuration names with the backup.

Keep distributed Workers stopped while reviewing restored Run/Tool states. Once started, they can
claim queued work or recover after persisted leases expire; an old fencing generation does not
authorize a restored process. Local API startup can recover `WAITING_CHILD` parents, while a
`RUNNING` local root requires explicit resume. `WAITING_APPROVAL` requires the recorded approval
decision. Inspect ambiguous tool outcomes before any resume: checkpoint recovery cannot undo
external effects performed after the backup boundary. Cancel unwanted Runs through existing
controls, and start execution only against disposable workspaces and the fake provider during
the drill.

Validate Thread/Run/Turn associations, paged messages and raw events, an Artifact download hash,
and replay of a saved submission key with the original payload: replay must return its existing
Run without a new model execution. Inspect nonterminal behavior separately, then explicitly resume
one safe fake-provider Run. Restart and reread the same records. A database-only restore does not
prove a Compose/MinIO/workspace restore.

The offline regression `uv run pytest tests/test_artifacts.py -k stopped_sqlite_backup` exercises
SQLite's backup interface with WAL frames present, a local Blob and workspace, message/Run/tool
links, retained idempotency, and explicit recovery of a pending Run using a fake client. It does
not exercise the Docker or MinIO commands above. Run the existing real
[Compose harness](../benchmarks/deployment/README.md) when Docker is available; retain the source
SHA, environment and actual result, and mark any unexecuted service restore as unverified.

A separate isolated PostgreSQL 15.12 `pg_dump`/`pg_restore` drill also preserved a completed Run,
a pending Run, their messages and submission keys, Artifact metadata with local Blob SHA-256,
and a copied disposable workspace. The pending Run remained unexecuted. This is database plus
local-file recovery evidence; it does not establish MinIO recovery, a Compose deployment cycle,
provider networking, or a real external model request. Repeat it against the exact deployment
revision before relying on the backup procedure operationally.

## Upgrade and rollback

1. Complete the coordinated backup above and record the current source SHA and PostgreSQL schema
   version (current Runtime schema is v9). Keep the deployment stopped and preserve the backup.
2. Record/retain actual image IDs or archive the images outside the checkout. The default local
   application tags and upstream base tags are mutable; a source SHA alone cannot reproduce an
   older built image. `dc --profile sandbox-build config --images` lists image references without
   printing interpolated service credentials; resolve them with `docker image inspect` and retain
   the corresponding images, including the configured Sandbox image.
3. Check out an explicitly reviewed target SHA in a clean deployment checkout, rather than
   pulling a moving branch. With the same project/configuration, build the target application,
   Web and Sandbox images: `dc --profile sandbox-build build`.
4. Start storage, providerd and sandboxd, then `dc up -d runtime-api`. Runtime startup performs
   the additive migration and rejects unsupported future schemas. Check service health and logs
   without printing credentials. Keep Workers stopped until the migration and read checks pass.
5. Confirm authenticated API access, an existing Run, message/event pages and an Artifact hash.
   Start Workers and Web, then perform one authorized bounded smoke Run. Record whether its
   provider was fake or a real external model; `/health` and HTTP 401 do not prove model success.

Code/image rollback and data rollback are different operations. This repository does not promise
that an arbitrary older revision can read v9; verify compatibility in an isolated restore first.
If compatibility is unproven, restore the coordinated pre-upgrade database, Blob export, auxiliary
data and workspace into fresh volumes, use the saved images/configuration, and apply the restore
validation above. That rollback **loses writes after the backup boundary** and may not undo external
side effects. Retain the failed-upgrade state for diagnosis rather than overwriting it. There is
no destructive automatic down migration, and production rollback requires separate authorization.

## Security boundary

This is a trusted single-user, single-host deployment baseline with one Runtime API key. It has no
RBAC, tenant isolation, centralized secret manager, automated TLS, or HA database. Shell execution
uses a Docker container boundary, but filesystem/Web/MCP Tools still run in the Worker under their
existing Permission and policy controls.

sandboxd is a trusted privileged infrastructure component with Docker daemon authority. Compromise
of sandboxd is effectively compromise of the Docker host. The Sandbox does not protect against
kernel/container escape, a malicious trusted image, side channels, or hostile multi-tenant access.
It also has no hard workspace disk quota or controlled Shell egress.

providerd is a separate trusted component with external LLM credentials. Compromise of providerd can
expose those credentials or issue provider calls. It is not connected to the Sandbox network and
does not expose an Internet-facing listener. The gateway is not a tenant boundary, billing system,
quality router, or HA service.

MinIO is internal trusted storage. Artifact credentials remain in MinIO initialization, Runtime API,
and Worker processes only. Sandbox containers retain `network=none` and receive neither MinIO
credentials nor the `artifact-storage` network. Artifact retention follows the deployment volumes;
automatic orphan GC and per-Run retention are not implemented.

Claim provenance stores source IDs, bounded locators, and digests only. It adds no credentials to
Web, providerd, sandboxd, or Sandbox containers, and it does not grant Sandbox network access.

HTTP is suitable only for localhost or a trusted private network. For Internet-facing access,
terminate TLS in a separately managed reverse proxy or load balancer and restrict access there.
