from __future__ import annotations

import json
import os
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


def _home() -> Path:
    return Path.home()


@dataclass(slots=True)
class LlmConfig:
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    base_url: str | None = None
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: float = 120.0
    route: str = "default"
    gateway_url: str = "http://providerd:8070"


@dataclass(slots=True)
class ProviderTargetConfig:
    id: str = "deepseek-primary"
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com/v1"
    api_key_env: str = "AXIOM_API_KEY"
    context_window: int = 1_000_000
    max_tokens: int = 8192
    temperature: float = 0.7
    timeout: float = 120.0
    prompt_cache: bool = True
    max_concurrency: int = 4
    max_pending: int = 8
    admission_timeout_seconds: float = 0.25
    requests_per_minute: int | None = None
    failure_threshold: int = 3
    open_seconds: float = 30.0
    half_open_max_probes: int = 1
    rate_limit_cooldown_seconds: float = 5.0


def _default_provider_routes() -> dict[str, list[ProviderTargetConfig]]:
    return {"default": [ProviderTargetConfig()]}


@dataclass(slots=True)
class ProviderGatewayConfig:
    routes: dict[str, list[ProviderTargetConfig]] = field(default_factory=_default_provider_routes)
    request_max_bytes: int = 2 * 1024 * 1024
    max_messages: int = 512
    max_tool_schema_bytes: int = 512 * 1024


@dataclass(slots=True)
class EmbeddingConfig:
    enabled: bool = False
    provider: str = "openai-compatible"
    model: str = ""
    api_key: str = ""
    base_url: str | None = None
    dimensions: int | None = None
    timeout: float = 60.0
    batch_size: int = 64
    search_mode: str = "auto"
    lexical_weight: float = 0.55
    vector_weight: float = 0.45
    v2_lexical_weight: float = 0.40
    v2_vector_weight: float = 0.10
    symbol_weight: float = 0.50
    candidate_limit: int = 200
    max_input_chars: int = 12000


@dataclass(slots=True)
class ToolsConfig:
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    timeout: float = 60.0
    batch_timeout: float = 90.0
    max_concurrent_read: int = 4


@dataclass(slots=True)
class SandboxConfig:
    controller_url: str = "http://sandboxd:8090"
    image: str = "axiom-sandbox:local"
    workspace_source: str = "axiom_workspace_data"
    docker_socket_path: str = "/var/run/docker.sock"
    cpu_limit: float = 1.0
    memory_limit_bytes: int = 512 * 1024 * 1024
    pids_limit: int = 128
    tmpfs_size_bytes: int = 64 * 1024 * 1024
    request_max_bytes: int = 64 * 1024
    command_max_bytes: int = 32 * 1024


@dataclass(slots=True)
class ExecutionConfig:
    backend: str = "restricted"
    stdout_limit_bytes: int = 20_000
    stderr_limit_bytes: int = 20_000
    termination_grace_seconds: float = 1.0
    allowed_env_names: list[str] = field(
        default_factory=lambda: [
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
        ]
    )
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)


@dataclass(slots=True)
class MultiAgentConfig:
    max_parallel_workers: int = 2


@dataclass(slots=True)
class PlanConfig:
    max_parallel_tasks: int = 2


@dataclass(slots=True)
class McpConfig:
    servers: list[dict[str, Any]] = field(default_factory=list)
    auto_start: bool = True


@dataclass(slots=True)
class MemoryConfig:
    max_conversation_history: int = 100
    long_term_enabled: bool = True
    long_term_db_path: str = "~/.axiom/memory.db"
    token_budget_mode: str = "balanced"
    compression_threshold: float = 0.8
    summary_threshold_messages: int = 6
    summary_map_chunk_estimated_tokens: int = 400
    summary_reduce_input_estimated_tokens: int = 1200
    summary_minimum_unsummarized_messages: int = 4
    summary_recent_message_reserve: int = 2
    summary_max_chars: int = 2000
    summary_max_attempts: int = 1


@dataclass(slots=True)
class ContextConfig:
    """Live model-input budgeting; ``None`` values derive from LLM metadata."""

    model_context_window: int | None = None
    reserved_output_tokens: int | None = None
    high_watermark_ratio: float = 0.80
    target_after_compaction_ratio: float = 0.60
    recent_message_reserve: int = 6
    hard_input_limit: int | None = None
    max_tool_result_chars: int = 2_000


