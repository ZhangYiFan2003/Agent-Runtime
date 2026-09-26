# Execution Isolation

Axiom routes subprocess-like Tools through an `ExecutionBackend` after Permission Policy has
authorized the invocation. The default `RestrictedExecutionBackend` reduces accidental host
exposure with workspace validation, environment filtering, bounded output, wall-clock timeout, and
best-effort process-tree cleanup.

> **`RestrictedExecutionBackend` is not an OS sandbox.**

> **Network policy is application-level; it is not a firewall or OS network namespace.**

> **Shell commands may still access the host filesystem outside cwd unless an OS or container
> boundary enforces otherwise.**

## Execution backends

```text
Permission Policy
    ↓
ToolExecutor
    ↓
ExecutionBackend
├── LocalExecutionBackend
├── RestrictedExecutionBackend
└── SandboxExecutionBackend
    ↓ narrow internal API
  sandboxd
    ↓ Docker Engine
  per-Run container
```

`ExecutionRequest` contains the command, Run/invocation identity, cwd, workspace, timeout, output
limits, and optional explicitly supplied environment. `ExecutionResult` returns separate stdout and
stderr, exit code, byte counts, truncation, timeout, duration, backend, filtered-variable count, and
cleanup method.

`LocalExecutionBackend` preserves the complete host environment for explicit compatibility use. It
still applies timeout, bounded streaming output, and process cleanup. `RestrictedExecutionBackend`
is the default and additionally validates cwd and filters the inherited environment.

`SandboxExecutionBackend` is opt-in and fail-closed. It maps the existing `ExecutionRequest` to
the narrow sandboxd API; it never talks to Docker directly and never falls back to a local backend.
sandboxd creates one container per Runtime `run_id`, reuses it for sequential Shell calls in the
same Run, and uses Docker labels to rediscover it after a controller restart. Child Runs have their
own `run_id` and therefore their own container.

| Backend | Process boundary | Host filesystem boundary | Shell network | Resource limits |
| --- | --- | --- | --- | --- |
| `local` | none | none | host network | time/output only |
| `restricted` | subprocess group | application-level cwd checks | host network | time/output only |
| `sandbox` | Docker container per Run | container rootfs except workspace | `none` | CPU, memory, PIDs, time, output |

Configuration is intentionally small:

```json
{
  "execution": {
    "backend": "restricted",
    "stdout_limit_bytes": 20000,
    "stderr_limit_bytes": 20000,
    "termination_grace_seconds": 1.0,
    "allowed_env_names": ["PATH", "SYSTEMROOT", "TEMP", "LANG"]
  }
}
```

`AXIOM_EXECUTION_BACKEND=local|restricted|sandbox` and the comma-separated
`AXIOM_EXECUTION_ALLOWED_ENV` provide environment-based overrides. The latter contains variable
names, never values. Sandbox controller, fixed image, workspace source, CPU, memory, PID, and tmpfs
settings use the existing `AXIOM_SANDBOX_*` environment merge path. Agent requests cannot override
the image, mounts, network mode, capabilities, devices, or resource policy.

## Workspace model

The v1 workspace is the configured Agent/Runtime cwd. Axiom does not copy the repository into a
per-Run directory because the current Agent is expected to inspect and edit that real workspace.

Restricted subprocess cwd and built-in filesystem Tools use the same canonical `PathGuard`
containment logic. Relative paths, absolute paths, `..`, `~`, and existing symlinks are resolved
before containment comparison. Built-in `read_file`, `write_file`, directory, glob, and grep paths
therefore cannot intentionally escape the configured workspace through their path arguments.

This is validation and application-level enforcement, not a filesystem jail. Once an approved
Shell starts, cwd alone cannot prevent commands such as reading an absolute host path. Symlink races
and other time-of-check/time-of-use changes also remain possible.

The Docker backend exposes only `/workspace` from the dedicated deployment workspace. Its root
filesystem is read-only and `/tmp` is a bounded tmpfs. In the single-user v1 deployment, different
Run containers share the same workspace volume so edits remain visible and survive container
replacement. Process/root-filesystem isolation is per Run; filesystem tenant isolation and a hard
workspace disk quota are not implemented.

## Network policy

Network-capable Tools are checked by the same Permission Policy before execution. The policy accepts
only `http` and `https`, rejects credentials embedded in URLs, and supports `public`, `allowlist`,
and `disabled` access modes. Public mode rejects loopback, private, link-local, multicast, and
reserved IP targets after DNS resolution. Allowlist mode permits an exact host or subdomain of a
configured host rule, then applies the same address checks. `web_fetch` validates every redirect
target rather than trusting only the initial URL.

This is SSRF risk reduction, not a perfect DNS-rebinding-proof egress boundary. MCP HTTP servers
remain static Runtime configuration outside this Web URL policy rather than model-selected endpoints;
they are integrations, not automatically trusted content. Their returned Tools and responses still
pass through the ordinary Tool permission path. MCP stdio uses the filtered environment, while its
transport process lifecycle remains owned by the SDK.

Sandbox Shell containers use Docker `network_mode=none`. They cannot directly reach the Internet,
Runtime API, PostgreSQL, Web, or sandboxd. Network-capable Axiom operations remain separate Web/MCP
Tools governed by Permission and Network Policy. Stage 11 does not provide controlled Shell egress.

## Environment filtering

Restricted execution starts from an empty environment and copies only configured names. Defaults
are the portable/Windows variables needed for process startup and locale handling:

```text
PATH, PATHEXT, SYSTEMROOT, WINDIR, COMSPEC,
TEMP, TMP, TMPDIR, LANG, LC_ALL, LC_CTYPE
```

