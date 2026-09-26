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
  |
  v
PostgreSQL (durable Run, Event, control, ownership authority)
  ^
  |
Worker replicas (claim, heartbeat, fenced execution)
  |
  v
sandboxd (trusted Docker controller)
  |
  v
per-Run Sandbox containers (network none)
```

The API and Workers share a single-host `runtime_data` volume only for the existing SQLite-backed
observability, memory, and local task support. PostgreSQL remains authoritative for distributed
Runs. Only `web` publishes a host port; Runtime API, Workers, and PostgreSQL stay on the Compose
network.

Workers reach sandboxd only on the private `sandbox-control` network. Only sandboxd mounts the
Docker socket. Runtime API, Web, PostgreSQL, Workers, and dynamically created Sandbox containers do
not receive Docker daemon access. Sandbox containers join no Compose network.

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
for startup and health checks, but real Agent Turns require a configured provider key.

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
Workers fail closed if sandboxd, Docker, or the configured Sandbox image is unavailable.

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

## Upgrade

```bash
git pull
docker compose --env-file deploy/.env -f deploy/compose.yaml --profile sandbox-build build
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

Back up the PostgreSQL database independently before upgrades. Docker volumes are persistence,
not backups.

## Security boundary

This is a trusted single-user, single-host deployment baseline with one Runtime API key. It has no
RBAC, tenant isolation, centralized secret manager, automated TLS, or HA database. Shell execution
uses a Docker container boundary, but filesystem/Web/MCP Tools still run in the Worker under their
existing Permission and policy controls.

sandboxd is a trusted privileged infrastructure component with Docker daemon authority. Compromise
of sandboxd is effectively compromise of the Docker host. The Sandbox does not protect against
kernel/container escape, a malicious trusted image, side channels, or hostile multi-tenant access.
It also has no hard workspace disk quota or controlled Shell egress.

HTTP is suitable only for localhost or a trusted private network. For Internet-facing access,
terminate TLS in a separately managed reverse proxy or load balancer and restrict access there.