@dataclass(slots=True)
class RunBudgetConfig:
    """Optional lifetime limits for one durable Run and its descendant tree."""

    max_steps: int | None = None
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_total_tokens: int | None = None
    max_wall_time_seconds: float | None = None
    max_cost_usd: str | float | None = None
    soft_limit_ratio: float = 0.80
    model_pricing: dict[str, dict[str, str | float]] = field(default_factory=dict)


@dataclass(slots=True)
class DependencyConfig:
    """Bounded retry defaults; per-attempt timeouts remain on LLM and Tool config."""

    max_attempts: int = 3
    base_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 4.0
    jitter_enabled: bool = True


@dataclass(slots=True)
class StorageConfig:
    """Durable Runtime storage selection. PostgreSQL credentials stay in the DSN."""

    backend: str = "sqlite"
    sqlite_path: str | None = None
    postgres_dsn: str = ""
    pool_min_size: int = 1
    pool_max_size: int = 4
    connect_timeout_seconds: float = 5.0


@dataclass(slots=True)
class ArtifactS3Config:
    endpoint: str = "http://minio:9000"
    bucket: str = "axiom-artifacts"
    access_key: str = ""
    secret_key: str = ""
    secure: bool = False
    region: str = ""


@dataclass(slots=True)
class ArtifactConfig:
    enabled: bool = False
    backend: str = "local"
    local_path: str = ""
    max_file_bytes: int = 100 * 1024 * 1024
    max_metadata_bytes: int = 16 * 1024
    s3: ArtifactS3Config = field(default_factory=ArtifactS3Config)


@dataclass(slots=True)
class ProvenanceConfig:
    enabled: bool = False
    prompt_guidance_enabled: bool = True
    max_claim_chars: int = 4_000
    max_summary_chars: int = 1_000
    max_metadata_bytes: int = 16 * 1024
    max_code_lines: int = 60
    max_excerpt_chars: int = 8_000
    max_evidence_per_claim: int = 16


@dataclass(slots=True)
class WorkerConfig:
    """Optional PostgreSQL distributed ownership Worker settings."""

    distributed_enabled: bool = False
    lease_seconds: float = 30.0
    heartbeat_interval_seconds: float = 10.0
    poll_interval_seconds: float = 0.5
    max_run_delivery_attempts: int | None = None


@dataclass(slots=True)
class CapacityConfig:
    """Optional shared PostgreSQL backlog and active-execution limits."""

    max_queued_runs: int | None = None
    max_active_runs: int | None = None
    max_queued_runs_per_principal: int | None = None
    max_active_runs_per_principal: int | None = None


@dataclass(slots=True)
class TrafficGovernanceConfig:
    """Optional distributed submission and scheduling governance."""

    global_submission_rate: float | None = None
    global_submission_burst: int | None = None
    principal_submission_rate: float | None = None
    principal_submission_burst: int | None = None
    aging_interval_seconds: float = 300.0
    aging_boost_cap: int = 2


@dataclass(slots=True)
class ProgressConfig:
    """Deterministic, bounded no-progress detection for durable Runs."""

    enabled: bool = True
    max_identical_actions: int = 4
    max_identical_errors: int = 3
    max_cycle_repetitions: int = 3
    max_stagnant_steps: int = 8
    max_recovery_attempts: int = 1
    history_limit: int = 32


@dataclass(slots=True)
class PolicyConfig:
    hitl_mode: str = "auto"
    path_guard_enabled: bool = True
    command_blacklist: list[str] = field(
        default_factory=lambda: [
            "sudo",
            "rm -rf /",
            "rm -rf ~",
            "mkfs",
            "dd if=/dev/zero",
            ":(){:|:&};:",
            "chmod -R 777 /",
            "curl | sh",
            "curl|sh",
            "shutdown",
            "reboot",
        ]
    )
    audit_log_path: str = "~/.axiom/audit.jsonl"
    network_access: str = "public"
    allowed_network_hosts: list[str] = field(default_factory=list)
    deny_private_networks: bool = True
    sensitive_path_patterns: list[str] = field(
        default_factory=lambda: [".env", ".env.*", "*.pem", "*.key", "*credentials*", "*token*"]
    )


@dataclass(slots=True)
class PromptConfig:
    personality: str = "default"
    agent_mode: str = "react"
    custom_prompt_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FeatureConfig:
    mcp: bool = True
    skill: bool = True
    memory: bool = True
    audit_log: bool = True
    context_compression: bool = True
    code_index: bool = True


