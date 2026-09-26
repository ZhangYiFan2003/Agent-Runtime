from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlparse

import httpx

from axiom.config import SandboxConfig

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


class SandboxApiError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class DockerApiError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SandboxExecuteRequest:
    run_id: str
    invocation_id: str
    command: str
    cwd: str
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    environment: dict[str, str]


@dataclass(frozen=True, slots=True)
class SandboxExecuteResult:
    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: float
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    cleanup_method: str | None
    sandbox_id: str
    resource_limits: dict[str, object]
    network_mode: str = "none"

    def to_dict(self) -> dict[str, object]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "cleanup_method": self.cleanup_method,
            "sandbox_id": self.sandbox_id,
            "resource_limits": self.resource_limits,
            "network_mode": self.network_mode,
        }


class SandboxEngine(Protocol):
    def health(self) -> None: ...

    def execute(self, request: SandboxExecuteRequest) -> SandboxExecuteResult: ...

    def remove_run(self, run_id: str) -> bool: ...


class SandboxController:
    """Validated controller facade; it never accepts Docker policy from callers."""

    def __init__(self, engine: SandboxEngine, config: SandboxConfig) -> None:
        self.engine = engine
        self.config = config
        self._locks_guard = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}

    def health(self) -> dict[str, str]:
        self.engine.health()
        return {"status": "ok", "docker": "ok", "image": "available"}

    def execute(self, payload: object) -> dict[str, object]:
        request = self._parse_request(payload)
        lock = self._run_lock(request.run_id)
        if not lock.acquire(blocking=False):
            raise SandboxApiError(
                "SANDBOX_BUSY",
                "another shell command is active for this Run",
                status=HTTPStatus.CONFLICT,
            )
        try:
            return self.engine.execute(request).to_dict()
        except SandboxApiError:
            raise
        except DockerApiError as exc:
            raise SandboxApiError(
                "SANDBOX_CONTROLLER_ERROR",
                "sandbox Docker operation failed",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            ) from exc
        finally:
            lock.release()

    def cancel(self, run_id: str) -> dict[str, object]:
        self._validate_run_id(run_id)
        removed = self.engine.remove_run(run_id)
        return {"status": "cancelled", "removed": removed}

    def cleanup(self, run_id: str) -> dict[str, object]:
        self._validate_run_id(run_id)
        removed = self.engine.remove_run(run_id)
        return {"status": "removed", "removed": removed}

    def _parse_request(self, payload: object) -> SandboxExecuteRequest:
        if not isinstance(payload, dict):
            raise SandboxApiError("INVALID_REQUEST", "request body must be an object", status=400)
        run_id = str(payload.get("run_id") or "")
        self._validate_run_id(run_id)
        invocation_id = str(payload.get("invocation_id") or "")
        if len(invocation_id.encode("utf-8")) > 512:
            raise SandboxApiError("INVALID_REQUEST", "invocation_id is too large", status=400)
        command = payload.get("command")
        if not isinstance(command, str):
            raise SandboxApiError("INVALID_REQUEST", "command must be a string", status=400)
        command_size = len(command.encode("utf-8"))
        if command_size == 0 or command_size > self.config.command_max_bytes:
            raise SandboxApiError("INVALID_REQUEST", "command size is invalid", status=400)
        cwd = self._validate_cwd(payload.get("cwd"))
        timeout = self._bounded_number(payload.get("timeout_seconds"), "timeout", 0.01, 3600)
        stdout_limit = self._bounded_int(
            payload.get("stdout_limit_bytes"), "stdout limit", 0, 10 * 1024 * 1024
        )
        stderr_limit = self._bounded_int(
            payload.get("stderr_limit_bytes"), "stderr limit", 0, 10 * 1024 * 1024
        )
        environment = self._validate_environment(payload.get("environment"))
        return SandboxExecuteRequest(
            run_id=run_id,
            invocation_id=invocation_id,
            command=command,
            cwd=cwd,
            timeout_seconds=timeout,
            stdout_limit_bytes=stdout_limit,
            stderr_limit_bytes=stderr_limit,
            environment=environment,
        )

    def _run_lock(self, run_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._run_locks.setdefault(run_id, threading.Lock())

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if not _RUN_ID.fullmatch(run_id):
            raise SandboxApiError("INVALID_RUN_ID", "run_id is invalid", status=400)

    @staticmethod
    def _validate_cwd(value: object) -> str:
        if not isinstance(value, str) or not value.startswith("/workspace"):
            raise SandboxApiError("INVALID_CWD", "cwd must be inside /workspace", status=400)
        path = PurePosixPath(value)
        if path == PurePosixPath("/workspace"):
            return "/workspace"
        if PurePosixPath("/workspace") not in path.parents or ".." in path.parts:
            raise SandboxApiError("INVALID_CWD", "cwd must be inside /workspace", status=400)
        return str(path)

    @staticmethod
    def _bounded_number(value: object, name: str, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise SandboxApiError("INVALID_REQUEST", f"{name} is invalid", status=400) from exc
        if not minimum <= parsed <= maximum:
            raise SandboxApiError("INVALID_REQUEST", f"{name} is out of range", status=400)
        return parsed

    @staticmethod
    def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool):
            raise SandboxApiError("INVALID_REQUEST", f"{name} is invalid", status=400)
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise SandboxApiError("INVALID_REQUEST", f"{name} is invalid", status=400) from exc
        if not minimum <= parsed <= maximum:
            raise SandboxApiError("INVALID_REQUEST", f"{name} is out of range", status=400)
        return parsed

    @staticmethod
    def _validate_environment(value: object) -> dict[str, str]:
        if not isinstance(value, dict) or len(value) > 64:
            raise SandboxApiError("INVALID_ENV", "environment is invalid", status=400)
        result: dict[str, str] = {}
        for raw_name, raw_value in value.items():
            if not isinstance(raw_name, str) or not _ENV_NAME.fullmatch(raw_name):
                raise SandboxApiError("INVALID_ENV", "environment name is invalid", status=400)
            if not isinstance(raw_value, str) or len(raw_value.encode("utf-8")) > 4096:
                raise SandboxApiError("INVALID_ENV", "environment value is invalid", status=400)
            result[raw_name] = raw_value
        return result


class DockerEngineClient:
    """Narrow Docker Engine adapter with all sandbox policy fixed by trusted config."""

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config
        transport = httpx.HTTPTransport(uds=config.docker_socket_path)
        self.client = httpx.Client(base_url="http://docker", transport=transport, timeout=10.0)

    def close(self) -> None:
        self.client.close()

    def health(self) -> None:
        self._request("GET", "/_ping")
        self._request("GET", f"/images/{quote(self.config.image, safe='')}/json")

    def execute(self, request: SandboxExecuteRequest) -> SandboxExecuteResult:
        container_id = self._ensure_container(request.run_id)
        exec_id = self._create_exec(container_id, request)
        capture = _DockerStreamCapture(
            stdout_limit=request.stdout_limit_bytes,
            stderr_limit=request.stderr_limit_bytes,
        )
        error: list[BaseException] = []

        def stream() -> None:
            try:
                with self.client.stream(
                    "POST",
                    f"/exec/{exec_id}/start",
                    json={"Detach": False, "Tty": False},
                    timeout=None,
                ) as response:
                    self._raise_for_status(response)
                    for chunk in response.iter_bytes():
                        capture.feed(chunk)
                capture.finish()
            except BaseException as exc:  # noqa: BLE001 - handed to controller thread
                error.append(exc)

        started = time.perf_counter()
        thread = threading.Thread(target=stream, name=f"sandbox-exec-{exec_id[:12]}", daemon=True)
        thread.start()
        thread.join(request.timeout_seconds)
        timed_out = thread.is_alive()
        cleanup_method: str | None = None
        if timed_out:
            self._remove_container(container_id)
            cleanup_method = "container_remove_timeout"
            thread.join(5.0)
        if error and not timed_out:
            raise DockerApiError("sandbox exec stream failed") from error[0]
        exit_code: int | None = None
        if not timed_out:
            inspect = self._request("GET", f"/exec/{exec_id}/json").json()
            raw_exit = inspect.get("ExitCode") if isinstance(inspect, dict) else None
            exit_code = int(raw_exit) if raw_exit is not None else None
            container = self._request("GET", f"/containers/{container_id}/json").json()
            state = container.get("State", {}) if isinstance(container, dict) else {}
            if not state.get("Running", False):
                if state.get("OOMKilled"):
                    capture.append_stderr(b"sandbox terminated by memory limit\n")
                self._remove_container(container_id)
                cleanup_method = "container_remove_terminated"
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return SandboxExecuteResult(
            stdout=capture.stdout.decode("utf-8", errors="replace"),
            stderr=capture.stderr.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout_bytes=capture.stdout_bytes,
            stderr_bytes=capture.stderr_bytes,
            stdout_truncated=capture.stdout_truncated,
            stderr_truncated=capture.stderr_truncated,
            timed_out=timed_out,
            cleanup_method=cleanup_method,
            sandbox_id=container_id[:12],
            resource_limits={
                "cpus": self.config.cpu_limit,
                "memory_bytes": self.config.memory_limit_bytes,
                "pids": self.config.pids_limit,
                "tmpfs_bytes": self.config.tmpfs_size_bytes,
            },
        )

    def remove_run(self, run_id: str) -> bool:
        removed = False
        for container in self._containers_for_run(run_id):
            container_id = str(container.get("Id") or "")
            if container_id:
                self._remove_container(container_id)
                removed = True
        return removed

    def _ensure_container(self, run_id: str) -> str:
        containers = self._containers_for_run(run_id)
        running = [item for item in containers if item.get("State") == "running"]
        if running:
            keep = str(running[0]["Id"])
            for stale in [*running[1:], *(item for item in containers if item not in running)]:
                self._remove_container(str(stale["Id"]))
            return keep
        for stale in containers:
            self._remove_container(str(stale["Id"]))
        identity = _run_identity(run_id)
        mount_type = "bind" if self.config.workspace_source.startswith("/") else "volume"
        body = {
            "Image": self.config.image,
            "Cmd": ["/bin/sh", "-c", "while :; do sleep 3600; done"],
            "WorkingDir": "/workspace",
            "User": "10001:10001",
            "Env": [
                "HOME=/tmp",
                "LANG=C.UTF-8",
                "PYTHONDONTWRITEBYTECODE=1",
                "PATH=/usr/local/bin:/usr/bin:/bin",
            ],
            "Labels": {
                "axiom.managed": "true",
                "axiom.run_id": run_id,
                "axiom.run_id_hash": identity,
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "NetworkMode": "none",
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": self.config.pids_limit,
                "Memory": self.config.memory_limit_bytes,
                "NanoCpus": int(self.config.cpu_limit * 1_000_000_000),
                "Tmpfs": {
                    "/tmp": (
                        "rw,nosuid,nodev,mode=1777,uid=10001,gid=10001,"
                        f"size={self.config.tmpfs_size_bytes}"
                    )
                },
                "Mounts": [
                    {
                        "Type": mount_type,
                        "Source": self.config.workspace_source,
                        "Target": "/workspace",
                        "ReadOnly": False,
                    }
                ],
            },
        }
        created = self._request(
            "POST", f"/containers/create?name=axiom-run-{identity[:20]}", json=body
        ).json()
        container_id = str(created.get("Id") or "") if isinstance(created, dict) else ""
        if not container_id:
            raise DockerApiError("Docker did not return a container ID")
        self._request("POST", f"/containers/{container_id}/start")
        return container_id

    def _create_exec(self, container_id: str, request: SandboxExecuteRequest) -> str:
        environment = [f"{name}={value}" for name, value in sorted(request.environment.items())]
        response = self._request(
            "POST",
            f"/containers/{container_id}/exec",
            json={
                "AttachStdout": True,
                "AttachStderr": True,
                "Tty": False,
                "User": "10001:10001",
                "WorkingDir": request.cwd,
                "Env": environment,
                "Cmd": ["/bin/sh", "-lc", request.command],
            },
        ).json()
        exec_id = str(response.get("Id") or "") if isinstance(response, dict) else ""
        if not exec_id:
            raise DockerApiError("Docker did not return an exec ID")
        return exec_id

    def _containers_for_run(self, run_id: str) -> list[dict[str, object]]:
        filters = json.dumps(
            {"label": ["axiom.managed=true", f"axiom.run_id_hash={_run_identity(run_id)}"]},
            separators=(",", ":"),
        )
        payload = self._request(
            "GET", "/containers/json", params={"all": "1", "filters": filters}
        ).json()
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _remove_container(self, container_id: str) -> None:
        response = self.client.delete(
            f"/containers/{container_id}", params={"force": "1", "v": "0"}
        )
        if response.status_code not in {204, 404}:
            self._raise_for_status(response)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
            self._raise_for_status(response)
            return response
        except httpx.HTTPError as exc:
            raise DockerApiError("Docker API is unavailable") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_error:
            raise DockerApiError(f"Docker API returned HTTP {response.status_code}")


class _DockerStreamCapture:
    def __init__(self, *, stdout_limit: int, stderr_limit: int) -> None:
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self._buffer = bytearray()
        self._stdout = bytearray()
        self._stderr = bytearray()
        self.stdout_bytes = 0
        self.stderr_bytes = 0

    @property
    def stdout(self) -> bytes:
        return bytes(self._stdout)

    @property
    def stderr(self) -> bytes:
        return bytes(self._stderr)

    @property
    def stdout_truncated(self) -> bool:
        return self.stdout_bytes > len(self._stdout)

    @property
    def stderr_truncated(self) -> bool:
        return self.stderr_bytes > len(self._stderr)

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)
        while len(self._buffer) >= 8:
            stream_type = self._buffer[0]
            frame_size = int.from_bytes(self._buffer[4:8], "big")
            if len(self._buffer) < 8 + frame_size:
                return
            payload = bytes(self._buffer[8 : 8 + frame_size])
            del self._buffer[: 8 + frame_size]
            if stream_type == 1:
                self._append(self._stdout, payload, self.stdout_limit, stdout=True)
            elif stream_type == 2:
                self._append(self._stderr, payload, self.stderr_limit, stdout=False)

    def finish(self) -> None:
        if self._buffer:
            self.append_stderr(b"sandbox returned an incomplete output frame\n")
            self._buffer.clear()

    def append_stderr(self, payload: bytes) -> None:
        self._append(self._stderr, payload, self.stderr_limit, stdout=False)

    def _append(self, target: bytearray, payload: bytes, limit: int, *, stdout: bool) -> None:
        if stdout:
            self.stdout_bytes += len(payload)
        else:
            self.stderr_bytes += len(payload)
        remaining = max(0, limit - len(target))
        if remaining:
            target.extend(payload[:remaining])


