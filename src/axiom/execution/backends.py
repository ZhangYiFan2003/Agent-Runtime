from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from axiom.config import AxiomConfig, ExecutionConfig
from axiom.policy.path_guard import PathGuard


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    command: str
    cwd: str
    workspace: str
    timeout_seconds: float
    stdout_limit_bytes: int
    stderr_limit_bytes: int
    run_id: str | None = None
    invocation_id: str | None = None
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: float
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    timeout_seconds: float
    execution_backend: str
    workspace: str
    env_filtered_count: int
    cleanup_method: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "execution_backend": self.execution_backend,
            "workspace": self.workspace,
            "timeout_seconds": self.timeout_seconds,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "timed_out": self.timed_out,
            "cancelled": False,
            "env_filtered_count": self.env_filtered_count,
            "process_cleanup": self.cleanup_method,
        }


class ExecutionBackend(Protocol):
    name: str

    async def execute(self, request: ExecutionRequest) -> ExecutionResult: ...


@dataclass(frozen=True, slots=True)
class _CapturedOutput:
    data: bytes
    total_bytes: int
    truncated: bool


class _SubprocessExecutionBackend:
    name = "local"

    def __init__(self, *, termination_grace_seconds: float = 1.0) -> None:
        self.termination_grace_seconds = max(0.05, termination_grace_seconds)

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        cwd = self._resolve_cwd(request)
        environment, filtered_count = self._environment(request)
        started = time.perf_counter()
        spawn_options: dict[str, object] = {}
        if os.name == "nt":
            spawn_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            spawn_options["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(
            request.command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            **spawn_options,
        )
        windows_job = _WindowsJob.create_and_assign(process.pid) if os.name == "nt" else None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _capture_stream(process.stdout, max(0, request.stdout_limit_bytes))
        )
        stderr_task = asyncio.create_task(
            _capture_stream(process.stderr, max(0, request.stderr_limit_bytes))
        )
        timed_out = False
        cleanup_method: str | None = None
        try:
            await asyncio.wait_for(process.wait(), timeout=max(0.001, request.timeout_seconds))
        except TimeoutError:
            timed_out = True
            cleanup_method = await self._terminate_process_tree(process, windows_job)
            windows_job = None
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate_process_tree(process, windows_job))
            windows_job = None
            await asyncio.shield(_finish_capture(stdout_task, stderr_task))
            raise

        if not timed_out:
            if windows_job is not None:
                windows_job.close()
                windows_job = None
            elif os.name != "nt":
                cleanup_method = await self._cleanup_finished_posix_group(process.pid)

        stdout, stderr = await _finish_capture(stdout_task, stderr_task)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return ExecutionResult(
            stdout=stdout.data.decode("utf-8", errors="replace"),
            stderr=stderr.data.decode("utf-8", errors="replace"),
            exit_code=process.returncode,
            duration_ms=duration_ms,
            stdout_bytes=stdout.total_bytes,
            stderr_bytes=stderr.total_bytes,
            stdout_truncated=stdout.truncated,
            stderr_truncated=stderr.truncated,
            timed_out=timed_out,
            timeout_seconds=request.timeout_seconds,
            execution_backend=self.name,
            workspace=str(Path(request.workspace).resolve()),
            env_filtered_count=filtered_count,
            cleanup_method=cleanup_method,
        )

    def _resolve_cwd(self, request: ExecutionRequest) -> Path:
        return Path(request.cwd).resolve()

    def _environment(self, request: ExecutionRequest) -> tuple[dict[str, str], int]:
        environment = dict(os.environ)
        environment.update(request.environment)
        return environment, 0

    async def _terminate_process_tree(
        self,
        process: asyncio.subprocess.Process,
        windows_job: _WindowsJob | None = None,
    ) -> str:
        if windows_job is not None:
            windows_job.close()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=self.termination_grace_seconds)
            if process.returncode is None:
                process.kill()
                await process.wait()
            return "job_object_kill"
        if process.returncode is not None:
            return "already_exited"
        if os.name == "nt":
            return await self._terminate_windows_tree(process)
        return await self._terminate_posix_group(process)

    async def _terminate_posix_group(self, process: asyncio.subprocess.Process) -> str:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self.termination_grace_seconds)
            return "process_group_terminate"
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            return "process_group_kill"

    async def _terminate_windows_tree(self, process: asyncio.subprocess.Process) -> str:
        with contextlib.suppress(OSError, ProcessLookupError):
            process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                await asyncio.wait_for(process.wait(), timeout=self.termination_grace_seconds)
                return "process_group_break"
            except TimeoutError:
                pass
        system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
        taskkill_path = Path(system_root) / "System32" / "taskkill.exe"
        try:
            killer = await asyncio.create_subprocess_exec(
                str(taskkill_path),
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            process.kill()
        with contextlib.suppress(TimeoutError, ProcessLookupError):
            await asyncio.wait_for(process.wait(), timeout=self.termination_grace_seconds)
        if process.returncode is None:
            process.kill()
            await process.wait()
            return "process_kill_fallback"
        return "taskkill_tree"

    async def _cleanup_finished_posix_group(self, process_group_id: int) -> str | None:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return None
        await asyncio.sleep(self.termination_grace_seconds)
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return "process_group_cleanup"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGKILL)
        return "process_group_cleanup_kill"


