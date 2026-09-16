from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from axiom.agent import QueryEngine
from axiom.config import AxiomConfig
from axiom.evaluation import (
    DurableEvaluationExecutor,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRunner,
    ScorerSpec,
)
from axiom.execution import (
    ExecutionRequest,
    ExecutionResult,
    LocalExecutionBackend,
    RestrictedExecutionBackend,
)
from axiom.policy import Capability
from axiom.runtime import (
    ActiveRunSupervisor,
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ObservabilityService,
    RunStatus,
    SpanStatus,
    SpanType,
    SQLiteCheckpointStore,
    ToolExecutionStatus,
    ToolRetryState,
)
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry, get_builtin_tools
from axiom.tools.base import Tool, ToolContext, ToolResult, object_schema
from axiom.tools.executor import ToolExecutor


class ToolLlm:
    provider_name = "execution-test"
    model_name = "execution-model"
    max_context_window = 10_000

    def __init__(
        self, tool_name: str, arguments: dict[str, object], final: str = "handled"
    ) -> None:
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


class RecordingBackend:
    name = "restricted"

    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.calls: list[ExecutionRequest] = []
        self.result = result

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls.append(request)
        return self.result or ExecutionResult(
            stdout="sandboxed",
            stderr="",
            exit_code=0,
            duration_ms=1.5,
            stdout_bytes=9,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            timeout_seconds=request.timeout_seconds,
            execution_backend=self.name,
            workspace=str(Path(request.workspace).resolve()),
            env_filtered_count=7,
        )


class BlockingBackend:
    name = "restricted"

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def execute(self, _request: ExecutionRequest) -> ExecutionResult:
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def _config(tmp_path, *, hitl: str = "auto") -> AxiomConfig:
    config = AxiomConfig()
    config.policy.hitl_mode = hitl
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    return config


def _python_command(source: str) -> str:
    arguments = [sys.executable, "-c", source]
    return subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)


