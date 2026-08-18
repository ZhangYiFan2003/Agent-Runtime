from __future__ import annotations

import asyncio
import json

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.evaluation import (
    DurableEvaluationExecutor,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunner,
    ScorerSpec,
)
from axiom.policy import (
    Capability,
    DefaultPermissionPolicy,
    PermissionAction,
    PermissionDecision,
    PermissionRequest,
)
from axiom.runtime import (
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RunStatus,
    SpanType,
    SQLiteCheckpointStore,
)
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry, get_builtin_tools
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema
from axiom.tools.executor import ToolExecutor


class ToolLlm:
    provider_name = "permission-test"
    model_name = "permission-model"
    max_context_window = 10_000

    def __init__(self, tool_name: str, arguments: dict[str, object], final: str = "done") -> None:
        self.tool_name = tool_name
        self.arguments = arguments
        self.final = final

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        if not any(message.role == "tool" for message in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": f"call_{self.tool_name}",
                    "function": {
                        "name": self.tool_name,
                        "arguments": json.dumps(self.arguments),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": self.final}
        yield {"type": "usage", "usage": {"input_tokens": 2, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _config(tmp_path) -> AxiomConfig:
    config = AxiomConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return config


def _tool(name, handler, capability: str, *, path: bool = False) -> Tool:
    properties = {"path": {"type": "string"}} if path else {"value": {"type": "string"}}
    key = "path" if path else "value"
    return Tool(
        name=name,
        description=name,
        parameters=object_schema(properties, [key]),
        required_keys=[key],
        handler=handler,
        is_read_only=capability
        in {
            Capability.FILESYSTEM_READ.value,
            Capability.NETWORK_READ.value,
        },
        capabilities=(capability,),
        path_argument_names=("path",) if path else (),
    )


def _registry(tool: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(tool)
    return registry


def _runtime(llm, registry, store, tmp_path, *, events=None, observations=None):
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=registry,
        system_prompt="test",
        cwd=str(tmp_path),
        config=_config(tmp_path),
        store=store,
        event_sink=(lambda name, payload: events.append((name, payload)))
        if events is not None
        else None,
        tracer=RunTracer(observations) if observations is not None else None,
    )


def test_workspace_read_is_allowed_and_tool_executes(tmp_path):
    async def scenario():
        (tmp_path / "inside.txt").write_text("workspace content", encoding="utf-8")
        tool = next(tool for tool in get_builtin_tools() if tool.name == "read_file")
        result = await ToolExecutor(_registry(tool)).execute_one(
            {
                "id": "read-1",
                "name": "read_file",
                "arguments": {"path": "inside.txt"},
            },
            ToolContext(cwd=str(tmp_path), config=_config(tmp_path)),
        )

        assert not result.is_error
        assert "workspace content" in result.content

    asyncio.run(scenario())


def test_write_requires_durable_approval(tmp_path):
    async def scenario():
        executions = 0

        async def write(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult("written")

        tool = _tool("write", write, Capability.FILESYSTEM_WRITE.value, path=True)
        runtime = _runtime(
            ToolLlm("write", {"path": "inside.txt"}),
            _registry(tool),
            MemoryCheckpointStore(),
            tmp_path,
        )
        state = await runtime.start(thread_id="thread-write", input="write")

        assert state.status == RunStatus.WAITING_APPROVAL
        assert state.interrupt is not None
        assert state.interrupt.kind == "tool_approval"
        assert executions == 0

    asyncio.run(scenario())


def test_approval_executes_the_invocation_exactly_once_without_loop(tmp_path):
    async def scenario():
        executions = 0

        async def write(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult("written")

        events = []
        tool = _tool("write", write, Capability.FILESYSTEM_WRITE.value, path=True)
        runtime = _runtime(
            ToolLlm("write", {"path": "inside.txt"}),
            _registry(tool),
            MemoryCheckpointStore(),
            tmp_path,
            events=events,
        )
        waiting = await runtime.start(thread_id="thread-approve", input="write")
        completed = await runtime.resume(waiting.run_id, decision="approve")

        decisions = [payload["decision"] for name, payload in events if name == "policy.decision"]
        assert completed.status == RunStatus.COMPLETED
        assert decisions == ["require_approval", "allow"]
        assert executions == 1

    asyncio.run(scenario())


def test_approval_survives_sqlite_runtime_restart(tmp_path):
    async def scenario():
        executions = 0

        async def write(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult("written")

        database = tmp_path / "runtime.db"
        tool = _tool("write", write, Capability.FILESYSTEM_WRITE.value, path=True)
        llm = ToolLlm("write", {"path": "inside.txt"})
        first = _runtime(llm, _registry(tool), SQLiteCheckpointStore(database), tmp_path)
        waiting = await first.start(thread_id="thread-restart", input="write", run_id="run-restart")

        restarted = _runtime(llm, _registry(tool), SQLiteCheckpointStore(database), tmp_path)
        completed = await restarted.resume(waiting.run_id, decision="approve")

        assert completed.status == RunStatus.COMPLETED
        assert executions == 1

    asyncio.run(scenario())


def test_invocation_approval_does_not_override_a_hard_deny(tmp_path):
    class DenyPolicy:
        async def evaluate(self, _request):
            return PermissionDecision(
                PermissionAction.DENY,
                "policy changed to deny",
                "test.hard_deny",
            )

    async def scenario():
        executions = 0

        async def write(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult("written")

        tool = _tool("write", write, Capability.FILESYSTEM_WRITE.value, path=True)
        runtime = _runtime(
            ToolLlm("write", {"path": "inside.txt"}),
            _registry(tool),
            MemoryCheckpointStore(),
            tmp_path,
        )
        waiting = await runtime.start(thread_id="thread-hard-deny", input="write")
        runtime.permission_policy = DenyPolicy()
        completed = await runtime.resume(waiting.run_id, decision="approve")

        tool_messages = [str(item.content) for item in completed.messages if item.role == "tool"]
        assert any("policy changed to deny" in message for message in tool_messages)
        assert executions == 0

    asyncio.run(scenario())


def test_reject_never_executes_tool_and_agent_receives_result(tmp_path):
    async def scenario():
        executions = 0

        async def write(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult("written")

        tool = _tool("write", write, Capability.FILESYSTEM_WRITE.value, path=True)
        runtime = _runtime(
            ToolLlm("write", {"path": "inside.txt"}, final="handled"),
            _registry(tool),
            MemoryCheckpointStore(),
            tmp_path,
        )
        waiting = await runtime.start(thread_id="thread-reject", input="write")
        completed = await runtime.resume(waiting.run_id, decision="reject")

        tool_messages = [str(item.content) for item in completed.messages if item.role == "tool"]
        assert completed.output_text == "handled"
        assert any("rejected" in message for message in tool_messages)
        assert executions == 0

    asyncio.run(scenario())


def test_outside_workspace_is_denied_without_execution(tmp_path):
    async def scenario():
        executions = 0

        async def read(_payload, _context):
            nonlocal executions
            executions += 1
            return ToolResult("secret")

        tool = _tool("read", read, Capability.FILESYSTEM_READ.value, path=True)
        runtime = _runtime(
            ToolLlm("read", {"path": "../outside.txt"}, final="cannot read"),
            _registry(tool),
            MemoryCheckpointStore(),
            tmp_path,
        )
        completed = await runtime.start(thread_id="thread-deny", input="read")

        tool_messages = [str(item.content) for item in completed.messages if item.role == "tool"]
        assert completed.status == RunStatus.COMPLETED
        assert any("outside workspace" in message for message in tool_messages)
        assert executions == 0

    asyncio.run(scenario())


def test_unknown_capability_is_denied():
    async def scenario():
        request = PermissionRequest(
            run_id="run",
            thread_id="thread",
            turn_id="turn",
            invocation_id="invocation",
            tool_name="unknown",
            capabilities=("future.root_access",),
            arguments={},
            workspace=".",
            cwd=".",
        )
        decision = await DefaultPermissionPolicy(".").evaluate(request)
        assert decision.action == PermissionAction.DENY
        assert decision.matched_rule == "capability.unknown"

    asyncio.run(scenario())


def test_policy_decision_enters_event_and_trace_pipeline(tmp_path):
    async def scenario():
        async def read(_payload, _context):
            return ToolResult("read")

        events = []
        observations = MemoryObservabilityStore()
        tool = _tool("read", read, Capability.FILESYSTEM_READ.value, path=True)
        state = await _runtime(
            ToolLlm("read", {"path": "inside.txt"}),
            _registry(tool),
            MemoryCheckpointStore(),
            tmp_path,
            events=events,
            observations=observations,
        ).start(thread_id="thread-audit", input="read")
        trace = await ObservabilityService(observations).trace(state.run_id)

        event = next(payload for name, payload in events if name == "policy.decision")
        policy_span = next(span for span in trace.spans if span.span_type == SpanType.POLICY)
        assert event["decision"] == "allow"
        assert event["capabilities"] == ["filesystem.read"]
        assert policy_span.attributes["invocation_id"] == event["invocation_id"]

    asyncio.run(scenario())


def test_shell_capability_requires_approval(tmp_path):
    async def scenario():
        request = PermissionRequest(
            run_id="run",
            thread_id="thread",
            turn_id="turn",
            invocation_id="shell-1",
            tool_name="shell",
            capabilities=(Capability.SHELL_EXECUTE.value,),
            arguments={"command": "echo ok"},
            workspace=str(tmp_path),
            cwd=str(tmp_path),
        )
        decision = await DefaultPermissionPolicy(tmp_path).evaluate(request)
        assert decision.action == PermissionAction.REQUIRE_APPROVAL

    asyncio.run(scenario())


def test_read_only_evaluation_still_uses_real_runtime_and_passes(tmp_path):
    async def scenario():
        async def read(_payload, _context):
            return ToolResult("permission smoke")

        tool = _tool("read", read, Capability.FILESYSTEM_READ.value, path=True)
        executor = DurableEvaluationExecutor(
            engine_factory=lambda _case: QueryEngine(
                llm_client=ToolLlm("read", {"path": "inside.txt"}, final="permission smoke"),
                tool_registry=_registry(tool),
                config=_config(tmp_path),
                cwd=str(tmp_path),
            ),
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=MemoryObservabilityStore(),
        )
        case = EvaluationCase(
            id="permission-smoke",
            prompt="read",
            scorers=(
                ScorerSpec(type="run_status"),
                ScorerSpec(type="contains", config={"expected": "permission smoke"}),
                ScorerSpec(type="tool_usage", config={"required_tools": ["read"]}),
            ),
        )
        suite = await EvaluationRunner(executor).run(
            EvaluationDataset(name="permission", version="1", cases=(case,))
        )

        assert suite.results[0].passed

    asyncio.run(scenario())
