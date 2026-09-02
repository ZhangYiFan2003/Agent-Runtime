from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from axiom import __version__
from axiom.agent import QueryEngine


def build_evaluation_attribution(
    engine: QueryEngine,
    *,
    runtime_version: str | None = None,
) -> dict[str, Any]:
    """Build a secret-free, deterministic description of an evaluation environment."""

    llm = engine.llm_client
    model_configuration = {
        "provider": str(getattr(llm, "provider_name", engine.config.llm.provider)),
        "model": str(getattr(llm, "model_name", engine.config.llm.model)),
        "max_tokens": int(engine.config.llm.max_tokens),
        "temperature": float(engine.config.llm.temperature),
        "timeout": float(engine.config.llm.timeout),
        "max_context_window": int(getattr(llm, "max_context_window", 0) or 0),
    }
    tool_schema = engine.tool_registry.definitions()
    return {
        "runtime_version": runtime_version or __version__ or "unknown",
        "model_provider": model_configuration["provider"],
        "model_name": model_configuration["model"],
        "model_configuration": model_configuration,
        "model_configuration_hash": stable_fingerprint(model_configuration),
        "prompt_version": stable_fingerprint(engine.system_prompt),
        "tool_schema_version": stable_fingerprint(tool_schema),
        "policy_version": stable_fingerprint(asdict(engine.config.policy)),
        "context_policy_version": stable_fingerprint(asdict(engine.config.context)),
        "run_budget_policy_version": stable_fingerprint(
            {
                key: value
                for key, value in asdict(engine.config.run_budget).items()
                if key != "model_pricing"
            }
        ),
        "model_pricing_version": stable_fingerprint(engine.config.run_budget.model_pricing),
    }


def stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
