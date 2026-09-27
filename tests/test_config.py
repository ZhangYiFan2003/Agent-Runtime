from __future__ import annotations

import json

import pytest

from axiom.config import load_config


def test_config_precedence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    (home / ".axiom").mkdir(parents=True)
    (project / ".axiom").mkdir(parents=True)
    (home / ".axiom" / "config.json").write_text(
        json.dumps({"llm": {"provider": "home", "model": "home-model"}}),
        encoding="utf-8",
    )
    (project / ".axiom" / "config.json").write_text(
        json.dumps({"llm": {"provider": "project", "model": "project-model"}}),
        encoding="utf-8",
    )
    (project / ".env").write_text("AXIOM_MODEL=env-file-model\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AXIOM_PROVIDER", "process")

    config = load_config(
        project_root=project,
        overrides={"llm": {"model": "cli-model"}},
    )

    assert config.llm.provider == "process"
    assert config.llm.model == "cli-model"


def test_provider_specific_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AXIOM_PROVIDER", "deepseek")
    monkeypatch.delenv("AXIOM_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    config = load_config(project_root=tmp_path)

    assert config.llm.api_key == "deepseek-key"


def test_multi_agent_parallelism_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AXIOM_MULTI_AGENT_MAX_PARALLEL_WORKERS", "3")

    config = load_config(project_root=tmp_path)

    assert config.multi_agent.max_parallel_workers == 3


def test_plan_parallelism_environment_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AXIOM_PLAN_MAX_PARALLEL_TASKS", "3")

    config = load_config(project_root=tmp_path)

    assert config.plan.max_parallel_tasks == 3


def test_capacity_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(
        project_root=tmp_path,
        env={
            "AXIOM_MAX_QUEUED_RUNS": "7",
            "AXIOM_MAX_ACTIVE_RUNS": "3",
        },
    )

    assert config.capacity.max_queued_runs == 7
    assert config.capacity.max_active_runs == 3


def test_capacity_limits_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="capacity.max_active_runs"):
        load_config(
            project_root=tmp_path,
            overrides={"capacity": {"max_active_runs": 0}},
            env={},
        )


def test_run_delivery_limit_is_optional_and_configurable(tmp_path):
    default = load_config(project_root=tmp_path, env={})
    configured = load_config(
        project_root=tmp_path,
        env={"AXIOM_MAX_RUN_DELIVERY_ATTEMPTS": "3"},
    )

    assert default.worker.max_run_delivery_attempts is None
    assert configured.worker.max_run_delivery_attempts == 3


def test_run_delivery_limit_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="max_run_delivery_attempts"):
        load_config(
            project_root=tmp_path,
            overrides={"worker": {"max_run_delivery_attempts": 0}},
            env={},
        )


@pytest.mark.parametrize(
    ("worker", "message"),
    [
        ({"lease_seconds": 0}, "heartbeat shorter than lease"),
        (
            {"lease_seconds": 10, "heartbeat_interval_seconds": 10},
            "heartbeat shorter than lease",
        ),
        ({"poll_interval_seconds": 0}, "positive polling"),
    ],
)
def test_distributed_worker_rejects_unsafe_timing(tmp_path, worker, message):
    with pytest.raises(ValueError, match=message):
        load_config(
            project_root=tmp_path,
            overrides={
                "storage": {"backend": "postgres", "postgres_dsn": "postgresql://local/test"},
                "worker": {"distributed_enabled": True, **worker},
            },
            env={},
        )


@pytest.mark.parametrize(
    "storage",
    [
        {"backend": "postgres", "postgres_dsn": ""},
        {"backend": "postgres", "postgres_dsn": "postgresql://local/test", "pool_min_size": 0},
        {
            "backend": "postgres",
            "postgres_dsn": "postgresql://local/test",
            "pool_min_size": 3,
            "pool_max_size": 2,
        },
        {
            "backend": "postgres",
            "postgres_dsn": "postgresql://local/test",
            "connect_timeout_seconds": 0,
        },
    ],
)
def test_postgres_storage_configuration_fails_fast(tmp_path, storage):
    with pytest.raises(ValueError, match="postgres|PostgreSQL"):
        load_config(project_root=tmp_path, overrides={"storage": storage}, env={})


def test_sqlite_does_not_require_postgres_pool_configuration(tmp_path):
    config = load_config(
        project_root=tmp_path,
        overrides={"storage": {"backend": "sqlite", "pool_min_size": 0}},
        env={},
    )
    assert config.storage.backend == "sqlite"


def test_provider_gateway_routes_use_existing_config_and_env_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    routes = {
        "fast": [
            {
                "id": "local-fake",
                "provider": "fake",
                "model": "fast-model",
                "base_url": "http://provider.invalid/v1",
                "api_key_env": "FAKE_PROVIDER_KEY",
                "max_concurrency": 2,
                "max_pending": 1,
            }
        ]
    }
    config = load_config(
        project_root=tmp_path,
        env={
            "AXIOM_PROVIDER": "gateway",
            "AXIOM_GATEWAY_ROUTE": "fast",
            "AXIOM_GATEWAY_URL": "http://providerd:8070",
            "AXIOM_PROVIDER_ROUTES_JSON": json.dumps(routes),
        },
    )

    target = config.provider_gateway.routes["fast"][0]
    assert config.llm.provider == "gateway"
    assert config.llm.route == "fast"
    assert target.provider == "fake"
    assert target.api_key_env == "FAKE_PROVIDER_KEY"
