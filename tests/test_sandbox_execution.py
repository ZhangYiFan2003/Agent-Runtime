from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest

from axiom.config import AxiomConfig, SandboxConfig, load_config
from axiom.execution import (
    ExecutionRequest,
    SandboxControllerError,
    SandboxExecutionBackend,
    create_execution_backend,
)
from axiom.sandbox.controller import (
    DockerEngineClient,
    SandboxApiError,
    SandboxController,
    SandboxExecuteRequest,
    SandboxExecuteResult,
)


def _request(tmp_path, **overrides) -> ExecutionRequest:
    values = {
        "command": "printf sandbox",
        "cwd": str(tmp_path),
        "workspace": str(tmp_path),
        "timeout_seconds": 2.0,
        "stdout_limit_bytes": 100,
        "stderr_limit_bytes": 100,
        "run_id": "run-a",
        "invocation_id": "run-a:call-shell",
        "environment": {},
    }
    values.update(overrides)
    return ExecutionRequest(**values)


def _controller_payload(**overrides):
    values = {
        "run_id": "run-a",
        "invocation_id": "run-a:call-shell",
        "command": "printf sandbox",
        "cwd": "/workspace",
        "timeout_seconds": 2.0,
        "stdout_limit_bytes": 100,
        "stderr_limit_bytes": 100,
        "environment": {"LANG": "C.UTF-8"},
    }
    values.update(overrides)
    return values


class _AsyncClient:
    responses: list[httpx.Response | BaseException] = []
    calls: list[tuple[str, str, object]] = []

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **_kwargs):
        return self._next("GET", url, None)

    async def post(self, url, json=None, **_kwargs):
        return self._next("POST", url, json)

    async def delete(self, url, **_kwargs):
        return self._next("DELETE", url, None)

    @classmethod
    def _next(cls, method, url, body):
        cls.calls.append((method, url, body))
        response = cls.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _Engine:
    def __init__(self):
        self.executions: list[SandboxExecuteRequest] = []
        self.removed: list[str] = []

    def health(self):
        return None

    def execute(self, request):
        self.executions.append(request)
        return SandboxExecuteResult(
            stdout="sandbox",
            stderr="",
            exit_code=0,
            duration_ms=1.0,
            stdout_bytes=7,
            stderr_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            cleanup_method=None,
            sandbox_id=f"box-{request.run_id}",
            resource_limits={"cpus": 1.0, "memory_bytes": 1024, "pids": 8},
        )

    def remove_run(self, run_id):
        self.removed.append(run_id)
        return True


def _response(status: int, payload: object) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "http://sandboxd/v1/execute"),
    )


def test_create_execution_backend_selects_sandbox(tmp_path):
    config = AxiomConfig()
    config.execution.backend = "sandbox"

    backend = create_execution_backend(config, tmp_path)

    assert isinstance(backend, SandboxExecutionBackend)
    assert backend.controller_url == "http://sandboxd:8090"


def test_sandbox_config_env_and_validation(tmp_path):
    config = load_config(
        project_root=tmp_path,
        env={
            "AXIOM_EXECUTION_BACKEND": "sandbox",
            "AXIOM_SANDBOX_CONTROLLER_URL": "http://controller:9999",
            "AXIOM_SANDBOX_CPU_LIMIT": "2.5",
            "AXIOM_SANDBOX_MEMORY_LIMIT_BYTES": "1048576",
            "AXIOM_SANDBOX_PIDS_LIMIT": "32",
        },
    )
    assert config.execution.backend == "sandbox"
    assert config.execution.sandbox.controller_url == "http://controller:9999"
    assert config.execution.sandbox.cpu_limit == 2.5
    assert config.execution.sandbox.memory_limit_bytes == 1048576
    assert config.execution.sandbox.pids_limit == 32

    with pytest.raises(ValueError, match="sandbox limits"):
        load_config(
            project_root=tmp_path,
            overrides={"execution": {"sandbox": {"pids_limit": 0}}},
            env={},
        )


def test_backend_serializes_safe_request_and_maps_result(tmp_path, monkeypatch):
    _AsyncClient.calls = []
    _AsyncClient.responses = [
        _response(
            200,
            {
                "stdout": "ok",
                "stderr": "warn",
                "exit_code": 0,
                "duration_ms": 2.5,
                "stdout_bytes": 2,
                "stderr_bytes": 4,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "timed_out": False,
                "cleanup_method": None,
                "sandbox_id": "abc123",
                "resource_limits": {"cpus": 1.0},
                "network_mode": "none",
            },
        )
    ]
    monkeypatch.setattr("axiom.execution.backends.httpx.AsyncClient", _AsyncClient)
    monkeypatch.setenv("AXIOM_TEST_SECRET", "must-not-pass")
    backend = SandboxExecutionBackend(
        tmp_path,
        controller_url="http://sandboxd:8090",
        allowed_env_names=["LANG"],
        request_max_bytes=4096,
        command_max_bytes=1024,
    )

    result = asyncio.run(
        backend.execute(
            _request(
                tmp_path,
                environment={"LANG": "C.UTF-8", "AXIOM_TEST_SECRET": "fake"},
            )
        )
    )

    body = _AsyncClient.calls[0][2]
    assert isinstance(body, dict)
    assert body["run_id"] == "run-a"
    assert body["cwd"] == "/workspace"
    assert body["environment"] == {"LANG": "C.UTF-8"}
    assert result.execution_backend == "sandbox"
    assert result.sandbox_id == "abc123"
    assert result.network_mode == "none"
    assert result.env_filtered_count > 0