@dataclass(slots=True)
class AxiomConfig:
    llm: LlmConfig = field(default_factory=LlmConfig)
    provider_gateway: ProviderGatewayConfig = field(default_factory=ProviderGatewayConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    render_mode: str = "inline"
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    multi_agent: MultiAgentConfig = field(default_factory=MultiAgentConfig)
    plan: PlanConfig = field(default_factory=PlanConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    run_budget: RunBudgetConfig = field(default_factory=RunBudgetConfig)
    dependency: DependencyConfig = field(default_factory=DependencyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    artifacts: ArtifactConfig = field(default_factory=ArtifactConfig)
    provenance: ProvenanceConfig = field(default_factory=ProvenanceConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    capacity: CapacityConfig = field(default_factory=CapacityConfig)
    traffic: TrafficGovernanceConfig = field(default_factory=TrafficGovernanceConfig)
    progress: ProgressConfig = field(default_factory=ProgressConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)


def load_config(
    project_root: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
    env: dict[str, str | None] | None = None,
) -> AxiomConfig:
    env_map = env if env is not None else os.environ
    data = _config_to_dict(AxiomConfig())

    user_config = _read_json(_home() / ".axiom" / "config.json")
    if user_config:
        data = _deep_merge(data, user_config)

    root = Path(project_root).resolve() if project_root else None
    if root:
        project_config = _read_json(root / ".axiom" / "config.json")
        if project_config:
            data = _deep_merge(data, project_config)
        project_env = _read_env(root / ".env")
        if project_env:
            data = _apply_env(data, project_env)

    if overrides:
        data = _deep_merge(data, overrides)

    data = _apply_env(data, env_map)
    config = _dict_to_config(data)
    config.memory.long_term_db_path = _expand_home(config.memory.long_term_db_path)
    config.policy.audit_log_path = _expand_home(config.policy.audit_log_path)
    storage_backend = config.storage.backend.strip().lower()
    if storage_backend not in {"sqlite", "postgres"}:
        raise ValueError("storage.backend must be sqlite or postgres")
    if storage_backend == "postgres" and not config.storage.postgres_dsn.strip():
        raise ValueError("storage.postgres_dsn is required for the postgres backend")
    if storage_backend == "postgres" and (
        config.storage.pool_min_size <= 0
        or config.storage.pool_max_size < config.storage.pool_min_size
        or config.storage.connect_timeout_seconds <= 0
    ):
        raise ValueError(
            "PostgreSQL pool sizes must be positive and ordered, with a positive connect timeout"
        )
    artifact_backend = config.artifacts.backend.strip().lower()
    if artifact_backend not in {"local", "s3"}:
        raise ValueError("artifacts.backend must be local or s3")
    if config.artifacts.max_file_bytes <= 0 or config.artifacts.max_metadata_bytes <= 0:
        raise ValueError("artifact size limits must be positive")
    if config.artifacts.enabled and artifact_backend == "s3":
        artifact_s3 = config.artifacts.s3
        if not artifact_s3.endpoint.startswith(("http://", "https://")):
            raise ValueError("artifacts.s3.endpoint must be HTTP(S)")
        if not all(
            value.strip()
            for value in (artifact_s3.bucket, artifact_s3.access_key, artifact_s3.secret_key)
        ):
            raise ValueError("S3 artifact storage requires bucket and credentials")
    provenance_limits = (
        config.provenance.max_claim_chars,
        config.provenance.max_summary_chars,
        config.provenance.max_metadata_bytes,
        config.provenance.max_code_lines,
        config.provenance.max_excerpt_chars,
        config.provenance.max_evidence_per_claim,
    )
    if any(value <= 0 for value in provenance_limits):
        raise ValueError("provenance limits must be positive")
    if config.worker.distributed_enabled and storage_backend != "postgres":
        raise ValueError("distributed Worker ownership requires storage.backend=postgres")
    if config.worker.distributed_enabled and (
        config.worker.lease_seconds <= 0
        or config.worker.heartbeat_interval_seconds <= 0
        or config.worker.heartbeat_interval_seconds >= config.worker.lease_seconds
        or config.worker.poll_interval_seconds <= 0
    ):
        raise ValueError(
            "distributed Worker requires positive polling and heartbeat shorter than lease"
        )
    if (
        config.worker.max_run_delivery_attempts is not None
        and config.worker.max_run_delivery_attempts <= 0
    ):
        raise ValueError("worker.max_run_delivery_attempts must be positive or null")
    for name, value in (
        ("max_queued_runs", config.capacity.max_queued_runs),
        ("max_active_runs", config.capacity.max_active_runs),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"capacity.{name} must be positive or null")
    for name, value in (
        ("max_queued_runs_per_principal", config.capacity.max_queued_runs_per_principal),
        ("max_active_runs_per_principal", config.capacity.max_active_runs_per_principal),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"capacity.{name} must be positive or null")
    for name, value in (
        ("global_submission_rate", config.traffic.global_submission_rate),
        ("principal_submission_rate", config.traffic.principal_submission_rate),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"traffic.{name} must be positive or null")
    for name, value in (
        ("global_submission_burst", config.traffic.global_submission_burst),
        ("principal_submission_burst", config.traffic.principal_submission_burst),
    ):
        if value is not None and value <= 0:
            raise ValueError(f"traffic.{name} must be positive or null")
    if config.traffic.aging_interval_seconds <= 0 or config.traffic.aging_boost_cap < 0:
        raise ValueError("traffic aging interval must be positive and boost cap non-negative")
    if config.policy.network_access not in {"disabled", "public", "allowlist"}:
        raise ValueError("policy.network_access must be disabled, public, or allowlist")
    if config.policy.network_access == "allowlist" and not any(
        host.strip() for host in config.policy.allowed_network_hosts
    ):
        raise ValueError("policy.allowed_network_hosts is required for allowlist network access")
    if config.execution.backend not in {"local", "restricted", "sandbox"}:
        raise ValueError("execution.backend must be local, restricted, or sandbox")
    sandbox = config.execution.sandbox
    if not sandbox.controller_url.startswith(("http://", "https://")):
        raise ValueError("execution.sandbox.controller_url must be HTTP(S)")
    if not sandbox.image.strip() or not sandbox.workspace_source.strip():
        raise ValueError("execution.sandbox image and workspace source are required")
    if (
        sandbox.cpu_limit <= 0
        or sandbox.memory_limit_bytes <= 0
        or sandbox.pids_limit <= 0
        or sandbox.tmpfs_size_bytes <= 0
        or sandbox.request_max_bytes <= 0
        or sandbox.command_max_bytes <= 0
        or sandbox.command_max_bytes >= sandbox.request_max_bytes
    ):
        raise ValueError("execution.sandbox limits must be positive and request-bounded")
    gateway = config.provider_gateway
    if gateway.request_max_bytes <= 0 or gateway.max_messages <= 0:
        raise ValueError("provider_gateway request and message limits must be positive")
    if (
        gateway.max_tool_schema_bytes <= 0
        or gateway.max_tool_schema_bytes >= gateway.request_max_bytes
    ):
        raise ValueError("provider_gateway tool schema limit must be positive and request-bounded")
    if not gateway.routes:
        raise ValueError("provider_gateway.routes must define at least one route")
    target_ids: set[str] = set()
    for route, targets in gateway.routes.items():
        if not route.strip() or not targets:
            raise ValueError("provider gateway routes require a name and at least one target")
        for target in targets:
            if not all(
                value.strip()
                for value in (
                    target.id,
                    target.provider,
                    target.model,
                    target.base_url,
                    target.api_key_env,
                )
            ):
                raise ValueError("provider gateway target identity and endpoint are required")
            if target.id in target_ids:
                raise ValueError(f"provider gateway target id must be unique: {target.id}")
            target_ids.add(target.id)
            if not target.base_url.startswith(("http://", "https://")):
                raise ValueError("provider gateway target base_url must be HTTP(S)")
            if (
                target.context_window <= 0
                or target.max_tokens <= 0
                or target.timeout <= 0
                or target.max_concurrency <= 0
                or target.max_pending < 0
                or target.admission_timeout_seconds < 0
                or (target.requests_per_minute is not None and target.requests_per_minute <= 0)
                or target.failure_threshold <= 0
                or target.open_seconds <= 0
                or target.half_open_max_probes <= 0
                or target.rate_limit_cooldown_seconds <= 0
            ):
                raise ValueError("provider gateway target limits must be positive")
    return config


def get_config_paths(project_root: str | Path | None = None) -> list[Path]:
    paths = [_home() / ".axiom" / "config.json"]
    if project_root:
        paths.append(Path(project_root).resolve() / ".axiom" / "config.json")
    return paths


def config_to_public_dict(config: AxiomConfig) -> dict[str, Any]:
    data = _config_to_dict(config)
    if data.get("llm", {}).get("api_key"):
        data["llm"]["api_key"] = "***"
    if data.get("embedding", {}).get("api_key"):
        data["embedding"]["api_key"] = "***"
    if data.get("storage", {}).get("postgres_dsn"):
        data["storage"]["postgres_dsn"] = "***"
    if data.get("artifacts", {}).get("s3", {}).get("access_key"):
        data["artifacts"]["s3"]["access_key"] = "***"
    if data.get("artifacts", {}).get("s3", {}).get("secret_key"):
        data["artifacts"]["s3"]["secret_key"] = "***"
    return data


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        result[key] = value
    return result


def _apply_env(data: dict[str, Any], env: dict[str, str | None]) -> dict[str, Any]:
    result = deepcopy(data)
    llm = result.setdefault("llm", {})
    embedding = result.setdefault("embedding", {})
    features = result.setdefault("features", {})
    policy = result.setdefault("policy", {})
    execution = result.setdefault("execution", {})
    multi_agent = result.setdefault("multi_agent", {})
    plan = result.setdefault("plan", {})
    context = result.setdefault("context", {})
    run_budget = result.setdefault("run_budget", {})
    progress = result.setdefault("progress", {})
    storage = result.setdefault("storage", {})
    artifacts = result.setdefault("artifacts", {})
    provenance = result.setdefault("provenance", {})
    worker = result.setdefault("worker", {})
    capacity = result.setdefault("capacity", {})
    traffic = result.setdefault("traffic", {})
    provider_gateway = result.setdefault("provider_gateway", {})
    policy = result.setdefault("policy", {})

    storage_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_STORAGE_BACKEND", "backend", str),
        ("AXIOM_SQLITE_PATH", "sqlite_path", str),
        ("AXIOM_POSTGRES_DSN", "postgres_dsn", str),
        ("AXIOM_POSTGRES_POOL_MIN_SIZE", "pool_min_size", int),
        ("AXIOM_POSTGRES_POOL_MAX_SIZE", "pool_max_size", int),
        ("AXIOM_POSTGRES_CONNECT_TIMEOUT_SECONDS", "connect_timeout_seconds", float),
    ]
    for env_key, config_key, caster in storage_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                storage[config_key] = caster(raw)

    artifact_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_ARTIFACTS_ENABLED", "enabled", _as_bool),
        ("AXIOM_ARTIFACTS_BACKEND", "backend", str),
        ("AXIOM_ARTIFACTS_LOCAL_PATH", "local_path", str),
        ("AXIOM_ARTIFACTS_MAX_FILE_BYTES", "max_file_bytes", int),
        ("AXIOM_ARTIFACTS_MAX_METADATA_BYTES", "max_metadata_bytes", int),
    ]
    for env_key, config_key, caster in artifact_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                artifacts[config_key] = caster(raw)
    provenance_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_PROVENANCE_ENABLED", "enabled", _as_bool),
        (
            "AXIOM_PROVENANCE_PROMPT_GUIDANCE_ENABLED",
            "prompt_guidance_enabled",
            _as_bool,
        ),
        ("AXIOM_PROVENANCE_MAX_CLAIM_CHARS", "max_claim_chars", int),
        ("AXIOM_PROVENANCE_MAX_SUMMARY_CHARS", "max_summary_chars", int),
        ("AXIOM_PROVENANCE_MAX_METADATA_BYTES", "max_metadata_bytes", int),
        ("AXIOM_PROVENANCE_MAX_CODE_LINES", "max_code_lines", int),
        ("AXIOM_PROVENANCE_MAX_EXCERPT_CHARS", "max_excerpt_chars", int),
        (
            "AXIOM_PROVENANCE_MAX_EVIDENCE_PER_CLAIM",
            "max_evidence_per_claim",
            int,
        ),
    ]
    for env_key, config_key, caster in provenance_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                provenance[config_key] = caster(raw)
    artifact_s3 = artifacts.setdefault("s3", {})
    artifact_s3_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_ARTIFACT_S3_ENDPOINT", "endpoint", str),
        ("AXIOM_ARTIFACT_S3_BUCKET", "bucket", str),
        ("AXIOM_ARTIFACT_S3_ACCESS_KEY", "access_key", str),
        ("AXIOM_ARTIFACT_S3_SECRET_KEY", "secret_key", str),
        ("AXIOM_ARTIFACT_S3_SECURE", "secure", _as_bool),
        ("AXIOM_ARTIFACT_S3_REGION", "region", str),
    ]
    for env_key, config_key, caster in artifact_s3_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                artifact_s3[config_key] = caster(raw)

    worker_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_DISTRIBUTED_WORKER", "distributed_enabled", _as_bool),
        ("AXIOM_WORKER_LEASE_SECONDS", "lease_seconds", float),
        ("AXIOM_WORKER_HEARTBEAT_INTERVAL_SECONDS", "heartbeat_interval_seconds", float),
        ("AXIOM_WORKER_POLL_INTERVAL_SECONDS", "poll_interval_seconds", float),
        ("AXIOM_MAX_RUN_DELIVERY_ATTEMPTS", "max_run_delivery_attempts", int),
    ]
    for env_key, config_key, caster in worker_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                worker[config_key] = caster(raw)

    capacity_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_MAX_QUEUED_RUNS", "max_queued_runs", int),
        ("AXIOM_MAX_ACTIVE_RUNS", "max_active_runs", int),
        ("AXIOM_MAX_QUEUED_RUNS_PER_PRINCIPAL", "max_queued_runs_per_principal", int),
        ("AXIOM_MAX_ACTIVE_RUNS_PER_PRINCIPAL", "max_active_runs_per_principal", int),
    ]
    for env_key, config_key, caster in capacity_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                capacity[config_key] = caster(raw)

    traffic_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_GLOBAL_SUBMISSION_RATE", "global_submission_rate", float),
        ("AXIOM_GLOBAL_SUBMISSION_BURST", "global_submission_burst", int),
        ("AXIOM_PRINCIPAL_SUBMISSION_RATE", "principal_submission_rate", float),
        ("AXIOM_PRINCIPAL_SUBMISSION_BURST", "principal_submission_burst", int),
        ("AXIOM_AGING_INTERVAL_SECONDS", "aging_interval_seconds", float),
        ("AXIOM_AGING_BOOST_CAP", "aging_boost_cap", int),
    ]
    for env_key, config_key, caster in traffic_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                traffic[config_key] = caster(raw)

    mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_API_KEY", "api_key", str),
        ("AXIOM_PROVIDER", "provider", str),
        ("AXIOM_MODEL", "model", str),
        ("AXIOM_BASE_URL", "base_url", str),
        ("AXIOM_MAX_TOKENS", "max_tokens", int),
        ("AXIOM_TEMPERATURE", "temperature", float),
        ("AXIOM_GATEWAY_ROUTE", "route", str),
        ("AXIOM_GATEWAY_URL", "gateway_url", str),
    ]
    for env_key, config_key, caster in mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                llm[config_key] = caster(raw)

    gateway_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_PROVIDER_GATEWAY_REQUEST_MAX_BYTES", "request_max_bytes", int),
        ("AXIOM_PROVIDER_GATEWAY_MAX_MESSAGES", "max_messages", int),
        ("AXIOM_PROVIDER_GATEWAY_MAX_TOOL_SCHEMA_BYTES", "max_tool_schema_bytes", int),
    ]
    for env_key, config_key, caster in gateway_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                provider_gateway[config_key] = caster(raw)
    routes_json = env.get("AXIOM_PROVIDER_ROUTES_JSON")
    if routes_json not in (None, ""):
        with suppress(TypeError, ValueError, json.JSONDecodeError):
            parsed_routes = json.loads(str(routes_json))
            if isinstance(parsed_routes, dict):
                provider_gateway["routes"] = parsed_routes

    context_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_CONTEXT_WINDOW", "model_context_window", int),
        ("AXIOM_CONTEXT_RESERVED_OUTPUT_TOKENS", "reserved_output_tokens", int),
        ("AXIOM_CONTEXT_HIGH_WATERMARK", "high_watermark_ratio", float),
        ("AXIOM_CONTEXT_TARGET_RATIO", "target_after_compaction_ratio", float),
        ("AXIOM_CONTEXT_RECENT_MESSAGES", "recent_message_reserve", int),
        ("AXIOM_CONTEXT_HARD_INPUT_LIMIT", "hard_input_limit", int),
        ("AXIOM_CONTEXT_MAX_TOOL_RESULT_CHARS", "max_tool_result_chars", int),
    ]
    for env_key, config_key, caster in context_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                context[config_key] = caster(raw)

    budget_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_RUN_MAX_STEPS", "max_steps", int),
        ("AXIOM_RUN_MAX_MODEL_CALLS", "max_model_calls", int),
        ("AXIOM_RUN_MAX_TOOL_CALLS", "max_tool_calls", int),
        ("AXIOM_RUN_MAX_INPUT_TOKENS", "max_input_tokens", int),
        ("AXIOM_RUN_MAX_OUTPUT_TOKENS", "max_output_tokens", int),
        ("AXIOM_RUN_MAX_TOTAL_TOKENS", "max_total_tokens", int),
        ("AXIOM_RUN_MAX_WALL_TIME_SECONDS", "max_wall_time_seconds", float),
        ("AXIOM_RUN_MAX_COST_USD", "max_cost_usd", str),
        ("AXIOM_RUN_BUDGET_SOFT_LIMIT", "soft_limit_ratio", float),
    ]
    for env_key, config_key, caster in budget_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                run_budget[config_key] = caster(raw)

    progress_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_PROGRESS_MAX_IDENTICAL_ACTIONS", "max_identical_actions", int),
        ("AXIOM_PROGRESS_MAX_IDENTICAL_ERRORS", "max_identical_errors", int),
        ("AXIOM_PROGRESS_MAX_CYCLE_REPETITIONS", "max_cycle_repetitions", int),
        ("AXIOM_PROGRESS_MAX_STAGNANT_STEPS", "max_stagnant_steps", int),
        ("AXIOM_PROGRESS_MAX_RECOVERY_ATTEMPTS", "max_recovery_attempts", int),
        ("AXIOM_PROGRESS_HISTORY_LIMIT", "history_limit", int),
    ]
    enabled = env.get("AXIOM_PROGRESS_ENABLED")
    if enabled in {"true", "false"}:
        progress["enabled"] = enabled == "true"
    for env_key, config_key, caster in progress_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                progress[config_key] = caster(raw)

    embedding_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_EMBEDDING_PROVIDER", "provider", str),
        ("AXIOM_EMBEDDING_MODEL", "model", str),
        ("AXIOM_EMBEDDING_API_KEY", "api_key", str),
        ("AXIOM_EMBEDDING_BASE_URL", "base_url", str),
        ("AXIOM_EMBEDDING_DIMENSIONS", "dimensions", int),
        ("AXIOM_EMBEDDING_BATCH_SIZE", "batch_size", int),
        ("AXIOM_CODE_SEARCH_MODE", "search_mode", str),
    ]
    enabled = env.get("AXIOM_EMBEDDING_ENABLED")
    if enabled in {"true", "false"}:
        embedding["enabled"] = enabled == "true"
    for env_key, config_key, caster in embedding_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                embedding[config_key] = caster(raw)

    provider = str(llm.get("provider") or "").lower()
    if not llm.get("api_key"):
        provider_key_map = {
            "deepseek": "DEEPSEEK_API_KEY",
            "glm": "GLM_API_KEY",
            "zhipu": "GLM_API_KEY",
            "step": "STEP_API_KEY",
            "kimi": "KIMI_API_KEY",
            "moonshot": "KIMI_API_KEY",
            "freellmapi": "FREELLMAPI_API_KEY",
            "xfyun": "XFYUN_API_KEY",
            "agnes": "AGNES_API_KEY",
        }
        provider_key = provider_key_map.get(provider)
        if provider_key and env.get(provider_key):
            llm["api_key"] = env[provider_key]

    provider_model_key = f"{provider.upper()}_MODEL" if provider else ""
    provider_base_url_key = f"{provider.upper()}_BASE_URL" if provider else ""
    if provider_model_key and env.get(provider_model_key):
        llm["model"] = env[provider_model_key]
    if provider_base_url_key and env.get(provider_base_url_key):
        llm["base_url"] = env[provider_base_url_key]

    render_mode = env.get("AXIOM_RENDER_MODE") or env.get("AXIOM_RENDERER")
    if render_mode in {"plain", "inline"}:
        result["render_mode"] = render_mode

    if env.get("AXIOM_TUI") == "true":
        result["render_mode"] = "inline"

    network_access = env.get("AXIOM_NETWORK_ACCESS")
    if network_access in {"disabled", "public", "allowlist"}:
        policy["network_access"] = network_access
    network_hosts = env.get("AXIOM_NETWORK_ALLOWED_HOSTS")
    if network_hosts is not None:
        policy["allowed_network_hosts"] = [
            host.strip() for host in network_hosts.split(",") if host.strip()
        ]
    deny_private = env.get("AXIOM_NETWORK_DENY_PRIVATE")
    if deny_private in {"true", "false"}:
        policy["deny_private_networks"] = deny_private == "true"

    for env_key, feature_key in [
        ("AXIOM_MCP", "mcp"),
        ("AXIOM_SKILL", "skill"),
        ("AXIOM_MEMORY", "memory"),
    ]:
        raw = env.get(env_key)
        if raw == "false":
            features[feature_key] = False
        elif raw == "true":
            features[feature_key] = True

    hitl = env.get("AXIOM_HITL")
    if hitl in {"always", "auto", "never"}:
        policy["hitl_mode"] = hitl

    execution_backend = env.get("AXIOM_EXECUTION_BACKEND")
    if execution_backend in {"local", "restricted", "sandbox"}:
        execution["backend"] = execution_backend
    allowed_env = env.get("AXIOM_EXECUTION_ALLOWED_ENV")
    if allowed_env is not None:
        execution["allowed_env_names"] = [
            name.strip() for name in allowed_env.split(",") if name.strip()
        ]
    sandbox = execution.setdefault("sandbox", {})
    sandbox_mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_SANDBOX_CONTROLLER_URL", "controller_url", str),
        ("AXIOM_SANDBOX_IMAGE", "image", str),
        ("AXIOM_SANDBOX_WORKSPACE_SOURCE", "workspace_source", str),
        ("AXIOM_SANDBOX_DOCKER_SOCKET", "docker_socket_path", str),
        ("AXIOM_SANDBOX_CPU_LIMIT", "cpu_limit", float),
        ("AXIOM_SANDBOX_MEMORY_LIMIT_BYTES", "memory_limit_bytes", int),
        ("AXIOM_SANDBOX_PIDS_LIMIT", "pids_limit", int),
        ("AXIOM_SANDBOX_TMPFS_SIZE_BYTES", "tmpfs_size_bytes", int),
        ("AXIOM_SANDBOX_REQUEST_MAX_BYTES", "request_max_bytes", int),
        ("AXIOM_SANDBOX_COMMAND_MAX_BYTES", "command_max_bytes", int),
    ]
    for env_key, config_key, caster in sandbox_mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                sandbox[config_key] = caster(raw)
    max_parallel_workers = env.get("AXIOM_MULTI_AGENT_MAX_PARALLEL_WORKERS")
    if max_parallel_workers not in (None, ""):
        with suppress(TypeError, ValueError):
            multi_agent["max_parallel_workers"] = max(1, int(max_parallel_workers))
    max_parallel_tasks = env.get("AXIOM_PLAN_MAX_PARALLEL_TASKS")
    if max_parallel_tasks not in (None, ""):
        with suppress(TypeError, ValueError):
            plan["max_parallel_tasks"] = max(1, int(max_parallel_tasks))

    return result


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(target)
    for key, value in source.items():
        if value is None:
            continue
        old = result.get(key)
        if isinstance(old, dict) and isinstance(value, dict):
            result[key] = _deep_merge(old, value)
        else:
            result[key] = deepcopy(value)
    return result


