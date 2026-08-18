# Capability-Based Permission Policy

Axiom evaluates a structured permission request before a Tool handler executes. Permission Policy
v1 is an authorization layer for the single-machine Runtime. It is not an operating-system security
boundary and does not replace a filesystem, process, or network sandbox.

## Capability model

Built-in Tools and MCP adapters declare only capabilities used by the current Runtime:

```text
filesystem.read
filesystem.write
shell.execute
network.read
network.write
external.side_effect
```

`Tool.capabilities` describes the kind of access requested. Filesystem Tools additionally declare
`path_argument_names`, allowing the policy to validate relevant arguments without hard-coding Tool
names in the Runtime. A legacy Tool with no capability metadata retains its previous behavior:
`requires_approval=True` requires approval, while other legacy Tools are allowed. An explicitly
unknown capability is denied rather than silently trusted.

## Request and decision

Each decision receives a `PermissionRequest` containing the Run, Thread, Turn, invocation, Tool,
capabilities, arguments, workspace, current working directory, and declared resource-path inputs.
The asynchronous `PermissionPolicy` protocol returns a `PermissionDecision`:

```text
ALLOW             execute the exact invocation
DENY              do not execute; return a policy error to the Agent
REQUIRE_APPROVAL  checkpoint and enter WAITING_APPROVAL
```

The decision also carries a human-readable reason and an optional matched-rule identifier.
`DefaultPermissionPolicy` implements the v1 rules:

| Capability / condition | Decision in `hitl_mode=auto` |
| --- | --- |
| `filesystem.read` inside the workspace | `ALLOW` |
| `network.read` | `ALLOW` |
| `filesystem.write` inside the workspace | `REQUIRE_APPROVAL` |
| `shell.execute` | `REQUIRE_APPROVAL` |
| `network.write` | `REQUIRE_APPROVAL` |
| `external.side_effect` | `REQUIRE_APPROVAL` |
| filesystem path outside the workspace | `DENY` |
| unknown capability | `DENY` |

`hitl_mode=always` requires approval for otherwise valid Tool calls. The explicit compatibility
setting `hitl_mode=never` converts approval-required decisions to `ALLOW`, but it does not bypass an
outside-workspace or unknown-capability `DENY`.

## Durable approval flow

```text
Tool call
  ↓
PermissionRequest → REQUIRE_APPROVAL
  ↓
checkpoint → WAITING_APPROVAL
  ↓
optional process restart
  ↓
resume(approve | reject)
  ↓
decision bound to invocation_id
  ↓
execute once, or return rejection to the Agent
```

Approval is scoped to the exact persisted `invocation_id`; it is not a global grant. After an
approved resume, the ToolExecutor accepts only a preauthorization carrying that same invocation ID.
This prevents the call from entering another approval interrupt while preserving ToolExecution
deduplication. Rejection is persisted as a failed ToolExecution result and the handler is never run.

The Durable Runtime owns checkpoint/interrupt behavior because it has the Run state. ToolExecutor
remains the final enforcement point for non-durable ReAct and MCP server paths; when it does not
receive a matching Durable preauthorization, it evaluates the policy itself.

## Audit and observability

Every Durable permission evaluation emits a structured `policy.decision` Runtime event containing:

```text
run_id, thread_id, turn_id, invocation_id, tool_name,
capabilities, decision, reason, matched_rule
```

The same evaluation is persisted as a `policy` Span when Run tracing is enabled. Arguments are not
copied into the policy event or Span. Existing Tool audit logging continues to redact sensitive
argument keys.

## Workspace scope and security limitations

Filesystem paths are expanded, normalized, resolved, and compared with the canonical workspace
root. Relative paths, absolute paths, `..`, and existing symlinks are therefore checked against the
same containment rule. This validation is still not a filesystem sandbox: it cannot prevent all
symlink races or time-of-check/time-of-use changes, and an allowed shell command can access resources
beyond what a Python path check can express.

Permission Policy does not provide process isolation, syscall filtering, network egress control,
secret isolation, or resource limits. Runtime idempotency also cannot guarantee exactly-once effects
in an external service; Tool/provider idempotency support remains necessary. MCP capability mapping
uses adapter-side conservative categories, but a remote MCP server and its `readOnlyHint` remain a
trust boundary rather than an enforceable OS guarantee.