def test_backend_fails_closed_when_controller_is_unavailable(tmp_path, monkeypatch):
    request = httpx.Request("POST", "http://sandboxd/v1/execute")
    _AsyncClient.responses = [httpx.ConnectError("offline", request=request)]
    _AsyncClient.calls = []
    monkeypatch.setattr("axiom.execution.backends.httpx.AsyncClient", _AsyncClient)
    backend = SandboxExecutionBackend(
        tmp_path,
        controller_url="http://sandboxd:8090",
        allowed_env_names=[],
        request_max_bytes=4096,
        command_max_bytes=1024,
    )

    with pytest.raises(SandboxControllerError) as failure:
        asyncio.run(backend.execute(_request(tmp_path)))

    assert failure.value.code == "SANDBOX_UNAVAILABLE"


def test_backend_rejects_invalid_controller_response(tmp_path, monkeypatch):
    _AsyncClient.responses = [_response(200, {"stdout": "not enough fields"})]
    _AsyncClient.calls = []
    monkeypatch.setattr("axiom.execution.backends.httpx.AsyncClient", _AsyncClient)
    backend = SandboxExecutionBackend(
        tmp_path,
        controller_url="http://sandboxd:8090",
        allowed_env_names=[],
        request_max_bytes=4096,
        command_max_bytes=1024,
    )

    with pytest.raises(SandboxControllerError, match="incomplete"):
        asyncio.run(backend.execute(_request(tmp_path)))


def test_backend_rejects_cwd_escape_before_controller_call(tmp_path):
    backend = SandboxExecutionBackend(
        tmp_path,
        controller_url="http://sandboxd:8090",
        allowed_env_names=[],
        request_max_bytes=4096,
        command_max_bytes=1024,
    )

    with pytest.raises(SandboxControllerError) as failure:
        asyncio.run(backend.execute(_request(tmp_path, cwd=str(tmp_path.parent))))

    assert failure.value.code == "SANDBOX_INVALID_CWD"


def test_controller_validates_requests_and_uses_run_identity():
    engine = _Engine()
    controller = SandboxController(engine, SandboxConfig())

    first = controller.execute(_controller_payload(run_id="run-a"))
    second = controller.execute(_controller_payload(run_id="run-a", command="printf again"))
    other = controller.execute(_controller_payload(run_id="run-b"))

    assert first["sandbox_id"] == second["sandbox_id"] == "box-run-a"
    assert other["sandbox_id"] == "box-run-b"
    assert [item.run_id for item in engine.executions] == ["run-a", "run-a", "run-b"]
    with pytest.raises(SandboxApiError, match="cwd"):
        controller.execute(_controller_payload(cwd="/etc"))
    with pytest.raises(SandboxApiError, match="run_id"):
        controller.execute(_controller_payload(run_id="../../escape"))


def test_controller_reports_same_run_busy_without_blocking():
    entered = threading.Event()
    release = threading.Event()

    class BlockingEngine(_Engine):
        def execute(self, request):
            entered.set()
            release.wait(2)
            return super().execute(request)

    controller = SandboxController(BlockingEngine(), SandboxConfig())
    thread = threading.Thread(target=lambda: controller.execute(_controller_payload()))
    thread.start()
    assert entered.wait(1)
    try:
        with pytest.raises(SandboxApiError) as failure:
            controller.execute(_controller_payload(command="printf second"))
        assert failure.value.code == "SANDBOX_BUSY"
        assert failure.value.status == 409
    finally:
        release.set()
        thread.join(2)


def test_docker_create_policy_is_fixed_and_hardened():
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/json":
            return httpx.Response(200, json=[])
        if request.url.path == "/containers/create":
            observed.update(json.loads(request.content))
            return httpx.Response(201, json={"Id": "container-123"})
        if request.url.path == "/containers/container-123/start":
            return httpx.Response(204)
        return httpx.Response(404)

    client = object.__new__(DockerEngineClient)
    client.config = SandboxConfig(
        workspace_source="axiom_workspace_data",
        cpu_limit=1.5,
        memory_limit_bytes=256 * 1024 * 1024,
        pids_limit=64,
    )
    client.client = httpx.Client(
        base_url="http://docker", transport=httpx.MockTransport(handler)
    )

    container_id = client._ensure_container("run-policy")

    host = observed["HostConfig"]
    assert container_id == "container-123"
    assert observed["Image"] == "axiom-sandbox:local"
    assert observed["User"] == "10001:10001"
    assert host["ReadonlyRootfs"] is True
    assert host["NetworkMode"] == "none"
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["PidsLimit"] == 64
    assert host["Memory"] == 256 * 1024 * 1024
    assert host["NanoCpus"] == 1_500_000_000
    assert host["Mounts"] == [
        {
            "Type": "volume",
            "Source": "axiom_workspace_data",
            "Target": "/workspace",
            "ReadOnly": False,
        }
    ]
    assert "Privileged" not in host
    assert observed["Labels"]["axiom.managed"] == "true"


def test_controller_timeout_result_remains_a_tool_failure_shape():
    class TimeoutEngine(_Engine):
        def execute(self, request):
            result = super().execute(request)
            return SandboxExecuteResult(
                stdout=result.stdout,
                stderr="",
                exit_code=None,
                duration_ms=100.0,
                stdout_bytes=result.stdout_bytes,
                stderr_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
                timed_out=True,
                cleanup_method="container_remove_timeout",
                sandbox_id=result.sandbox_id,
                resource_limits=result.resource_limits,
            )

    payload = SandboxController(TimeoutEngine(), SandboxConfig()).execute(_controller_payload())

    assert payload["timed_out"] is True
    assert payload["exit_code"] is None
    assert payload["cleanup_method"] == "container_remove_timeout"