def _config_to_dict(config: AxiomConfig) -> dict[str, Any]:
    return asdict(config)


def _dict_to_config(data: dict[str, Any]) -> AxiomConfig:
    execution_data = dict(data.get("execution", {}))
    execution_data["sandbox"] = SandboxConfig(**execution_data.get("sandbox", {}))
    gateway_data = dict(data.get("provider_gateway", {}))
    gateway_data["routes"] = {
        str(route): [ProviderTargetConfig(**target) for target in targets]
        for route, targets in gateway_data.get("routes", {}).items()
    }
    artifact_data = dict(data.get("artifacts", {}))
    artifact_data["s3"] = ArtifactS3Config(**artifact_data.get("s3", {}))
    return AxiomConfig(
        llm=LlmConfig(**data.get("llm", {})),
        provider_gateway=ProviderGatewayConfig(**gateway_data),
        embedding=EmbeddingConfig(**data.get("embedding", {})),
        render_mode=data.get("render_mode", "inline"),
        tools=ToolsConfig(**data.get("tools", {})),
        execution=ExecutionConfig(**execution_data),
        multi_agent=MultiAgentConfig(**data.get("multi_agent", {})),
        plan=PlanConfig(**data.get("plan", {})),
        mcp=McpConfig(**data.get("mcp", {})),
        memory=MemoryConfig(**data.get("memory", {})),
        context=ContextConfig(**data.get("context", {})),
        run_budget=RunBudgetConfig(**data.get("run_budget", {})),
        dependency=DependencyConfig(**data.get("dependency", {})),
        storage=StorageConfig(**data.get("storage", {})),
        artifacts=ArtifactConfig(**artifact_data),
        provenance=ProvenanceConfig(**data.get("provenance", {})),
        worker=WorkerConfig(**data.get("worker", {})),
        capacity=CapacityConfig(**data.get("capacity", {})),
        progress=ProgressConfig(**data.get("progress", {})),
        policy=PolicyConfig(**data.get("policy", {})),
        prompt=PromptConfig(**data.get("prompt", {})),
        features=FeatureConfig(**data.get("features", {})),
    )


def _expand_home(path: str) -> str:
    return str(Path(path).expanduser())


def _as_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")
