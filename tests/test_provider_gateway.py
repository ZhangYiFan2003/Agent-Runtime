from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
import pytest

from axiom.config import (
    AxiomConfig,
    LlmConfig,
    ProviderGatewayConfig,
    ProviderTargetConfig,
    RunBudgetConfig,
)
from axiom.llm import GatewayLlmClient, ProviderGatewayError, create_llm_client
from axiom.provider_gateway import (
    ProviderGateway,
    ProviderGatewayHttpServer,
    ProviderGatewayRequestError,
)
from axiom.runtime import (
    DurableAgentRuntime,
    MemoryCheckpointStore,
    MemoryObservabilityStore,
    ModelPricing,
    ModelPricingRegistry,
    ObservabilityService,
    RunBudgetPolicy,
    RunStatus,
    SpanType,
)
from axiom.runtime.budget import BudgetManager, UnknownModelPricingError
from axiom.runtime.dependency import DependencyFailureCategory, RetryClassifier
from axiom.runtime.models import Checkpoint
from axiom.runtime.observability_store import RunTracer
from axiom.tools import ToolRegistry
from axiom.types import Message


def _success(text: str = "ok") -> list[dict]:
    return [
        {"type": "message_start", "model": "upstream"},
        {"type": "text_delta", "text": text},
        {
            "type": "usage",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cached_input_tokens": 2,
                "reasoning_tokens": 3,
            },
        },
        {"type": "message_end", "stop_reason": "end_turn"},
    ]


def _status_error(status: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    headers = {"retry-after": retry_after} if retry_after else None
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("upstream failed", request=request, response=response)


@dataclass(slots=True)
class BlockingScript:
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)


class ScriptClient:
    provider_name = "fake"
    model_name = "fake"
    max_context_window = 10_000

    def __init__(self, script, factory) -> None:
        self.script = script
        self.factory = factory

    async def chat(self, messages, tools, *, system_prompt):
        self.factory.requests.append((messages, tools, system_prompt))
        if isinstance(self.script, BlockingScript):
            self.script.started.set()
            try:
                while not self.script.release.is_set():
                    await asyncio.sleep(0.005)
                for event in _success("released"):
                    yield event
            finally:
                if not self.script.release.is_set():
                    self.script.cancelled.set()
            return
        events = self.script if isinstance(self.script, list) else [self.script]
        for event in events:
            await asyncio.sleep(0)
            if isinstance(event, BaseException):
                raise event
            yield event


class ScriptFactory:
    def __init__(self, scripts: dict[str, list[object]]) -> None:
        self.scripts = {key: list(value) for key, value in scripts.items()}
        self.calls: list[tuple[str, str]] = []
        self.requests: list[tuple[list[Message], list[dict], str]] = []

    def __call__(self, target: ProviderTargetConfig, api_key: str) -> ScriptClient:
        self.calls.append((target.id, api_key))
        queue = self.scripts.setdefault(target.id, [])
        script = queue.pop(0) if queue else _success()
        return ScriptClient(script, self)


def _target(target_id: str, **overrides) -> ProviderTargetConfig:
    values = {
        "id": target_id,
        "provider": f"provider-{target_id}",
        "model": f"model-{target_id}",
        "base_url": f"https://{target_id}.invalid/v1",
        "api_key_env": f"KEY_{target_id.upper()}",
        "max_concurrency": 2,
        "max_pending": 2,
        "admission_timeout_seconds": 0.02,
    }
    values.update(overrides)
    return ProviderTargetConfig(**values)


def _gateway(
    targets: list[ProviderTargetConfig],
    factory: ScriptFactory,
    *,
    credentials: dict[str, str] | None = None,
    **config_overrides,
) -> ProviderGateway:
    config = ProviderGatewayConfig(routes={"default": targets}, **config_overrides)
    values = credentials or {}
    return ProviderGateway(
        config,
        client_factory=factory,
        credential_resolver=lambda name: values.get(name, ""),
    )


def _request(route: str = "default", *, tools: list[dict] | None = None) -> dict:
    return {
        "route": route,
        "messages": [
            {
                "role": "user",
                "content": "hello",
                "name": None,
                "tool_call_id": None,
                "tool_calls": [],
            }
        ],
        "tools": tools or [],
        "system_prompt": "test",
        "settings": {"max_tokens": 64, "temperature": 0.2, "timeout_seconds": 5},
    }