def _request(tmp_path, command: str, **overrides) -> ExecutionRequest:
    values = {
        "command": command,
        "cwd": str(tmp_path),
        "workspace": str(tmp_path),
        "timeout_seconds": 2.0,
        "stdout_limit_bytes": 1024,
        "stderr_limit_bytes": 1024,
        "run_id": "run-execution",
        "invocation_id": "run-execution:call-shell",
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def _restricted(tmp_path, *, allowed_env_names=None) -> RestrictedExecutionBackend:
    config = AxiomConfig().execution
    return RestrictedExecutionBackend(
        tmp_path,
        allowed_env_names=allowed_env_names or config.allowed_env_names,
        termination_grace_seconds=0.2,
    )


def _registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(list(tools))
    return registry


def _builtin(name: str) -> Tool:
    return next(tool for tool in get_builtin_tools() if tool.name == name)


def _runtime(
    llm,
    registry,
    store,
    tmp_path,
    *,
    config=None,
    backend=None,
    observations=None,
    supervisor=None,
):
    return DurableAgentRuntime(
        llm_client=llm,
        tool_registry=registry,
        system_prompt="test",
        cwd=str(tmp_path),
        config=config or _config(tmp_path),
        store=store,
        execution_backend=backend,
        active_run_supervisor=supervisor,
        tracer=RunTracer(observations) if observations is not None else None,
    )


def test_restricted_backend_filters_host_secret_environment(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("AXIOM_TEST_SECRET", "synthetic-secret-value")
        result = await _restricted(tmp_path).execute(
            _request(
                tmp_path,
                _python_command("import os; print(os.environ.get('AXIOM_TEST_SECRET', 'missing'))"),
            )
        )

        assert result.stdout.strip() == "missing"
        assert "synthetic-secret-value" not in result.stdout
        assert result.env_filtered_count > 0

    asyncio.run(scenario())


def test_restricted_backend_allows_explicit_environment_name(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("AXIOM_TEST_ALLOWED", "allowed-test-value")
        allowed = [*AxiomConfig().execution.allowed_env_names, "AXIOM_TEST_ALLOWED"]
        result = await _restricted(tmp_path, allowed_env_names=allowed).execute(
            _request(
                tmp_path,
                _python_command("import os; print(os.environ.get('AXIOM_TEST_ALLOWED'))"),
            )
        )

        assert result.stdout.strip() == "allowed-test-value"

    asyncio.run(scenario())


def test_request_environment_cannot_bypass_allowlist(tmp_path):
    async def scenario():
        result = await _restricted(tmp_path).execute(
            _request(
                tmp_path,
                _python_command(
                    "import os; print(os.environ.get('AXIOM_TEST_OVERRIDE', 'missing'))"
                ),
                environment={"AXIOM_TEST_OVERRIDE": "synthetic-override-value"},
            )
        )

        assert result.stdout.strip() == "missing"
        assert "synthetic-override-value" not in result.stdout
        assert result.env_filtered_count > 0

    asyncio.run(scenario())


def test_local_backend_preserves_host_environment_for_compatibility(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("AXIOM_TEST_LOCAL_ENV", "local-test-value")
        result = await LocalExecutionBackend(termination_grace_seconds=0.2).execute(
            _request(
                tmp_path,
                _python_command("import os; print(os.environ.get('AXIOM_TEST_LOCAL_ENV'))"),
            )
        )

        assert result.stdout.strip() == "local-test-value"
        assert result.execution_backend == "local"
        assert result.env_filtered_count == 0

    asyncio.run(scenario())


def test_restricted_backend_uses_workspace_as_shell_cwd(tmp_path):
    async def scenario():
        probe = tmp_path / "cwd-probe.py"
        output = tmp_path / "cwd.txt"
        probe.write_text(
            "from pathlib import Path\n"
            "import os\n"
            "Path('cwd.txt').write_text(os.getcwd(), encoding='utf-8')\n",
            encoding="utf-8",
        )
        result = await _restricted(tmp_path).execute(
            _request(tmp_path, _python_command("exec(open('cwd-probe.py').read())"))
        )
        assert result.exit_code == 0
        assert Path(output.read_text(encoding="utf-8")).resolve() == tmp_path.resolve()
        assert result.workspace == str(tmp_path.resolve())

    asyncio.run(scenario())


def test_filesystem_tool_cannot_modify_outside_workspace(tmp_path):
    async def scenario():
        outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
        result = await ToolExecutor(_registry(_builtin("write_file"))).execute_one(
            {
                "id": "write-outside",
                "name": "write_file",
                "arguments": {"path": str(outside), "content": "blocked"},
            },
            ToolContext(
                cwd=str(tmp_path), workspace=str(tmp_path), config=_config(tmp_path, hitl="never")
            ),
        )

        assert result.is_error
        assert "outside workspace" in result.content
        assert not outside.exists()

    asyncio.run(scenario())


def test_shell_timeout_marks_tool_execution_unknown_and_terminates_process(tmp_path):
    async def scenario():
        config = _config(tmp_path, hitl="never")
        config.tools.timeout = 0.1
        store = MemoryCheckpointStore()
        command = _python_command("import time; time.sleep(10)")
        runtime = _runtime(
            ToolLlm("bash", {"command": command}),
            _registry(_builtin("bash")),
            store,
            tmp_path,
            config=config,
        )
        state = await runtime.start(
            thread_id="thread-timeout",
            input="timeout",
            run_id="run-timeout",
        )
        record = await store.load_tool_execution("run-timeout:call_bash")

        assert state.status == RunStatus.COMPLETED
        assert record is not None
        assert record.status == ToolExecutionStatus.UNKNOWN
        assert "timed out" in str(record.error)
        assert record.retry_suppressed_reason == "unsafe"
        assert record.attempt == 1

    asyncio.run(scenario())


def test_timeout_cleans_up_child_process_tree(tmp_path):
    async def scenario():
        marker = tmp_path / "child-survived.txt"
        child = (
            "import time; "
            "time.sleep(0.8); "
            f"open({str(marker)!r}, 'w', encoding='utf-8').write('alive')"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "time.sleep(10)"
        )
        result = await _restricted(tmp_path).execute(
            _request(
                tmp_path,
                _python_command(parent),
                timeout_seconds=0.15,
            )
        )
        await asyncio.sleep(1.0)

        assert result.timed_out
        assert result.cleanup_method in {
            "taskkill_tree",
            "job_object_kill",
            "process_group_break",
            "process_group_terminate",
            "process_group_kill",
        }
        assert not marker.exists()

    asyncio.run(scenario())


def test_async_cancellation_cleans_up_child_process_tree(tmp_path):
    async def scenario():
        marker = tmp_path / "cancelled-child-survived.txt"
        child = (
            "import time; "
            "time.sleep(0.8); "
            f"open({str(marker)!r}, 'w', encoding='utf-8').write('alive')"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "time.sleep(10)"
        )
        task = asyncio.create_task(
            _restricted(tmp_path).execute(
                _request(tmp_path, _python_command(parent), timeout_seconds=10)
            )
        )
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("execution task was not cancelled")
        await asyncio.sleep(1.0)

        assert not marker.exists()

    asyncio.run(scenario())


def test_stdout_is_streamed_with_a_bounded_result(tmp_path):
    async def scenario():
        result = await _restricted(tmp_path).execute(
            _request(
                tmp_path,
                _python_command("import sys; sys.stdout.write('x' * 4096)"),
                stdout_limit_bytes=128,
            )
        )

        assert len(result.stdout.encode()) == 128
        assert result.stdout_bytes == 4096
        assert result.stdout_truncated

    asyncio.run(scenario())


def test_stderr_is_streamed_with_a_bounded_result(tmp_path):
    async def scenario():
        result = await _restricted(tmp_path).execute(
            _request(
                tmp_path,
                _python_command("import sys; sys.stderr.write('e' * 4096)"),
                stderr_limit_bytes=96,
            )
        )

        assert len(result.stderr.encode()) == 96
        assert result.stderr_bytes == 4096
        assert result.stderr_truncated

    asyncio.run(scenario())


def test_approved_shell_runs_through_injected_backend_once(tmp_path):
    async def scenario():
        backend = RecordingBackend()
        runtime = _runtime(
            ToolLlm("bash", {"command": "echo sandbox"}),
            _registry(_builtin("bash")),
            MemoryCheckpointStore(),
            tmp_path,
            backend=backend,
        )
        waiting = await runtime.start(thread_id="thread-approved", input="shell")
        completed = await runtime.resume(waiting.run_id, decision="approve")

        assert waiting.status == RunStatus.WAITING_APPROVAL
        assert completed.status == RunStatus.COMPLETED
        assert len(backend.calls) == 1
        assert backend.calls[0].run_id == completed.run_id
        assert backend.calls[0].invocation_id == f"{completed.run_id}:call_bash"

    asyncio.run(scenario())


def test_rejected_shell_never_starts_execution_backend(tmp_path):
    async def scenario():
        backend = RecordingBackend()
        runtime = _runtime(
            ToolLlm("bash", {"command": "echo blocked"}),
            _registry(_builtin("bash")),
            MemoryCheckpointStore(),
            tmp_path,
            backend=backend,
        )
        waiting = await runtime.start(thread_id="thread-rejected", input="shell")
        completed = await runtime.resume(waiting.run_id, decision="reject")

        assert completed.status == RunStatus.COMPLETED
        assert backend.calls == []

    asyncio.run(scenario())


def test_restart_approval_executes_once_through_restricted_backend(tmp_path):
    async def scenario():
        database = tmp_path / "runtime.db"
        llm = ToolLlm("bash", {"command": "echo restarted"})
        first_backend = RecordingBackend()
        first = _runtime(
            llm,
            _registry(_builtin("bash")),
            SQLiteCheckpointStore(database),
            tmp_path,
            backend=first_backend,
        )
        waiting = await first.start(
            thread_id="thread-restart",
            input="shell",
            run_id="run-restart",
        )

        restarted_backend = RecordingBackend()
        restarted = _runtime(
            llm,
            _registry(_builtin("bash")),
            SQLiteCheckpointStore(database),
            tmp_path,
            backend=restarted_backend,
        )
        completed = await restarted.resume(waiting.run_id, decision="approve")

        assert completed.status == RunStatus.COMPLETED
        assert first_backend.calls == []
        assert len(restarted_backend.calls) == 1

    asyncio.run(scenario())


def test_execution_metadata_is_recorded_without_environment_values(tmp_path):
    async def scenario():
        synthetic_secret = "never-store-this-value"
        observations = MemoryObservabilityStore()
        backend = RecordingBackend()
        runtime = _runtime(
            ToolLlm("bash", {"command": "echo traced"}),
            _registry(_builtin("bash")),
            MemoryCheckpointStore(),
            tmp_path,
            backend=backend,
            observations=observations,
        )
        waiting = await runtime.start(thread_id="thread-trace", input="shell")
        completed = await runtime.resume(waiting.run_id, decision="approve")
        trace = await ObservabilityService(observations).trace(completed.run_id)
        tool_span = next(span for span in trace.spans if span.span_type == SpanType.TOOL)

        assert tool_span.attributes["execution_backend"] == "restricted"
        assert tool_span.attributes["exit_code"] == 0
        assert tool_span.attributes["env_filtered_count"] == 7
        assert tool_span.attributes["stdout_truncated"] is False
        assert synthetic_secret not in json.dumps(tool_span.attributes)

    asyncio.run(scenario())


def test_runtime_task_cancellation_updates_tool_record_and_span(tmp_path):
    async def scenario():
        backend = BlockingBackend()
        store = MemoryCheckpointStore()
        observations = MemoryObservabilityStore()
        runtime = _runtime(
            ToolLlm("bash", {"command": "echo cancelled"}),
            _registry(_builtin("bash")),
            store,
            tmp_path,
            config=_config(tmp_path, hitl="never"),
            backend=backend,
            observations=observations,
        )
        task = asyncio.create_task(
            runtime.start(
                thread_id="thread-cancelled",
                input="shell",
                run_id="run-cancelled",
            )
        )
        await asyncio.wait_for(backend.started.wait(), timeout=2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("runtime task was not cancelled")

        record = await store.load_tool_execution("run-cancelled:call_bash")
        trace = await ObservabilityService(observations).trace("run-cancelled")
        tool_span = next(span for span in trace.spans if span.span_type == SpanType.TOOL)
        assert record is not None
        assert record.status == ToolExecutionStatus.UNKNOWN
        assert record.retry_state == ToolRetryState.RETRY_SUPPRESSED
        assert record.retry_suppressed_reason == "unsafe"
        assert record.error == "tool execution cancelled"
        assert tool_span.status == SpanStatus.CANCELLED
        assert tool_span.attributes["cancelled"] is True

    asyncio.run(scenario())


def test_supervisor_cancellation_reaches_restricted_process_tree_cleanup(tmp_path):
    async def scenario():
        marker = tmp_path / "supervisor-child-survived.txt"
        child = (
            "import time; "
            "time.sleep(0.8); "
            f"open({str(marker)!r}, 'w', encoding='utf-8').write('alive')"
        )
        parent = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "time.sleep(10)"
        )
        store = MemoryCheckpointStore()
        supervisor = ActiveRunSupervisor()
        runtime = _runtime(
            ToolLlm("bash", {"command": _python_command(parent), "timeout": 10}),
            _registry(_builtin("bash")),
            store,
            tmp_path,
            config=_config(tmp_path, hitl="never"),
            backend=_restricted(tmp_path),
            supervisor=supervisor,
        )
        task = asyncio.create_task(
            runtime.start(
                thread_id="thread-supervisor-process",
                input="shell",
                run_id="run-supervisor-process",
            )
        )
        for _attempt in range(200):
            record = await store.load_tool_execution("run-supervisor-process:call_bash")
            if record is not None and record.status == ToolExecutionStatus.RUNNING:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("subprocess tool did not start")

        signal_result = supervisor.request_cancel("run-supervisor-process")
        state = await asyncio.wait_for(task, timeout=3)
        await asyncio.sleep(1.0)
        record = await store.load_tool_execution("run-supervisor-process:call_bash")

        assert signal_result.status == "signalled"
        assert state.status == RunStatus.CANCELLED
        assert record is not None
        assert record.status == ToolExecutionStatus.UNKNOWN
        assert record.retry_state == ToolRetryState.RETRY_SUPPRESSED
        assert record.retry_suppressed_reason == "unsafe"
        assert record.error == "tool execution cancelled"
        assert not marker.exists()
        assert supervisor.list_active() == ()

    asyncio.run(scenario())


def test_existing_read_only_evaluation_path_still_passes(tmp_path):
    async def scenario():
        async def read(_payload, _context):
            return ToolResult("isolation smoke")

        tool = Tool(
            name="read",
            description="read",
            parameters=object_schema({"path": {"type": "string"}}, ["path"]),
            required_keys=["path"],
            handler=read,
            capabilities=(Capability.FILESYSTEM_READ.value,),
            path_argument_names=("path",),
        )
        executor = DurableEvaluationExecutor(
            engine_factory=lambda _case: QueryEngine(
                llm_client=ToolLlm(
                    "read",
                    {"path": "inside.txt"},
                    final="isolation smoke",
                ),
                tool_registry=_registry(tool),
                config=_config(tmp_path),
                cwd=str(tmp_path),
            ),
            checkpoint_store=MemoryCheckpointStore(),
            observability_store=MemoryObservabilityStore(),
        )
        case = EvaluationCase(
            id="isolation-smoke",
            prompt="read",
            scorers=(
                ScorerSpec(type="run_status"),
                ScorerSpec(type="contains", config={"expected": "isolation smoke"}),
            ),
        )
        suite = await EvaluationRunner(executor).run(
            EvaluationDataset(name="isolation", version="1", cases=(case,))
        )

        assert suite.results[0].passed

    asyncio.run(scenario())
