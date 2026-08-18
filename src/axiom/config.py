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
    mcp: McpConfig = field(default_factory=McpConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
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
        mcp=McpConfig(**data.get("mcp", {})),
        memory=MemoryConfig(**data.get("memory", {})),
        policy=PolicyConfig(**data.get("policy", {})),
        prompt=PromptConfig(**data.get("prompt", {})),
        features=FeatureConfig(**data.get("features", {})),
    )


def _expand_home(path: str) -> str:
    return str(Path(path).expanduser())
