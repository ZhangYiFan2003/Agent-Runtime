from __future__ import annotations

import asyncio
import json
import socket

import httpx
import pytest

from axiom.config import AxiomConfig, load_config
from axiom.policy import (
    AuditLog,
    Capability,
    DefaultPermissionPolicy,
    NetworkPolicy,
    NetworkPolicyError,
    PermissionAction,
    PermissionRequest,
    redact_secrets,
)
from axiom.tools.base import ToolContext
from axiom.tools.builtins import get_builtin_tools
from axiom.tools.executor import ToolExecutor
from axiom.tools.registry import ToolRegistry


def _request(tmp_path, *, capabilities, arguments, paths=()):
    return PermissionRequest(
        run_id="run",
        thread_id="thread",
        turn_id="turn",
        invocation_id="invocation",
        tool_name="test",
        capabilities=tuple(capabilities),
        arguments=arguments,
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        resource_paths=tuple(paths),
    )


def test_default_permission_policy_blocks_sensitive_files(tmp_path):
    async def scenario():
        decision = await DefaultPermissionPolicy(tmp_path).evaluate(
            _request(
                tmp_path,
                capabilities=(Capability.FILESYSTEM_READ.value,),
                arguments={"path": ".env"},
                paths=(str(tmp_path / ".env"),),
            )
        )
        assert decision.action == PermissionAction.DENY
        assert decision.matched_rule == "filesystem.sensitive_path"

    asyncio.run(scenario())


def test_network_policy_rejects_private_and_non_http_targets():
    policy = NetworkPolicy()
    with pytest.raises(NetworkPolicyError):
        policy.validate_url("file:///etc/passwd")
    for unsafe in (
        "https://user:pass@example.com/",
        "http://localhost:8080/",
        "http://127.0.0.1:8080/",
        "http://[::1]:8080/",
        "http://10.0.0.1/",
        "http://[fd00::1]/",
        "http://169.254.169.254/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
    ):
        with pytest.raises(NetworkPolicyError):
            policy.validate_url(unsafe)


def test_allowlist_network_policy_uses_host_boundary(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
    )
    policy = NetworkPolicy(access="allowlist", allowed_hosts=("example.com",))
    policy.validate_url("https://www.example.com/page")
    with pytest.raises(NetworkPolicyError):
        policy.validate_url("https://example.com.evil.test/page")


def test_web_fetch_revalidates_redirect_target(monkeypatch):
    from axiom.web import fetch as fetch_module

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
    )

    class Response:
        def __init__(self, url, status_code, headers=None):
            self.url = httpx.URL(url)
            self.status_code = status_code
            self.headers = headers or {}
            self.text = "ok"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("error", request=None, response=self)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **_kwargs):
            assert url == "https://public.example/"
            return Response(url, 302, {"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(fetch_module.httpx, "AsyncClient", lambda **_kwargs: Client())

    async def scenario():
        with pytest.raises(NetworkPolicyError):
            await fetch_module.fetch_url("https://public.example/")

    asyncio.run(scenario())


def test_network_permission_is_fail_closed_when_disabled(tmp_path):
    async def scenario():
        config = AxiomConfig()
        config.policy.network_access = "disabled"
        decision = await DefaultPermissionPolicy.from_config(tmp_path, config).evaluate(
            _request(
                tmp_path,
                capabilities=(Capability.NETWORK_READ.value,),
                arguments={"url": "https://example.com"},
            )
        )
        assert decision.action == PermissionAction.DENY
        assert decision.matched_rule == "network.policy"

    asyncio.run(scenario())


def test_web_fetch_is_blocked_before_handler_when_network_disabled(tmp_path):
    async def scenario():
        config = AxiomConfig()
        config.policy.network_access = "disabled"
        registry = ToolRegistry()
        registry.register_all([tool for tool in get_builtin_tools() if tool.name == "web_fetch"])
        result = await ToolExecutor(registry).execute_one(
            {
                "id": "blocked-fetch",
                "name": "web_fetch",
                "arguments": {"url": "https://example.com"},
            },
            ToolContext(cwd=str(tmp_path), workspace=str(tmp_path), config=config),
        )
        assert result.is_error
        assert result.metadata["error_type"] == "PermissionDenied"
        assert "network access is disabled" in result.content

    asyncio.run(scenario())


def test_invalid_network_configuration_fails_fast():
    with pytest.raises(ValueError, match="network_access"):
        load_config(overrides={"policy": {"network_access": "everything"}})


def test_audit_redacts_structured_secret_fields(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.record(
        tool_name="test",
        input_data={
            "api_key": "synthetic-api-key",
            "postgresql_dsn": "postgresql://user:secret@db/app",
            "nested": {"authorization": "Bearer synthetic-token"},
        },
        outcome="allow",
        approver="none",
        cwd=str(tmp_path),
    )
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "synthetic-api-key" not in raw
    assert "postgresql://user:secret@db/app" not in raw
    assert "synthetic-token" not in raw
    assert json.loads(raw)["input"]["postgresql_dsn"] == "***"


def test_shared_redaction_preserves_non_secret_metrics():
    value = redact_secrets(
        {"api_key": "dummy-api-key", "token_count": 12, "nested": {"password": "dummy"}}
    )
    assert value == {"api_key": "***", "token_count": 12, "nested": {"password": "***"}}
