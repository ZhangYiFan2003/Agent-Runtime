from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from axiom.config import AxiomConfig
from axiom.execution import SandboxExecutionBackend
from axiom.policy import DefaultPermissionPolicy
from axiom.tools import ToolRegistry, get_builtin_tools
from axiom.tools.base import ToolContext
from axiom.tools.executor import ToolExecutor

pytestmark = pytest.mark.docker


def _sandbox_url() -> str:
    value = os.environ.get("AXIOM_SANDBOXD_TEST_URL")
    if not value:
        pytest.skip("set AXIOM_SANDBOXD_TEST_URL to run live Docker sandbox tests")
    return value.rstrip("/")


def _bash_tool():
    return next(tool for tool in get_builtin_tools() if tool.name == "bash")


def test_live_tool_runtime_uses_non_root_networkless_sandbox(tmp_path):
    async def scenario():
        config = AxiomConfig()
        config.policy.hitl_mode = "never"
        config.execution.backend = "sandbox"
        config.execution.sandbox.controller_url = _sandbox_url()
        backend = SandboxExecutionBackend(
            tmp_path,
            controller_url=config.execution.sandbox.controller_url,
            allowed_env_names=config.execution.allowed_env_names,
            request_max_bytes=config.execution.sandbox.request_max_bytes,
            command_max_bytes=config.execution.sandbox.command_max_bytes,
        )
        registry = ToolRegistry()
        registry.register(_bash_tool())
        context = ToolContext(
            cwd=str(tmp_path),
            workspace=str(tmp_path),
            config=config,
            run_id="run-docker-tool-runtime",
            invocation_id="run-docker-tool-runtime:call-shell",
            permission_policy=DefaultPermissionPolicy(tmp_path, hitl_mode="never"),
            execution_backend=backend,
        )
        result = await ToolExecutor(registry, backend).execute_one(
            {
                "id": "call-shell",
                "name": "bash",
                "arguments": {
                    "command": (
                        "test \"$(id -u)\" = 10001 && "
                        "test ! -e /var/run/docker.sock && "
                        "test \"$(ls /sys/class/net)\" = lo && printf isolated"
                    )
                },
            },
            context,
        )
        assert not result.is_error, result.content
        assert result.content == "isolated"
        assert result.metadata["execution_backend"] == "sandbox"
        assert result.metadata["network_mode"] == "none"
        await backend.cleanup_run(context.run_id)

    asyncio.run(scenario())


def test_live_timeout_removes_container_and_next_command_recreates(tmp_path):
    async def scenario():
        url = _sandbox_url()
        backend = SandboxExecutionBackend(
            tmp_path,
            controller_url=url,
            allowed_env_names=[],
            request_max_bytes=64 * 1024,
            command_max_bytes=32 * 1024,
        )
        from axiom.execution import ExecutionRequest

        def request(command: str, timeout: float):
            return ExecutionRequest(
                command=command,
                cwd=str(tmp_path),
                workspace=str(tmp_path),
                timeout_seconds=timeout,
                stdout_limit_bytes=1024,
                stderr_limit_bytes=1024,
                run_id="run-docker-timeout",
                invocation_id="run-docker-timeout:call-shell",
            )

        timed_out = await backend.execute(request("sleep 3", 0.2))
        recovered = await backend.execute(request("printf recovered", 2.0))
        assert timed_out.timed_out
        assert timed_out.cleanup_method == "container_remove_timeout"
        assert recovered.stdout == "recovered"
        assert recovered.sandbox_id != timed_out.sandbox_id
        await backend.cleanup_run("run-docker-timeout")

        response = httpx.delete(f"{url}/v1/runs/run-docker-timeout", timeout=5.0)
        assert response.status_code == 200

    asyncio.run(scenario())
