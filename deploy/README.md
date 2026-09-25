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
```

The API and Workers share a single-host `runtime_data` volume only for the existing SQLite-backed
observability, memory, and local task support. PostgreSQL remains authoritative for distributed
Runs. Only `web` publishes a host port; Runtime API, Workers, and PostgreSQL stay on the Compose
network.

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
to Axiom but is not an OS sandbox.

## Start

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
docker compose --env-file deploy/.env -f deploy/compose.yaml ps
```

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

## Stop and restart

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml down
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

Do not add `-v` unless you intentionally want to delete PostgreSQL and Runtime volumes.

## Upgrade

```bash
git pull
docker compose --env-file deploy/.env -f deploy/compose.yaml build
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d
```

Back up the PostgreSQL database independently before upgrades. Docker volumes are persistence,
not backups.

## Security boundary

This is a trusted single-user, single-host deployment baseline with one Runtime API key. It has no
RBAC, tenant isolation, centralized secret manager, automated TLS, HA database, or OS-level Tool
sandbox. `RestrictedExecutionBackend` reduces accidental exposure but is not a security boundary
for hostile code. Do not expose a shell-capable Runtime to arbitrary untrusted users.

HTTP is suitable only for localhost or a trusted private network. For Internet-facing access,
terminate TLS in a separately managed reverse proxy or load balancer and restrict access there.