class LocalExecutionBackend(_SubprocessExecutionBackend):
    """Compatibility backend that keeps the complete host environment."""

    name = "local"


class RestrictedExecutionBackend(_SubprocessExecutionBackend):
    """Risk-reduced local execution; not a container or operating-system sandbox."""

    name = "restricted"

    def __init__(
        self,
        workspace: str | Path,
        *,
        allowed_env_names: list[str] | tuple[str, ...],
        termination_grace_seconds: float = 1.0,
    ) -> None:
        super().__init__(termination_grace_seconds=termination_grace_seconds)
        self.workspace = Path(workspace).resolve()
        self.allowed_env_names = {name.casefold() for name in allowed_env_names}

    def _resolve_cwd(self, request: ExecutionRequest) -> Path:
        request_workspace = Path(request.workspace).resolve()
        if request_workspace != self.workspace:
            raise ValueError("execution workspace does not match restricted backend workspace")
        cwd = PathGuard(self.workspace).validate(request.cwd)
        if not cwd.is_dir():
            raise ValueError(f"execution cwd is not a directory: {cwd}")
        return cwd

    def _environment(self, request: ExecutionRequest) -> tuple[dict[str, str], int]:
        environment, filtered_count = filtered_host_environment(self.allowed_env_names)
        for name, value in request.environment.items():
            if name.casefold() in self.allowed_env_names:
                environment[name] = value
            else:
                filtered_count += 1
        return environment, filtered_count


def create_execution_backend(
    config: AxiomConfig | ExecutionConfig,
    workspace: str | Path,
) -> ExecutionBackend:
    execution = config.execution if isinstance(config, AxiomConfig) else config
    if execution.backend == "local":
        return LocalExecutionBackend(
            termination_grace_seconds=execution.termination_grace_seconds,
        )
    if execution.backend != "restricted":
        raise ValueError(f"unknown execution backend: {execution.backend}")
    return RestrictedExecutionBackend(
        workspace,
        allowed_env_names=execution.allowed_env_names,
        termination_grace_seconds=execution.termination_grace_seconds,
    )


def filtered_host_environment(
    allowed_env_names: list[str] | tuple[str, ...] | set[str],
) -> tuple[dict[str, str], int]:
    allowed = {name.casefold() for name in allowed_env_names}
    environment = {name: value for name, value in os.environ.items() if name.casefold() in allowed}
    return environment, max(0, len(os.environ) - len(environment))


async def _capture_stream(
    stream: asyncio.StreamReader,
    limit: int,
) -> _CapturedOutput:
    chunks: list[bytes] = []
    kept = 0
    total = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if kept < limit:
            selected = chunk[: limit - kept]
            chunks.append(selected)
            kept += len(selected)
    return _CapturedOutput(
        data=b"".join(chunks),
        total_bytes=total,
        truncated=total > limit,
    )


async def _finish_capture(
    stdout_task: asyncio.Task[_CapturedOutput],
    stderr_task: asyncio.Task[_CapturedOutput],
) -> tuple[_CapturedOutput, _CapturedOutput]:
    stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    return stdout, stderr


class _WindowsJob:
    def __init__(self, handle: int, kernel32: Any) -> None:
        self.handle = handle
        self.kernel32 = kernel32

    @classmethod
    def create_and_assign(cls, process_id: int) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("read_operations", ctypes.c_ulonglong),
                ("write_operations", ctypes.c_ulonglong),
                ("other_operations", ctypes.c_ulonglong),
                ("read_bytes", ctypes.c_ulonglong),
                ("write_bytes", ctypes.c_ulonglong),
                ("other_bytes", ctypes.c_ulonglong),
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time", ctypes.c_longlong),
                ("per_job_user_time", ctypes.c_longlong),
                ("limit_flags", wintypes.DWORD),
                ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimitInformation),
                ("io", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        information = ExtendedLimitInformation()
        information.basic.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        process = kernel32.OpenProcess(0x0001 | 0x0100 | 0x1000, False, process_id)
        assigned = bool(process) and bool(kernel32.AssignProcessToJobObject(job, process))
        if process:
            kernel32.CloseHandle(process)
        if not configured or not assigned:
            kernel32.CloseHandle(job)
            return None
        return cls(job, kernel32)

    def close(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = 0