Variables such as provider API keys, cloud credentials, GitHub tokens, database URLs, and arbitrary
host application state are not inherited unless their names are explicitly allowlisted. Axiom does
not inspect `.env` or infer safety from values. Traces record only `env_filtered_count`; environment
values are never copied into execution attributes.

The sandbox client applies the same allowlist before serialization. sandboxd validates names,
counts, and value sizes and supplies only a small fixed base environment plus permitted values.
Provider keys, Runtime API keys, PostgreSQL credentials, Docker variables, and controller state are
not inherited by Sandbox containers.

## Timeout, cancellation, and process cleanup

Subprocesses are created in an isolated process group/session where the platform permits it.

- **POSIX:** a new session/process group is created. Timeout or task cancellation sends `SIGTERM` to
  the group, waits for the configured grace period, then uses `SIGKILL`. Remaining background group
  members are also cleaned when the command's shell exits.
- **Windows:** Axiom attempts to assign the shell process to a Job Object configured with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Closing the Job terminates descendants. If Job assignment is
  unavailable, Axiom falls back to process-group break, `taskkill /T /F`, and finally parent-process
  kill. The final fallback cannot guarantee descendant cleanup.

An `asyncio` cancellation reaches the backend, triggers cleanup, and is re-raised. Durable Runtime
then persists the ToolExecution as failed and marks the Tool Span `CANCELLED`. Evaluation timeouts
therefore clean up an active Shell. The current threaded HTTP `POST /cancel` path updates persisted
Run state but does not yet deliver cross-thread cancellation to an already executing subprocess;
this remains an explicit limitation.

For the Docker backend, timeout or cancellation force-removes the per-Run container, killing the
active exec and descendants. The next command recreates the container while retaining the external
workspace. `COMPLETED`, `FAILED`, and `CANCELLED` transitions request idempotent container removal.
Cleanup failure is side-cleanup evidence and does not rewrite a durable Run outcome.

## Output and resource limits

stdout and stderr are drained concurrently in bounded chunks. Axiom retains at most the configured
byte limit for each stream while continuing to drain discarded bytes, so an active process cannot
grow Runtime memory without bound through captured output. Results and Tool spans expose:

```text
stdout_bytes, stderr_bytes,
stdout_truncated, stderr_truncated,
timed_out, exit_code, duration_ms
```

Wall-clock timeout and output retention apply to every backend. The Docker backend additionally
configures cgroup CPU/memory limits and a PID limit. Memory termination is reported when Docker
reliably exposes `OOMKilled`; otherwise Axiom reports a generic sandbox termination. Open-file and
workspace disk quotas are not implemented.

## Permission and observability integration

```text
ALLOW / approved invocation
    ↓
restricted backend
    ↓
ToolExecution + Tool Span + Runtime events
```

`DENY` and rejected approvals never call the backend. An approved invocation remains bound to its
persisted `invocation_id`, then executes through the injected backend exactly once under the existing
ToolExecution deduplication rules.

Shell Tool spans and `tool.started`/`tool.completed`/`tool.failed` events include safe execution
metadata: backend, workspace, timeout, exit code, duration, byte counts, truncation, timeout,
cancellation, cleanup method, and filtered-variable count. Commands and output continue to follow
the existing Tool event/result behavior; environment values are never emitted by the backend.

MCP stdio servers are spawned by the MCP SDK rather than by `ExecutionBackend`, but Axiom applies
the same filtered host-environment construction before starting them. Values explicitly declared in
an MCP server specification remain an intentional configuration override. Process-tree supervision
for those transport processes remains owned by the SDK.

CLI maintenance subprocesses and subprocesses created inside third-party libraries are trusted
application operations and are not routed through the Tool execution backend.

## Docker socket and ownership boundaries

```text
PostgreSQL lease/fencing = who may execute a Run
Docker Sandbox           = where approved Shell code executes
```

Only sandboxd mounts `/var/run/docker.sock`. Workers, Runtime API, Web, PostgreSQL, and dynamic
Sandbox containers do not. sandboxd is a trusted privileged infrastructure component: compromise
of sandboxd is effectively compromise of the Docker host. Its narrow API is reachable only from
Workers on the internal `sandbox-control` network and is not a generic Docker proxy.

Worker takeover continues to use PostgreSQL ownership. A replacement Worker can rediscover the
existing Run container through labels; it does not create a second lease system. If sandboxd
restarts during an active exec, that invocation may become failed/UNKNOWN and the existing durable
ToolExecution retry-suppression rules remain authoritative.

## Threat model

The Docker backend is intended to contain buggy or malicious approved Shell commands, including
host-path traversal, environment-secret discovery, direct internal-service access, fork/process
explosion, memory exhaustion, and orphaned descendants.

It does not defend against Docker daemon compromise, kernel/container escape, a malicious trusted
sandbox image, side channels, hostile multi-tenant workspace sharing, or incomplete host secret
management. It is container isolation, not VM isolation.

## Security limitations

Local and restricted execution do not provide:

- a full host filesystem jail;
- syscall filtering, seccomp, AppArmor, or SELinux;
- a network namespace or egress proxy (the URL policy is not a firewall);
- container, VM, or WebAssembly isolation;
- reliable CPU/memory quotas across platforms;
- hardened multi-tenant code execution;
- protection from an approved command exploiting the host kernel or available executables.

Use `RestrictedExecutionBackend` as risk reduction for a trusted single-user local Runtime. Use the
Docker backend when a real container boundary is required, while retaining Permission Policy as the
authorization layer and the limitations above.
