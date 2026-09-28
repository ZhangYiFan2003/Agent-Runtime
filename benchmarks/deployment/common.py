from __future__ import annotations

import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "deploy" / "compose.yaml"
STAGE15_COMPOSE = Path(__file__).with_name("compose.stage15.yaml")
DEFAULT_OUTPUT = ROOT / ".tmp" / "stage15"
PROJECT_PATTERN = re.compile(r"^axiom-stage15(?:-[a-z0-9][a-z0-9-]{0,31})?$")
KNOWN_SERVICES = {
    "fake-provider",
    "minio",
    "minio-init",
    "postgres",
    "providerd",
    "runtime-api",
    "sandboxd",
    "sandbox-image",
    "web",
    "worker",
}
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
SAFE_STATUSES = TERMINAL_STATUSES | {"INTERRUPTED", "WAITING_APPROVAL", "WAITING_CHILD"}


@dataclass(frozen=True, slots=True)
class LoadProfile:
    name: str
    workers: int
    concurrency: int
    runs: int
    warmups: int
    provider_latency_ms: int
    run_timeout_seconds: float
    max_active_runs: int = 4
    max_queued_runs: int = 100
    provider_max_concurrency: int = 4
    provider_max_pending: int = 8
    submission_rate: float | None = None
    submission_burst: int | None = None


def load_profile(path: Path, name: str, *, force: bool = False) -> LoadProfile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("profiles"), dict):
        raise ValueError("invalid deployment profile schema")
    raw = payload["profiles"].get(name)
    if not isinstance(raw, dict):
        raise ValueError(f"unknown deployment profile: {name}")
    profile = LoadProfile(name=name, **raw)
    limits = {
        "workers": (profile.workers, 1, 4),
        "concurrency": (profile.concurrency, 1, 64),
        "runs": (profile.runs, 1, 500),
        "warmups": (profile.warmups, 0, 20),
        "provider_latency_ms": (profile.provider_latency_ms, 0, 10_000),
        "run_timeout_seconds": (profile.run_timeout_seconds, 1, 300),
        "max_active_runs": (profile.max_active_runs, 1, 64),
        "max_queued_runs": (profile.max_queued_runs, 1, 1_000),
        "provider_max_concurrency": (profile.provider_max_concurrency, 1, 64),
        "provider_max_pending": (profile.provider_max_pending, 0, 256),
    }
    violations = [
        name
        for name, (value, minimum, maximum) in limits.items()
        if not minimum <= value <= maximum
    ]
    if profile.submission_rate is not None and not 0.1 <= profile.submission_rate <= 1_000:
        violations.append("submission_rate")
    if profile.submission_burst is not None and not 1 <= profile.submission_burst <= 10_000:
        violations.append("submission_burst")
    if violations and not force:
        raise ValueError(f"unsafe profile values require --force: {', '.join(violations)}")
    return profile


def nearest_rank(values: Iterable[float], percentile: float) -> float | None:
    items = sorted(float(value) for value in values)
    if not items:
        return None
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    index = max(0, min(len(items) - 1, math.ceil(len(items) * percentile / 100) - 1))
    return round(items[index], 3)


def latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    items = [float(value) for value in values]
    return {
        "count": len(items),
        "p50_ms": nearest_rank(items, 50),
        "p95_ms": nearest_rank(items, 95),
        "p99_ms": nearest_rank(items, 99),
        "max_ms": round(max(items), 3) if items else None,
    }


def validate_project_name(project: str) -> str:
    if not PROJECT_PATTERN.fullmatch(project):
        raise ValueError("project must use the reserved axiom-stage15 prefix")
    return project