async def _collect(gateway: ProviderGateway, request: dict | None = None) -> list[dict]:
    return [event async for event in gateway.stream(request or _request())]


@contextmanager
def _server(gateway: ProviderGateway) -> Iterator[str]:
    server = ProviderGatewayHttpServer(gateway, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while server.port == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.port != 0
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_direct_mode_remains_default_and_gateway_is_opt_in():
    direct = LlmConfig()
    assert direct.provider == "deepseek"
    assert create_llm_client(direct).provider_name == "deepseek"

    gateway = create_llm_client(
        LlmConfig(provider="gateway", route="reasoning", gateway_url="http://gateway.invalid")
    )
    assert isinstance(gateway, GatewayLlmClient)
    assert gateway.provider_name == "gateway"
    assert gateway.model_name == "reasoning"


def test_primary_stream_preserves_normalized_events_tools_usage_and_identity():
    async def scenario():
        target = _target("primary")
        events = [
            {"type": "thinking_delta", "thinking": "reason"},
            {
                "type": "tool_call_delta",
                "tool_call": {"index": 0, "function": {"name": "bash", "arguments": "{"}},
            },
            *_success(),
        ]
        factory = ScriptFactory({"primary": [events]})
        gateway = _gateway(
            [target],
            factory,
            credentials={target.api_key_env: "test-provider-secret"},
        )

        result = await _collect(gateway, _request(tools=[{"type": "function"}]))

        assert result[0] == {
            "type": "provider_selected",
            "route": "default",
            "provider": target.provider,
            "model": target.model,
            "target_id": target.id,
            "gateway_wait_ms": result[0]["gateway_wait_ms"],
            "circuit_state": "CLOSED",
            "rate_limited": False,
            "skipped_targets": [],
        }
        usage = next(event for event in result if event["type"] == "usage")
        assert usage["provider"] == target.provider
        assert usage["usage"]["cached_input_tokens"] == 2
        assert usage["usage"]["reasoning_tokens"] == 3
        assert any(event["type"] == "tool_call_delta" for event in result)
        assert factory.calls == [("primary", "test-provider-secret")]
        assert "test-provider-secret" not in json.dumps(result)

    asyncio.run(scenario())


def test_upstream_failure_does_not_hidden_retry_and_next_attempt_uses_secondary():
    async def scenario():
        primary = _target("primary", failure_threshold=1)
        secondary = _target("secondary")
        factory = ScriptFactory(
            {"primary": [[_status_error(500)]], "secondary": [_success("fallback")]}
        )
        gateway = _gateway([primary, secondary], factory)

        first = await _collect(gateway)
        assert [call[0] for call in factory.calls] == ["primary"]
        assert first[-1]["type"] == "error"
        assert first[-1]["failure_category"] == "transient_server_error"

        second = await _collect(gateway)
        assert second[0]["target_id"] == "secondary"
        assert second[0]["skipped_targets"][0]["reason"] == "circuit_open"
        assert [call[0] for call in factory.calls] == ["primary", "secondary"]

    asyncio.run(scenario())


def test_partial_stream_failure_never_mixes_secondary_generation():
    async def scenario():
        primary = _target("primary", failure_threshold=1)
        secondary = _target("secondary")
        factory = ScriptFactory(
            {
                "primary": [[{"type": "text_delta", "text": "partial"}, _status_error(500)]],
                "secondary": [_success("must-not-appear")],
            }
        )
        result = await _collect(_gateway([primary, secondary], factory))
        assert [event.get("text") for event in result if event["type"] == "text_delta"] == [
            "partial"
        ]
        assert result[-1]["type"] == "error"
        assert [call[0] for call in factory.calls] == ["primary"]

    asyncio.run(scenario())


def test_auth_failure_is_clear_and_does_not_trip_circuit():
    async def scenario():
        primary = _target("primary", failure_threshold=1)
        factory = ScriptFactory({"primary": [[_status_error(401)], _success()]})
        gateway = _gateway([primary], factory)
        first = await _collect(gateway)
        second = await _collect(gateway)
        assert first[-1]["failure_category"] == "auth_error"
        assert second[0]["target_id"] == "primary"
        assert gateway.health()["targets"][0]["circuit_state"] == "CLOSED"

    asyncio.run(scenario())


def test_rate_limit_sets_cooldown_without_opening_circuit():
    async def scenario():
        primary = _target("primary", rate_limit_cooldown_seconds=10)
        secondary = _target("secondary")
        factory = ScriptFactory(
            {"primary": [[_status_error(429, retry_after="7")]], "secondary": [_success()]}
        )
        gateway = _gateway([primary, secondary], factory)
        first = await _collect(gateway)
        second = await _collect(gateway)
        assert first[-1]["retry_after_seconds"] == 7
        assert second[0]["target_id"] == "secondary"
        primary_health = next(
            item for item in gateway.health()["targets"] if item["id"] == "primary"
        )
        assert primary_health["circuit_state"] == "CLOSED"
        assert primary_health["cooldown_seconds"] > 0

    asyncio.run(scenario())


def test_open_target_transitions_half_open_and_success_closes_it():
    async def scenario():
        primary = _target("primary", failure_threshold=1, open_seconds=0.02)
        factory = ScriptFactory({"primary": [[_status_error(500)], _success()]})
        gateway = _gateway([primary], factory)
        await _collect(gateway)
        with pytest.raises(ProviderGatewayRequestError, match="no provider target"):
            await _collect(gateway)
        await asyncio.sleep(0.03)
        result = await _collect(gateway)
        assert result[0]["circuit_state"] == "HALF_OPEN"
        assert gateway.health()["targets"][0]["circuit_state"] == "CLOSED"

    asyncio.run(scenario())


def test_pending_admission_is_bounded():
    async def scenario():
        blocker = BlockingScript()
        primary = _target("primary", max_concurrency=1, max_pending=0)
        gateway = _gateway([primary], ScriptFactory({"primary": [blocker]}))
        first = asyncio.create_task(_collect(gateway))
        assert await asyncio.to_thread(blocker.started.wait, 1)
        with pytest.raises(ProviderGatewayRequestError) as captured:
            await _collect(gateway)
        assert captured.value.code == "PROVIDER_BUSY"
        blocker.release.set()
        await first

    asyncio.run(scenario())


def test_rpm_admission_rejects_without_calling_upstream_twice():
    async def scenario():
        primary = _target("primary", requests_per_minute=1)
        factory = ScriptFactory({"primary": [_success()]})
        gateway = _gateway([primary], factory)
        await _collect(gateway)
        with pytest.raises(ProviderGatewayRequestError) as captured:
            await _collect(gateway)
        assert captured.value.retry_after_seconds is not None
        assert len(factory.calls) == 1

    asyncio.run(scenario())


def test_two_gateway_clients_share_provider_concurrency_and_route_to_secondary():
    async def scenario(url: str, blocker: BlockingScript):
        first_client = GatewayLlmClient("default", url, timeout=5)
        second_client = GatewayLlmClient("default", url, timeout=5)

        async def collect(client):
            return [event async for event in client.chat([], [], system_prompt="test")]

        first = asyncio.create_task(collect(first_client))
        assert await asyncio.to_thread(blocker.started.wait, 1)
        second = await collect(second_client)
        blocker.release.set()
        await first
        assert second[0]["target_id"] == "secondary"

    blocker = BlockingScript()
    primary = _target("primary", max_concurrency=1, max_pending=0)
    secondary = _target("secondary")
    factory = ScriptFactory({"primary": [blocker], "secondary": [_success("secondary")]})
    gateway = _gateway([primary, secondary], factory)
    with _server(gateway) as url:
        asyncio.run(scenario(url, blocker))
    assert [call[0] for call in factory.calls] == ["primary", "secondary"]


def test_cancellation_closes_upstream_and_releases_capacity():
    async def scenario():
        blocker = BlockingScript()
        gateway = _gateway(
            [_target("primary", max_concurrency=1)],
            ScriptFactory({"primary": [blocker]}),
        )
        stream = gateway.stream(_request())
        selected = await anext(stream)
        assert selected["target_id"] == "primary"
        pending = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(blocker.started.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()
        assert blocker.cancelled.wait(1)
        assert gateway.health()["targets"][0]["active"] == 0

    asyncio.run(scenario())


def test_gateway_client_disconnect_cancels_providerd_upstream():
    async def scenario(url: str, blocker: BlockingScript):
        client = GatewayLlmClient("default", url, timeout=5)
        stream = client.chat([], [], system_prompt="test")
        selected = await anext(stream)
        assert selected["target_id"] == "primary"
        pending = asyncio.create_task(anext(stream))
        assert await asyncio.to_thread(blocker.started.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await stream.aclose()
        assert await asyncio.to_thread(blocker.cancelled.wait, 2)

    blocker = BlockingScript()
    gateway = _gateway([_target("primary")], ScriptFactory({"primary": [blocker]}))
    with _server(gateway) as url:
        asyncio.run(scenario(url, blocker))
    assert gateway.health()["targets"][0]["active"] == 0


def test_unknown_route_and_oversized_tools_are_rejected_before_upstream():
    async def scenario():
        factory = ScriptFactory({"primary": [_success()]})
        gateway = _gateway(
            [_target("primary")],
            factory,
            max_tool_schema_bytes=20,
            request_max_bytes=1000,
        )
        with pytest.raises(ProviderGatewayRequestError) as unknown:
            await _collect(gateway, _request("missing"))
        assert unknown.value.code == "PROVIDER_ROUTE_NOT_FOUND"
        with pytest.raises(ProviderGatewayRequestError, match="tool schemas"):
            await _collect(gateway, _request(tools=[{"description": "x" * 100}]))
        assert factory.calls == []

    asyncio.run(scenario())


def test_health_contains_governance_state_but_no_endpoint_or_credentials():
    target = _target("primary")
    gateway = _gateway(
        [target],
        ScriptFactory({}),
        credentials={target.api_key_env: "never-serialize-this"},
    )
    encoded = json.dumps(gateway.health())
    assert target.id in encoded
    assert target.provider in encoded
    assert target.base_url not in encoded
    assert target.api_key_env not in encoded
    assert "never-serialize-this" not in encoded


def test_gateway_client_is_fail_closed_when_providerd_is_unavailable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = GatewayLlmClient("default", f"http://127.0.0.1:{port}", timeout=0.1)

    async def scenario():
        with pytest.raises(ProviderGatewayError) as captured:
            await anext(client.chat([], [], system_prompt="test"))
        assert captured.value.code == "PROVIDER_GATEWAY_UNAVAILABLE"

    asyncio.run(scenario())


def test_gateway_error_preserves_retry_classifier_evidence():
    primary = _target("primary")
    gateway = _gateway(
        [primary],
        ScriptFactory({"primary": [[_status_error(429, retry_after="3")]]}),
    )

    async def scenario(url: str):
        client = GatewayLlmClient("default", url, timeout=2)
        events = [event async for event in client.chat([], [], system_prompt="test")]
        error = events[-1]["error"]
        classifier = RetryClassifier()
        assert classifier.classify(error) == DependencyFailureCategory.RATE_LIMITED
        assert classifier.retry_after_seconds(error) == 3

    with _server(gateway) as url:
        asyncio.run(scenario(url))


def test_actual_fallback_target_drives_cost_reconciliation():
    async def scenario():
        store = MemoryCheckpointStore()
        policy = RunBudgetPolicy(max_cost_usd=Decimal("1"))
        primary = ModelPricing(
            "provider-primary",
            "model-primary",
            Decimal("1"),
            Decimal("2"),
        )
        secondary = ModelPricing(
            "provider-secondary",
            "model-secondary",
            Decimal("5"),
            Decimal("7"),
        )
        manager = BudgetManager(
            store,
            policy=policy,
            provider="gateway",
            model="default",
            pricing_registry=ModelPricingRegistry([primary, secondary]),
            pricing_targets=[
                (primary.provider, primary.model),
                (secondary.provider, secondary.model),
            ],
        )
        state = Checkpoint.create(thread_id="thread", input="test", run_id="run")
        await manager.initialize(state)
        await manager.reserve_model_call(state, "call", estimated_input_tokens=10)
        result = await manager.complete_model_call(
            state,
            "call",
            input_tokens=10,
            output_tokens=5,
            provider=secondary.provider,
            model=secondary.model,
        )
        assert result.local_usage.cost_known is True
        assert result.local_usage.cost_usd == Decimal("0.000085")

    asyncio.run(scenario())


def test_unknown_actual_target_pricing_remains_unknown_not_zero():
    async def scenario():
        manager = BudgetManager(
            MemoryCheckpointStore(),
            policy=RunBudgetPolicy(),
            provider="gateway",
            model="default",
            pricing_targets=[("unknown", "model")],
        )
        state = Checkpoint.create(thread_id="thread", input="test", run_id="run")
        await manager.initialize(state)
        await manager.reserve_model_call(state, "call")
        result = await manager.complete_model_call(
            state,
            "call",
            input_tokens=2,
            output_tokens=1,
            provider="unknown",
            model="model",
        )
        assert result.local_usage.cost_known is False
        assert result.local_usage.cost_usd is None

    asyncio.run(scenario())


def test_hard_cost_budget_fails_if_selected_target_pricing_is_unknown():
    async def scenario():
        known = ModelPricing("known", "model", Decimal("1"), Decimal("1"))
        manager = BudgetManager(
            MemoryCheckpointStore(),
            policy=RunBudgetPolicy(max_cost_usd=Decimal("1")),
            provider="gateway",
            model="default",
            pricing_registry=ModelPricingRegistry([known]),
            pricing_targets=[("known", "model")],
        )
        state = Checkpoint.create(thread_id="thread", input="test", run_id="run")
        await manager.initialize(state)
        await manager.reserve_model_call(state, "call")
        with pytest.raises(UnknownModelPricingError) as captured:
            await manager.complete_model_call(
                state,
                "call",
                input_tokens=2,
                output_tokens=1,
                provider="unknown",
                model="fallback",
            )
        assert captured.value.code == "COST_PRICING_UNKNOWN"
        snapshot = await manager.snapshot(state)
        assert snapshot.local_usage.cost_known is False
        assert snapshot.local_usage.cost_usd is None

    asyncio.run(scenario())


def test_runtime_trace_and_metrics_record_actual_gateway_target(tmp_path):
    class GatewayEvents:
        provider_name = "gateway"
        model_name = "default"
        route_name = "default"
        max_context_window = 10_000

        async def chat(self, _messages, _tools, *, system_prompt):
            del system_prompt
            yield {
                "type": "provider_selected",
                "route": "default",
                "provider": "provider-secondary",
                "model": "model-secondary",
                "target_id": "secondary",
                "gateway_wait_ms": 4.5,
                "circuit_state": "CLOSED",
                "rate_limited": False,
            }
            for event in _success("complete"):
                yield event

    async def scenario():
        config = AxiomConfig()
        config.llm.provider = "gateway"
        config.llm.route = "default"
        config.provider_gateway.routes = {
            "default": [
                _target(
                    "secondary",
                    provider="provider-secondary",
                    model="model-secondary",
                )
            ]
        }
        config.run_budget = RunBudgetConfig(
            model_pricing={
                "provider-secondary/model-secondary": {
                    "input_per_million_usd": "2",
                    "output_per_million_usd": "4",
                }
            }
        )
        observations = MemoryObservabilityStore()
        runtime = DurableAgentRuntime(
            llm_client=GatewayEvents(),
            tool_registry=ToolRegistry(),
            system_prompt="test",
            cwd=str(tmp_path),
            config=config,
            store=MemoryCheckpointStore(),
            tracer=RunTracer(observations),
        )
        state = await runtime.start(thread_id="thread", input="hello", run_id="run-gateway")
        bundle = await ObservabilityService(observations).trace(state.run_id)
        metrics = await ObservabilityService(observations).metrics(state.run_id)
        assert state.status == RunStatus.COMPLETED
        assert bundle is not None and metrics is not None
        llm_span = next(span for span in bundle.spans if span.span_type == SpanType.LLM)
        assert llm_span.attributes["provider"] == "provider-secondary"
        assert llm_span.attributes["model"] == "model-secondary"
        assert llm_span.attributes["llm.route"] == "default"
        assert llm_span.attributes["provider.target"] == "secondary"
        assert llm_span.attributes["provider.gateway_wait_ms"] == 4.5
        assert metrics.provider_attempts == 1
        assert metrics.provider_targets == ("provider-secondary/model-secondary",)
        assert metrics.cost_known is True
        assert metrics.cost_usd == "0.00004"

    asyncio.run(scenario())
