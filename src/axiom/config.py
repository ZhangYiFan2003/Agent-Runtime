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

    mappings: list[tuple[str, str, Any]] = [
        ("AXIOM_API_KEY", "api_key", str),
        ("AXIOM_PROVIDER", "provider", str),
        ("AXIOM_MODEL", "model", str),
        ("AXIOM_BASE_URL", "base_url", str),
        ("AXIOM_MAX_TOKENS", "max_tokens", int),
        ("AXIOM_TEMPERATURE", "temperature", float),
    ]
    for env_key, config_key, caster in mappings:
        raw = env.get(env_key)
        if raw not in (None, ""):
            with suppress(TypeError, ValueError):
                llm[config_key] = caster(raw)

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
    if execution_backend in {"local", "restricted"}:
        execution["backend"] = execution_backend
    allowed_env = env.get("AXIOM_EXECUTION_ALLOWED_ENV")
    if allowed_env is not None:
        execution["allowed_env_names"] = [
            name.strip() for name in allowed_env.split(",") if name.strip()
        ]
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
    return AxiomConfig(
        llm=LlmConfig(**data.get("llm", {})),
        embedding=EmbeddingConfig(**data.get("embedding", {})),
        render_mode=data.get("render_mode", "inline"),
        tools=ToolsConfig(**data.get("tools", {})),
        execution=ExecutionConfig(**data.get("execution", {})),
        multi_agent=MultiAgentConfig(**data.get("multi_agent", {})),
        plan=PlanConfig(**data.get("plan", {})),
        mcp=McpConfig(**data.get("mcp", {})),
        memory=MemoryConfig(**data.get("memory", {})),
        context=ContextConfig(**data.get("context", {})),
        run_budget=RunBudgetConfig(**data.get("run_budget", {})),
        dependency=DependencyConfig(**data.get("dependency", {})),
        storage=StorageConfig(**data.get("storage", {})),
        progress=ProgressConfig(**data.get("progress", {})),
        policy=PolicyConfig(**data.get("policy", {})),
        prompt=PromptConfig(**data.get("prompt", {})),
        features=FeatureConfig(**data.get("features", {})),
    )


def _expand_home(path: str) -> str:
    return str(Path(path).expanduser())