class ComposeController:
    def __init__(self, project: str, *, web_port: int = 18080) -> None:
        self.project = validate_project_name(project)
        if not 1024 <= web_port <= 65535:
            raise ValueError("web port must be between 1024 and 65535")
        self.web_port = web_port
        self.base_command = [
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(BASE_COMPOSE),
            "--file",
            str(STAGE15_COMPOSE),
        ]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.web_port}"

    def environment(self, **overrides: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "POSTGRES_USER": "axiom",
                "POSTGRES_DB": "axiom",
                "POSTGRES_PASSWORD": "stage15-postgres-password",
                "AXIOM_POSTGRES_DSN": "postgresql://axiom:stage15-postgres-password@postgres:5432/axiom",
                "AXIOM_RUNTIME_API_KEY": "stage15-runtime-key",
                "AXIOM_API_KEY": "stage15-fake-provider",
                "DEEPSEEK_API_KEY": "",
                "OPENAI_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "MINIO_ROOT_USER": "stage15-minio-user",
                "MINIO_ROOT_PASSWORD": "stage15-minio-password",
                "AXIOM_ARTIFACT_S3_BUCKET": "stage15-artifacts",
                "AXIOM_WEB_PORT": str(self.web_port),
                "AXIOM_SANDBOX_WORKSPACE_SOURCE": f"{self.project}_workspace_data",
                "AXIOM_SANDBOX_IMAGE": "axiom-sandbox:local",
                "STAGE15_PROVIDER_MODE": "healthy",
                "STAGE15_PROVIDER_LATENCY_MS": "25",
                "STAGE15_PROVIDER_MAX_CONCURRENCY": "2",
                "STAGE15_PROVIDER_MAX_PENDING": "2",
                "STAGE15_POSTGRES_PORT": "15432",
                "AXIOM_WORKER_LEASE_SECONDS": "8",
                "AXIOM_WORKER_HEARTBEAT_INTERVAL_SECONDS": "2",
            }
        )
        env.update(overrides)
        return env

    def run(
        self,
        arguments: list[str],
        *,
        services: Iterable[str] = (),
        env_overrides: Mapping[str, str] | None = None,
        check: bool = True,
        capture_output: bool = True,
        timeout: float = 180,
    ) -> subprocess.CompletedProcess[str]:
        for service in services:
            if service not in KNOWN_SERVICES:
                raise ValueError(f"unsupported Stage15 service: {service}")
        command = [*self.base_command, *arguments, *services]
        return subprocess.run(
            command,
            cwd=ROOT,
            env=self.environment(**dict(env_overrides or {})),
            check=check,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
        )

    def up(
        self,
        *,
        workers: int,
        build: bool = False,
        env_overrides: Mapping[str, str] | None = None,
    ) -> None:
        arguments = ["up", "-d", "--wait", "--scale", f"worker={workers}"]
        if build:
            arguments.insert(1, "--build")
        self.run(arguments, env_overrides=env_overrides, timeout=600, capture_output=False)

    def cleanup(self) -> None:
        self.run(
            ["down", "--volumes", "--remove-orphans", "--timeout", "20"],
            check=False,
            timeout=180,
            capture_output=False,
        )

    def stop(self, service: str) -> None:
        self.run(["stop", "--timeout", "10"], services=[service], capture_output=False)

    def start(self, service: str) -> None:
        self.run(["up", "-d", "--wait", "--no-deps"], services=[service], capture_output=False)

    def restart(self, service: str) -> None:
        self.run(["restart", "--timeout", "10"], services=[service], capture_output=False)
        self.run(
            ["up", "-d", "--wait", "--no-deps"],
            services=[service],
            capture_output=False,
        )

    def recreate(self, service: str, **env_overrides: str) -> None:
        self.run(
            ["up", "-d", "--wait", "--no-deps", "--force-recreate"],
            services=[service],
            env_overrides=env_overrides,
            capture_output=False,
        )

    def exec(self, service: str, command: list[str], *, timeout: float = 30) -> str:
        self._validate_service(service)
        result = self.run(
            ["exec", "-T", service, *command],
            timeout=timeout,
        )
        return result.stdout

    def provider_health(self) -> dict[str, Any]:
        raw = self.exec(
            "providerd",
            [
                "python",
                "-c",
                (
                    "import urllib.request;"
                    "print(urllib.request.urlopen('http://127.0.0.1:8070/v1/providers',"
                    "timeout=3).read().decode())"
                ),
            ],
        )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("providerd returned a non-object health response")
        return payload

    def service_container_ids(self, service: str, *, all_containers: bool = False) -> list[str]:
        self._validate_service(service)
        arguments = ["ps", "-q"]
        if all_containers:
            arguments.append("--all")
        result = self.run(arguments, services=[service])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def stop_container(self, container_id: str, *, expected_service: str) -> None:
        self._validate_container(container_id, expected_service=expected_service)
        subprocess.run(
            ["docker", "stop", "--time", "10", container_id],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )

    def kill_container(self, container_id: str, *, expected_service: str) -> None:
        self._validate_container(container_id, expected_service=expected_service)
        subprocess.run(
            ["docker", "kill", container_id],
            check=True,
            timeout=20,
            capture_output=True,
            text=True,
        )

    def _validate_container(self, container_id: str, *, expected_service: str) -> None:
        self._validate_service(expected_service)
        labels = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                (
                    '{{index .Config.Labels "com.docker.compose.project"}}|'
                    '{{index .Config.Labels "com.docker.compose.service"}}'
                ),
                container_id,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
        if labels != f"{self.project}|{expected_service}":
            raise ValueError("refusing to stop a container outside the reserved Stage15 project")

    @staticmethod
    def sandbox_container_ids(run_id: str) -> list[str]:
        if not re.fullmatch(r"run_[A-Za-z0-9_-]+", run_id):
            raise ValueError("invalid Run id for sandbox inspection")
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                "label=axiom.managed=true",
                "--filter",
                f"label=axiom.run_id={run_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _validate_service(service: str) -> None:
        if service not in KNOWN_SERVICES:
            raise ValueError(f"unsupported Stage15 service: {service}")


