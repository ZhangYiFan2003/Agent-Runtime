from __future__ import annotations

from axiom.config import LlmConfig
from axiom.llm.base import LlmClient
from axiom.llm.openai_compatible import OpenAICompatibleClient
from axiom.provider_gateway.client import GatewayLlmClient

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
PROVIDER_BASE_URLS = {
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "kimi": "https://api.moonshot.cn/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "step": "https://api.stepfun.com/v1",
}

MODEL_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4-pro": 1_000_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek-coder": 128_000,
}


def create_llm_client(config: LlmConfig) -> LlmClient:
    provider = config.provider.lower()
    if provider == "gateway":
        return GatewayLlmClient(
            route_name=config.route or config.model,
            gateway_url=config.gateway_url,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
        )
    if provider == "deepseek":
        base_url = config.base_url or DEEPSEEK_BASE_URL
        context = MODEL_CONTEXT_WINDOWS.get(config.model, 64_000)
        return OpenAICompatibleClient(
            provider_name="deepseek",
            model=config.model,
            api_key=config.api_key,
            base_url=base_url,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
            max_context_window=context,
            prompt_cache=True,
        )
    if provider in {"openai", "openai-compatible", "compatible"}:
        return OpenAICompatibleClient(
            provider_name=provider,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or OPENAI_BASE_URL,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
            max_context_window=128_000,
            prompt_cache=False,
        )
    if provider in PROVIDER_BASE_URLS:
        return OpenAICompatibleClient(
            provider_name=provider,
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url or PROVIDER_BASE_URLS[provider],
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout=config.timeout,
            max_context_window=128_000,
            prompt_cache=False,
        )
    return OpenAICompatibleClient(
        provider_name=provider,
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url or DEEPSEEK_BASE_URL,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        timeout=config.timeout,
        max_context_window=64_000,
        prompt_cache=False,
    )
