from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Any

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.runtime import (
    AssignmentStatus,
    Checkpoint,
    ControlOperationName,
    MultiAgentState,
    MultiAgentStatus,
    RunStatus,
    SQLiteControlOperationStore,
    WorkerAssignment,
)
from axiom.runtime.api import RuntimeApiServer, RuntimeTurnContext
from axiom.runtime.models import Interrupt
from axiom.tools import ToolRegistry
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema


class FakeRequest:
    def __init__(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> None:
        body = json.dumps(payload or {}).encode()
        self.command = method
        self.path = path
        self.headers = {
            "content-length": str(len(body)),
            "x-api-key": "test-key",
        }
        if idempotency_key:
            self.headers["Idempotency-Key"] = idempotency_key
        self.rfile = BytesIO(body)
        self.wfile = BytesIO()
        self.status = 0
        self.response_headers: dict[str, str] = {}

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name.lower()] = value

    def end_headers(self) -> None:
        return

    def json(self) -> dict[str, Any]:
        return json.loads(self.wfile.getvalue())


class StaticClient:
    provider_name = "api-test"
    model_name = "api-model"
    max_context_window = 10_000

    def __init__(self, *, delay: float = 0.0) -> None:
        self.calls = 0
        self.delay = delay

    async def chat(self, _messages, _tools, *, system_prompt):
        del system_prompt
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        yield {"type": "text_delta", "text": "recovered"}
        yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ToolClient(StaticClient):
    def __init__(self, tool_name: str = "dangerous") -> None:
        super().__init__()
        self.tool_name = tool_name

    async def chat(self, messages, _tools, *, system_prompt):
        del system_prompt
        self.calls += 1
        if not any(message.role == "tool" for message in messages):
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call-dangerous",
                    "function": {
                        "name": self.tool_name,
                        "arguments": json.dumps({"value": "x"}),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        yield {"type": "text_delta", "text": "finished"}
        yield {"type": "usage", "usage": {"input_tokens": 1, "output_tokens": 1}}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class TeamToolClient(ToolClient):
    async def chat(self, messages, tools, *, system_prompt):
        if "Planner in a multi-agent workflow" in system_prompt:
            self.calls += 1
            yield {
                "type": "text_delta",
                "text": json.dumps(
                    {
                        "steps": [
                            {
                                "id": "a",
                                "description": "A",
                                "type": "worker",
                                "dependencies": [],
                            }
                        ]
                    }
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "You are the Reviewer" in system_prompt:
            self.calls += 1
            yield {"type": "text_delta", "text": '{"approved": true, "issues": []}'}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        async for event in super().chat(messages, tools, system_prompt=system_prompt):
            yield event


def _server(tmp_path, *, client=None, data_dir=None, team: bool = False):
    config = AxiomConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    if team:
        config.prompt.agent_mode = "team"
    client = client or StaticClient()
    registry = ToolRegistry()

    def factory(context: RuntimeTurnContext) -> QueryEngine:
        return QueryEngine(
            llm_client=client,
            tool_registry=registry,
            config=context.config,
            cwd=context.cwd,
        )

    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=config,
        api_key="test-key",
        workers=0,
        data_dir=data_dir or tmp_path / "runtime",
        engine_factory=factory,
    )
    return server, registry


def _dangerous_tool(calls: list[str], *, delay: float = 0.0) -> Tool:
    async def execute(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
        if delay:
            await asyncio.sleep(delay)
        calls.append(str(payload.get("value")))
        return ToolResult("tool-ok")

    return Tool(
        name="dangerous",
        description="dangerous test tool",
        parameters=object_schema({"value": {"type": "string"}}, ["value"]),
        required_keys=["value"],
        handler=execute,
        requires_approval=True,
        capabilities=("external.side_effect",),
    )


def _thread(server: RuntimeApiServer) -> str:
    return server.repository.create_thread()


def _checkpoint(
    thread_id: str,
    run_id: str,
    *,
    strategy: str = "react",
    status: RunStatus = RunStatus.RUNNING,
    turn_id: str = "turn-control",
) -> Checkpoint:
    state = Checkpoint.create(
        thread_id=thread_id,
        turn_id=turn_id,
        run_id=run_id,
        input="control",
        execution_strategy=strategy,
    )
    state.status = status
    return state


def _save(server: RuntimeApiServer, *states: Checkpoint) -> None:
    async def save_all() -> None:
        for state in states:
            await server.checkpoint_store.save(state)

    asyncio.run(save_all())


def _handle(server: RuntimeApiServer, request: FakeRequest) -> tuple[int, dict[str, Any]]:
    server._handle(request)
    return request.status, request.json()


def _multi_parent(
    thread_id: str,
    assignments: list[WorkerAssignment],
    *,
    status: RunStatus = RunStatus.WAITING_CHILD,
    run_id: str = "run-parent",
) -> Checkpoint:
    state = _checkpoint(thread_id, run_id, strategy="multi_agent", status=status)
    state.run_kind = "orchestrator"
    orchestration = MultiAgentState(
        orchestration_goal="goal",
        status=MultiAgentStatus.WAITING_CHILD,
        assignments=assignments,
        current_assignment_id=assignments[0].assignment_id if assignments else None,
    )
    state.strategy_state["multi_agent"] = orchestration.to_dict()
    return state


def _child(
    parent: Checkpoint,
    run_id: str,
    status: RunStatus,
    *,
    interrupt: bool = False,
) -> Checkpoint:
    state = _checkpoint(parent.thread_id, run_id, status=status, turn_id=parent.turn_id)
    state.parent_run_id = parent.run_id
    state.parent_step_id = f"span-{run_id}"
    state.run_kind = "worker"
    if interrupt:
        state.interrupt = Interrupt(
            kind="tool_approval",
            reason="approval required",
            invocation_id=f"{run_id}:call",
            tool_name="dangerous",
            arguments={"secret": "must-not-leak"},
        )
    return state


def _start_approval_run(server: RuntimeApiServer, registry: ToolRegistry) -> dict[str, Any]:
    calls = getattr(server, "_test_tool_calls", None)
    if calls is None:
        calls = []
        server._test_tool_calls = calls
    registry.register(_dangerous_tool(calls))
    thread_id = _thread(server)
    status, result = _handle(
        server,
        FakeRequest("POST", f"/v1/threads/{thread_id}/turns", {"message": "approve"}),
    )
    assert status == 202
    return result


def test_run_query_returns_stable_react_representation(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    state = _checkpoint(thread_id, "run-react", status=RunStatus.COMPLETED)
    state.output_text = "done"
    _save(server, state)

    status, result = _handle(server, FakeRequest("GET", "/v1/runs/run-react"))

    assert status == 200
    assert result["execution_strategy"] == "react"
    assert result["run_kind"] == "agent"
    assert result["completed_at"]
    assert "strategy_state" not in result


def test_run_query_returns_plan_strategy(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    _save(server, _checkpoint(thread_id, "run-plan", strategy="plan_execute"))

    _, result = _handle(server, FakeRequest("GET", "/v1/runs/run-plan"))

    assert result["execution_strategy"] == "plan_execute"
    assert result["recovery_action"] == "client_resume"


def test_run_query_returns_multi_agent_parent_counts(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignment = WorkerAssignment("a", "worker", "A", child_run_id="run-child")
    parent = _multi_parent(thread_id, [assignment])
    child = _child(parent, "run-child", RunStatus.WAITING_APPROVAL, interrupt=True)
    _save(server, parent, child)

    _, result = _handle(server, FakeRequest("GET", f"/v1/runs/{parent.run_id}"))

    assert result["children_count"] == 1
    assert result["active_child_run_ids"] == [child.run_id]
    assert result["waiting_children_count"] == 1


def test_parent_children_endpoint_is_sorted_and_array_shaped(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignments = [
        WorkerAssignment("a", "researcher", "A", child_run_id="run-b"),
        WorkerAssignment("b", "writer", "B", child_run_id="run-a"),
    ]
    parent = _multi_parent(thread_id, assignments)
    first = _child(parent, "run-b", RunStatus.RUNNING)
    second = _child(parent, "run-a", RunStatus.COMPLETED)
    _save(server, parent, first, second)

    _, result = _handle(server, FakeRequest("GET", f"/v1/runs/{parent.run_id}/children"))

    assert isinstance(result["children"], list)
    assert {item["child_run_id"] for item in result["children"]} == {"run-a", "run-b"}
    assert {item["assignment_id"] for item in result["children"]} == {"a", "b"}


def test_child_run_query_exposes_parent_linkage(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignment = WorkerAssignment("a", "worker", "A", child_run_id="run-child")
    parent = _multi_parent(thread_id, [assignment])
    child = _child(parent, "run-child", RunStatus.RUNNING)
    _save(server, parent, child)

    _, result = _handle(server, FakeRequest("GET", "/v1/runs/run-child"))
    _, children = _handle(server, FakeRequest("GET", "/v1/runs/run-child/children"))

    assert result["parent_run_id"] == parent.run_id
    assert result["assignment"]["assignment_id"] == "a"
    assert children["children"] == []


def test_duplicate_resume_with_same_idempotency_key_advances_once(tmp_path):
    client = StaticClient()
    server, _ = _server(tmp_path, client=client)
    thread_id = _thread(server)
    state = _checkpoint(thread_id, "run-resume", status=RunStatus.INTERRUPTED)
    state.interrupt = Interrupt(kind="manual", reason="pause")
    _save(server, state)

    def request() -> FakeRequest:
        return FakeRequest("POST", "/v1/runs/run-resume/resume", idempotency_key="resume-once")

    first = _handle(server, request())
    second = _handle(server, request())

    assert first == second
    assert first[0] == 200
    assert client.calls == 1


def test_approval_response_loss_restart_and_retry_does_not_repeat_tool(tmp_path):
    calls: list[str] = []
    client = ToolClient()
    data_dir = tmp_path / "restart-idempotency"
    first, registry = _server(tmp_path, client=client, data_dir=data_dir)
    first._test_tool_calls = calls
    started = _start_approval_run(first, registry)
    state = asyncio.run(first.checkpoint_store.load(started["run_id"]))
    invocation_id = state.interrupt.invocation_id
    approve = FakeRequest(
        "POST",
        f"/v1/runs/{state.run_id}/resume",
        {"decision": "approve", "invocation_id": invocation_id},
        idempotency_key="approve-restart",
    )
    first._handle(approve)  # response intentionally discarded

    second, registry2 = _server(tmp_path, client=client, data_dir=data_dir)
    registry2.register(_dangerous_tool(calls))
    status, result = _handle(
        second,
        FakeRequest(
            "POST",
            f"/v1/runs/{state.run_id}/resume",
            {"decision": "approve", "invocation_id": invocation_id},
            idempotency_key="approve-restart",
        ),
    )

    assert status == 200
    assert result == approve.json()
    assert calls == ["x"]


def test_idempotency_key_reuse_with_different_payload_conflicts(tmp_path):
    client = ToolClient()
    server, registry = _server(tmp_path, client=client)
    started = _start_approval_run(server, registry)
    state = asyncio.run(server.checkpoint_store.load(started["run_id"]))
    payload = {"decision": "approve", "invocation_id": state.interrupt.invocation_id}
    _handle(
        server,
        FakeRequest("POST", f"/v1/runs/{state.run_id}/resume", payload, idempotency_key="same-key"),
    )

    status, result = _handle(
        server,
        FakeRequest(
            "POST",
            f"/v1/runs/{state.run_id}/resume",
            {**payload, "decision": "reject"},
            idempotency_key="same-key",
        ),
    )

    assert status == 409
    assert result["error"]["code"] == "idempotency_key_conflict"


def test_duplicate_cancel_is_idempotent(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    _save(server, _checkpoint(thread_id, "run-cancel"))

    first = _handle(
        server,
        FakeRequest("POST", "/v1/runs/run-cancel/cancel", idempotency_key="cancel-once"),
    )
    second = _handle(
        server,
        FakeRequest("POST", "/v1/runs/run-cancel/cancel", idempotency_key="cancel-once"),
    )

    assert first == second
    assert first[1]["status"] == RunStatus.CANCELLED


def test_resume_completed_returns_transition_conflict(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    _save(server, _checkpoint(thread_id, "run-complete", status=RunStatus.COMPLETED))

    status, result = _handle(server, FakeRequest("POST", "/v1/runs/run-complete/resume"))

    assert status == 409
    assert result["error"]["code"] == "invalid_run_transition"
    assert result["error"]["status"] == "COMPLETED"


def test_resume_cancelled_returns_transition_conflict(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    _save(server, _checkpoint(thread_id, "run-cancelled", status=RunStatus.CANCELLED))

    status, result = _handle(server, FakeRequest("POST", "/v1/runs/run-cancelled/resume"))

    assert status == 409
    assert result["error"]["status"] == "CANCELLED"


def test_approve_after_reject_reports_interrupt_already_resolved(tmp_path):
    client = ToolClient()
    server, registry = _server(tmp_path, client=client)
    started = _start_approval_run(server, registry)
    state = asyncio.run(server.checkpoint_store.load(started["run_id"]))
    invocation_id = state.interrupt.invocation_id
    _handle(
        server,
        FakeRequest(
            "POST",
            f"/v1/runs/{state.run_id}/resume",
            {"decision": "reject", "invocation_id": invocation_id},
            idempotency_key="reject-first",
        ),
    )

    status, result = _handle(
        server,
        FakeRequest(
            "POST",
            f"/v1/runs/{state.run_id}/resume",
            {"decision": "approve", "invocation_id": invocation_id},
            idempotency_key="approve-late",
        ),
    )

    assert status == 409
    assert result["error"]["code"] == "interrupt_already_resolved"


def test_cancel_completed_is_stable_conflict(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    _save(server, _checkpoint(thread_id, "run-done", status=RunStatus.COMPLETED))

    first = _handle(server, FakeRequest("POST", "/v1/runs/run-done/cancel"))
    second = _handle(server, FakeRequest("POST", "/v1/runs/run-done/cancel"))

    assert first[0] == second[0] == 409
    assert first[1]["error"]["code"] == second[1]["error"]["code"]


def test_two_concurrent_resume_requests_advance_at_most_once(tmp_path):
    client = StaticClient(delay=0.05)
    server, _ = _server(tmp_path, client=client)
    thread_id = _thread(server)
    state = _checkpoint(thread_id, "run-concurrent", status=RunStatus.INTERRUPTED)
    state.interrupt = Interrupt(kind="manual", reason="pause")
    _save(server, state)

    def resume(key: str):
        return _handle(
            server,
            FakeRequest("POST", "/v1/runs/run-concurrent/resume", idempotency_key=key),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(resume, ["resume-a", "resume-b"]))

    assert any(status == 200 for status, _ in results)
    assert all(status in {200, 409} for status, _ in results)
    assert client.calls == 1


def test_resume_cancel_race_leaves_legal_terminal_state(tmp_path):
    client = StaticClient(delay=0.05)
    server, _ = _server(tmp_path, client=client)
    thread_id = _thread(server)
    state = _checkpoint(thread_id, "run-race", status=RunStatus.INTERRUPTED)
    state.interrupt = Interrupt(kind="manual", reason="pause")
    _save(server, state)

    requests = [
        FakeRequest("POST", "/v1/runs/run-race/resume", idempotency_key="race-resume"),
        FakeRequest("POST", "/v1/runs/run-race/cancel", idempotency_key="race-cancel"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda req: _handle(server, req), requests))
    final = asyncio.run(server.checkpoint_store.load("run-race"))

    assert all(status in {200, 409} for status, _ in results)
    assert final.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}


def test_two_concurrent_approvals_execute_tool_once(tmp_path):
    calls: list[str] = []
    client = ToolClient()
    server, registry = _server(tmp_path, client=client)
    server._test_tool_calls = calls
    registry.register(_dangerous_tool(calls, delay=0.05))
    thread_id = _thread(server)
    _, started = _handle(
        server,
        FakeRequest("POST", f"/v1/threads/{thread_id}/turns", {"message": "approve"}),
    )
    state = asyncio.run(server.checkpoint_store.load(started["run_id"]))
    payload = {"decision": "approve", "invocation_id": state.interrupt.invocation_id}

    def approve():
        return _handle(
            server,
            FakeRequest(
                "POST",
                f"/v1/runs/{state.run_id}/resume",
                payload,
                idempotency_key="approve-concurrent",
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _item: approve(), range(2)))

    assert all(status in {200, 409} for status, _ in results)
    assert calls == ["x"]


def test_parent_waiting_child_lists_waiting_child(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignment = WorkerAssignment("a", "worker", "A", child_run_id="run-waiting")
    parent = _multi_parent(thread_id, [assignment])
    child = _child(parent, "run-waiting", RunStatus.WAITING_APPROVAL, interrupt=True)
    _save(server, parent, child)

    _, result = _handle(server, FakeRequest("GET", f"/v1/runs/{parent.run_id}/children"))

    assert result["children"][0]["status"] == "WAITING_APPROVAL"
    assert result["children"][0]["interrupt"]["invocation_id"]


def test_parent_pending_interrupts_aggregate_child_without_arguments(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignment = WorkerAssignment("a", "worker", "A", child_run_id="run-approval")
    parent = _multi_parent(thread_id, [assignment])
    child = _child(parent, "run-approval", RunStatus.WAITING_APPROVAL, interrupt=True)
    _save(server, parent, child)

    _, result = _handle(
        server,
        FakeRequest("GET", f"/v1/runs/{parent.run_id}/interrupts"),
    )

    assert result["pending_interrupts"][0]["run_id"] == child.run_id
    assert "arguments" not in result["pending_interrupts"][0]
    assert "must-not-leak" not in json.dumps(result)


def test_approve_child_automatically_advances_parent(tmp_path):
    calls: list[str] = []
    client = TeamToolClient()
    server, registry = _server(tmp_path, client=client, team=True)
    server._test_tool_calls = calls
    registry.register(_dangerous_tool(calls))
    thread_id = _thread(server)
    status, waiting = _handle(
        server,
        FakeRequest("POST", f"/v1/threads/{thread_id}/turns", {"message": "team goal"}),
    )
    child_id = waiting["child_run_id"]
    child = asyncio.run(server.checkpoint_store.load(child_id))

    approve_status, result = _handle(
        server,
        FakeRequest(
            "POST",
            f"/v1/runs/{child_id}/resume",
            {"decision": "approve", "invocation_id": child.interrupt.invocation_id},
            idempotency_key="child-approve",
        ),
    )
    parent = asyncio.run(server.checkpoint_store.load(waiting["run_id"]))

    assert status == 202
    assert approve_status == 200
    assert result["child_run_id"] == child_id
    assert parent.status == RunStatus.COMPLETED
    assert calls == ["x"]


def test_cancel_parent_cascades_all_non_terminal_children(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignments = [
        WorkerAssignment("a", "worker", "A", child_run_id="run-a"),
        WorkerAssignment("b", "worker", "B", child_run_id="run-b"),
        WorkerAssignment("c", "worker", "C", child_run_id="run-c"),
    ]
    parent = _multi_parent(thread_id, assignments)
    children = [
        _child(parent, "run-a", RunStatus.RUNNING),
        _child(parent, "run-b", RunStatus.WAITING_APPROVAL, interrupt=True),
        _child(parent, "run-c", RunStatus.COMPLETED),
    ]
    _save(server, parent, *children)

    status, result = _handle(
        server,
        FakeRequest("POST", f"/v1/runs/{parent.run_id}/cancel", idempotency_key="parent-cancel"),
    )
    restored = [asyncio.run(server.checkpoint_store.load(child.run_id)) for child in children]

    assert status == 200
    assert result["status"] == "CANCELLED"
    assert [child.status for child in restored] == [
        RunStatus.CANCELLED,
        RunStatus.CANCELLED,
        RunStatus.COMPLETED,
    ]


def test_cancel_child_does_not_cancel_parent(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignment = WorkerAssignment("a", "worker", "A", child_run_id="run-child-cancel")
    parent = _multi_parent(thread_id, [assignment])
    child = _child(parent, "run-child-cancel", RunStatus.RUNNING)
    _save(server, parent, child)

    _handle(server, FakeRequest("POST", f"/v1/runs/{child.run_id}/cancel"))
    restored_parent = asyncio.run(server.checkpoint_store.load(parent.run_id))

    assert restored_parent.status == RunStatus.WAITING_CHILD


def test_restart_preserves_waiting_approval_without_auto_decision(tmp_path):
    data_dir = tmp_path / "approval-restart"
    first, _ = _server(tmp_path, data_dir=data_dir)
    thread_id = _thread(first)
    waiting = _checkpoint(thread_id, "run-waiting-restart", status=RunStatus.WAITING_APPROVAL)
    waiting.interrupt = Interrupt(
        kind="tool_approval",
        reason="approval",
        invocation_id="invocation-1",
        tool_name="dangerous",
    )
    _save(first, waiting)

    second, _ = _server(tmp_path, data_dir=data_dir)
    asyncio.run(second._recover_waiting_parents())
    restored = asyncio.run(second.checkpoint_store.load(waiting.run_id))

    assert restored.status == RunStatus.WAITING_APPROVAL
    assert restored.interrupt.invocation_id == "invocation-1"


def test_restart_recovers_waiting_parent_with_terminal_child(tmp_path):
    client = TeamToolClient()
    data_dir = tmp_path / "parent-recovery"
    first, _ = _server(tmp_path, client=client, data_dir=data_dir, team=True)
    thread_id = _thread(first)
    assignment = WorkerAssignment(
        "a",
        "worker",
        "A",
        status=AssignmentStatus.WAITING_CHILD,
        child_run_id="run-terminal-child",
    )
    parent = _multi_parent(thread_id, [assignment], run_id="run-recover-parent")
    child = _child(parent, "run-terminal-child", RunStatus.COMPLETED)
    child.output_text = "child result"
    _save(first, parent, child)

    second, _ = _server(tmp_path, client=client, data_dir=data_dir, team=True)
    asyncio.run(second._recover_waiting_parents())
    restored = asyncio.run(second.checkpoint_store.load(parent.run_id))

    assert restored.status == RunStatus.COMPLETED
    assert "child result" in restored.output_text


def test_control_operation_record_persists_across_sqlite_instances(tmp_path):
    path = tmp_path / "operations.db"
    first = SQLiteControlOperationStore(path)
    record, created = first.begin(
        run_id="run-1",
        idempotency_key="key-1",
        operation=ControlOperationName.CANCEL,
        request={},
    )
    first.complete(record.operation_id, {"status": "CANCELLED"})

    second = SQLiteControlOperationStore(path)
    restored = second.lookup("run-1", "key-1")

    assert created
    assert restored.result == {"status": "CANCELLED"}


def test_parent_and_child_sse_events_have_hierarchy_fields(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    assignment = WorkerAssignment("a", "worker", "A", child_run_id="run-sse-child")
    parent = _multi_parent(thread_id, [assignment])
    child = _child(parent, "run-sse-child", RunStatus.RUNNING)
    _save(server, parent, child)
    sink = server._runtime_event_sink(thread_id)
    asyncio.run(sink("worker.started", {"run_id": child.run_id}))

    request = FakeRequest("GET", f"/v1/threads/{thread_id}/events")
    server._handle(request)
    data = _sse_data(request.wfile.getvalue().decode())[-1]

    assert data["event_id"]
    assert data["thread_id"] == thread_id
    assert data["turn_id"] == parent.turn_id
    assert data["run_id"] == child.run_id
    assert data["parent_run_id"] == parent.run_id
    assert data["parent_step_id"] == child.parent_step_id
    assert data["assignment_id"] == "a"
    assert data["event_type"] == "worker.started"
    assert data["timestamp"]


def test_sse_after_id_replay_has_no_duplicates_or_gaps(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    for index in range(4):
        server.repository.append_event(
            thread_id,
            "step",
            {"run_id": "run-replay", "index": index},
        )
    all_request = FakeRequest("GET", f"/v1/threads/{thread_id}/events")
    server._handle(all_request)
    all_data = _sse_data(all_request.wfile.getvalue().decode())
    cursor = all_data[1]["event_id"]

    after = FakeRequest("GET", f"/v1/threads/{thread_id}/events?after_id={cursor}")
    server._handle(after)
    replay = _sse_data(after.wfile.getvalue().decode())

    assert [item["event_id"] for item in replay] == [
        item["event_id"] for item in all_data if item["event_id"] > cursor
    ]


def test_sse_run_filter_keeps_thread_wide_replay_compatible(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    server.repository.append_event(thread_id, "step", {"run_id": "run-a"})
    server.repository.append_event(thread_id, "step", {"run_id": "run-b"})
    filtered = FakeRequest("GET", f"/v1/threads/{thread_id}/events?run_id=run-b")
    server._handle(filtered)
    thread_wide = FakeRequest("GET", f"/v1/threads/{thread_id}/events")
    server._handle(thread_wide)

    assert {item["run_id"] for item in _sse_data(filtered.wfile.getvalue().decode())} == {"run-b"}
    assert {item["run_id"] for item in _sse_data(thread_wide.wfile.getvalue().decode())} >= {
        "run-a",
        "run-b",
    }


def test_run_model_represents_future_parallel_child_states(tmp_path):
    server, _ = _server(tmp_path)
    thread_id = _thread(server)
    statuses = [
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    ]
    assignments = [
        WorkerAssignment(str(index), "worker", str(index), child_run_id=f"run-{index}")
        for index in range(4)
    ]
    parent = _multi_parent(thread_id, assignments)
    children = [
        _child(parent, f"run-{index}", status, interrupt=status == RunStatus.WAITING_APPROVAL)
        for index, status in enumerate(statuses)
    ]
    _save(server, parent, *children)

    _, result = _handle(server, FakeRequest("GET", f"/v1/runs/{parent.run_id}"))

    assert result["children_count"] == 4
    assert result["active_children_count"] == 2
    assert result["terminal_children_count"] == 2
    assert len(result["pending_interrupts"]) == 1


def _sse_data(body: str) -> list[dict[str, Any]]:
    result = []
    for block in body.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                result.append(json.loads(line.removeprefix("data: ")))
    return result