def wait_until(predicate, *, timeout: float, interval: float = 0.25, label: str = "condition"):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - bounded polling records the latest failure
            last_error = exc
        time.sleep(interval)
    suffix = f": {last_error}" if last_error else ""
    raise TimeoutError(f"timed out waiting for {label}{suffix}")


def environment_metadata(*, profile: str, workers: int) -> dict[str, Any]:
    return {
        "os": platform.system(),
        "os_release": platform.release(),
        "python": platform.python_version(),
        "docker": _docker_version(),
        "logical_cpus": os.cpu_count(),
        "worker_count": workers,
        "profile": profile,
        "runtime_git_commit": _git_commit(),
        "captured_at": datetime.now(UTC).isoformat(),
    }


def write_report(payload: dict[str, Any], output: Path, markdown: str) -> None:
    _validate_report(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown, encoding="utf-8")


def _validate_report(payload: dict[str, Any]) -> None:
    forbidden_keys = (
        "api_key",
        "authorization",
        "credential",
        "password",
        "dsn",
        "secret",
        "token",
    )
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if any(marker in str(key).casefold() for marker in forbidden_keys):
                    raise ValueError(f"secret-like report field is forbidden: {key}")
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str) and (
            re.search(r"[A-Za-z]:\\", item) or item.startswith("/home/")
        ):
            raise ValueError("absolute private paths are forbidden in reports")


def profile_environment(profile: LoadProfile) -> dict[str, str]:
    env = {
        "AXIOM_MAX_ACTIVE_RUNS": str(profile.max_active_runs),
        "AXIOM_MAX_QUEUED_RUNS": str(profile.max_queued_runs),
        "STAGE15_PROVIDER_LATENCY_MS": str(profile.provider_latency_ms),
        "STAGE15_PROVIDER_MAX_CONCURRENCY": str(profile.provider_max_concurrency),
        "STAGE15_PROVIDER_MAX_PENDING": str(profile.provider_max_pending),
    }
    if profile.submission_rate is not None:
        env["AXIOM_GLOBAL_SUBMISSION_RATE"] = str(profile.submission_rate)
        env["AXIOM_PRINCIPAL_SUBMISSION_RATE"] = str(profile.submission_rate)
    if profile.submission_burst is not None:
        env["AXIOM_GLOBAL_SUBMISSION_BURST"] = str(profile.submission_burst)
        env["AXIOM_PRINCIPAL_SUBMISSION_BURST"] = str(profile.submission_burst)
    return env


def profile_dict(profile: LoadProfile) -> dict[str, Any]:
    return asdict(profile)


def _docker_version() -> str:
    result = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "unavailable"


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() or "unknown"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def monotonic_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def python_command() -> list[str]:
    return [sys.executable]
