# Execution Isolation / Sandbox-lite v1

Axiom routes subprocess-like Tools through an `ExecutionBackend` after Permission Policy has
authorized the invocation. The default `RestrictedExecutionBackend` reduces accidental host
exposure with workspace validation, environment filtering, bounded output, wall-clock timeout, and
best-effort process-tree cleanup.

> **This is not a complete OS sandbox.**

> **Network isolation is not enforced.**

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
└── RestrictedExecutionBackend
    ↓
subprocess lifecycle
```

`ExecutionRequest` contains the command, Run/invocation identity, cwd, workspace, timeout, output
limits, and optional explicitly supplied environment. `ExecutionResult` returns separate stdout and
stderr, exit code, byte counts, truncation, timeout, duration, backend, filtered-variable count, and
cleanup method.

`LocalExecutionBackend` preserves the complete host environment for explicit compatibility use. It
still applies timeout, bounded streaming output, and process cleanup. `RestrictedExecutionBackend`
is the default and additionally validates cwd and filters the inherited environment.

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

`AXIOM_EXECUTION_BACKEND=local|restricted` and the comma-separated
`AXIOM_EXECUTION_ALLOWED_ENV` provide environment-based overrides. The latter contains variable
names, never values.

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

## Output and resource limits

stdout and stderr are drained concurrently in bounded chunks. Axiom retains at most the configured
byte limit for each stream while continuing to drain discarded bytes, so an active process cannot
grow Runtime memory without bound through captured output. Results and Tool spans expose:

```text
stdout_bytes, stderr_bytes,
stdout_truncated, stderr_truncated,
timed_out, exit_code, duration_ms
```

Wall-clock timeout and output retention are portable v1 controls. CPU, memory, process-count, open
file, and file-size limits are not enforced portably. POSIX `setrlimit`, Windows Job memory/CPU
limits, and container-level quotas are deliberately deferred rather than presented as equivalent
cross-platform guarantees.

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

## Security limitations

Sandbox-lite v1 does not provide:

- a full host filesystem jail;
- syscall filtering, seccomp, AppArmor, or SELinux;
- a network namespace, egress proxy, or network denial;
- container, VM, or WebAssembly isolation;
- reliable CPU/memory quotas across platforms;
- hardened multi-tenant code execution;
- protection from an approved command exploiting the host kernel or available executables.

Use `RestrictedExecutionBackend` as risk reduction for a trusted single-user local Runtime, not as a
security boundary for hostile multi-tenant workloads.