class SandboxHttpServer:
    def __init__(
        self,
        controller: SandboxController,
        *,
        host: str = "127.0.0.1",
        port: int = 8090,
    ) -> None:
        self.controller = controller
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None

    def serve_forever(self) -> None:
        controller = self.controller

        class Handler(BaseHTTPRequestHandler):
            server_version = "AxiomSandbox/1"

            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/health":
                    self._send(404, {"code": "NOT_FOUND", "message": "not found"})
                    return
                self._call(controller.health)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/v1/execute":
                    self._call(lambda: controller.execute(self._json_body()))
                    return
                run_id = _run_path(parsed.path, suffix="/cancel")
                if run_id is not None:
                    self._call(lambda: controller.cancel(run_id))
                    return
                self._send(404, {"code": "NOT_FOUND", "message": "not found"})

            def do_DELETE(self) -> None:  # noqa: N802
                run_id = _run_path(urlparse(self.path).path)
                if run_id is None:
                    self._send(404, {"code": "NOT_FOUND", "message": "not found"})
                    return
                self._call(lambda: controller.cleanup(run_id))

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _json_body(self) -> object:
                raw_length = self.headers.get("Content-Length")
                try:
                    length = int(raw_length or "0")
                except ValueError as exc:
                    raise SandboxApiError(
                        "INVALID_REQUEST", "invalid body length", status=400
                    ) from exc
                if length <= 0 or length > controller.config.request_max_bytes:
                    raise SandboxApiError(
                        "INVALID_REQUEST", "request body is too large", status=413
                    )
                try:
                    return json.loads(self.rfile.read(length))
                except json.JSONDecodeError as exc:
                    raise SandboxApiError(
                        "INVALID_REQUEST", "request body is invalid", status=400
                    ) from exc

            def _call(self, operation: Any) -> None:
                try:
                    self._send(200, operation())
                except SandboxApiError as exc:
                    self._send(exc.status, {"code": exc.code, "message": str(exc)})
                except Exception:
                    self._send(
                        503,
                        {"code": "SANDBOX_UNAVAILABLE", "message": "sandbox service unavailable"},
                    )

            def _send(self, status: int, payload: object) -> None:
                encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = int(self._httpd.server_address[1])
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def _run_identity(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()


def _run_path(path: str, *, suffix: str = "") -> str | None:
    prefix = "/v1/runs/"
    if not path.startswith(prefix):
        return None
    remainder = path[len(prefix) :]
    if suffix:
        if not remainder.endswith(suffix):
            return None
        remainder = remainder[: -len(suffix)]
    elif "/" in remainder:
        return None
    return unquote(remainder)
