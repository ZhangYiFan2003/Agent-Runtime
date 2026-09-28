from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from benchmarks.deployment.client import event_timings, parse_sse_events
from benchmarks.deployment.common import (
    ComposeController,
    latency_summary,
    load_profile,
    nearest_rank,
    validate_project_name,
    write_report,
)
from benchmarks.deployment.fake_provider import _response_for
from benchmarks.deployment.run_faults import _safe_message


def test_nearest_rank_and_latency_summary_are_deterministic():
    assert nearest_rank([40, 10, 30, 20], 50) == 20
    assert nearest_rank([40, 10, 30, 20], 95) == 40
    assert latency_summary([]) == {
        "count": 0,
        "p50_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "max_ms": None,
    }


def test_profile_loader_rejects_unsafe_values_without_force(tmp_path: Path):
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "profiles": {
                    "unsafe": {
                        "workers": 50,
                        "concurrency": 1,
                        "runs": 1,
                        "warmups": 0,
                        "provider_latency_ms": 1,
                        "run_timeout_seconds": 5,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="--force"):
        load_profile(path, "unsafe")
    assert load_profile(path, "unsafe", force=True).workers == 50


def test_project_name_is_reserved():
    for project in ("axiom-stage15", "axiom-stage15-load"):
        assert validate_project_name(project) == project
    for project in ("axiom-stage15-Upper", "axiom-production", "axiom-stage15-../other"):
        with pytest.raises(ValueError, match="reserved"):
            validate_project_name(project)


def test_compose_controller_rejects_unknown_service_before_subprocess(monkeypatch):
    called = False

    def fail(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(subprocess, "run", fail)
    controller = ComposeController("axiom-stage15-test")
    with pytest.raises(ValueError, match="unsupported"):
        controller.stop("not-a-service")
    assert called is False


def test_compose_command_uses_argument_list_without_shell(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="container\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    controller = ComposeController("axiom-stage15-test")
    assert controller.service_container_ids("worker") == ["container"]
    assert captured["command"][:3] == ["docker", "compose", "--project-name"]
    assert "shell" not in captured["kwargs"]


def test_process_environment_scrubs_external_provider_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "do-not-forward")
    env = ComposeController("axiom-stage15-test").environment()
    assert env["OPENAI_API_KEY"] == ""
    assert env["DEEPSEEK_API_KEY"] == ""
    assert env["AXIOM_API_KEY"] == "stage15-fake-provider"


def test_report_writer_rejects_secret_fields_and_private_paths(tmp_path: Path):
    with pytest.raises(ValueError, match="secret-like"):
        write_report({"api_key": "value"}, tmp_path / "secret.json", "")
    with pytest.raises(ValueError, match="private paths"):
        write_report({"error": r"C:\Users\person\file"}, tmp_path / "path.json", "")


def test_fault_errors_redact_absolute_private_paths():
    message = _safe_message(RuntimeError(r"failed at C:\Users\person\workspace\file.py"))
    assert message == "failed at <local>"


def test_sse_parser_skips_malformed_frames_and_cursor_ids_remain_ordered():
    text = (
        'id: 2\nevent: run.completed\ndata: {"event_id":2,"event_type":"run.completed"}\n\n'
        "data: {broken\n\n"
        'id: 1\nevent: run.started\ndata: {"event_id":1,"event_type":"run.started"}\n\n'
    )
    assert [item["event_id"] for item in parse_sse_events(text)] == [1, 2]


def test_event_timing_uses_persisted_server_timestamps():
    events = [
        {"event_type": "turn.started", "timestamp": "2026-01-01T00:00:00+00:00"},
        {"event_type": "run.claimed", "timestamp": "2026-01-01T00:00:00.100000+00:00"},
        {"event_type": "run.completed", "timestamp": "2026-01-01T00:00:00.350000+00:00"},
    ]
    assert event_timings(events) == {
        "queue_wait_ms": 100.0,
        "run_ms": 250.0,
        "server_end_to_end_ms": 350.0,
    }


def test_fake_provider_fixture_covers_text_tool_and_followup():
    text = _response_for({"messages": [{"role": "user", "content": "hello"}]})
    shell = _response_for({"messages": [{"role": "user", "content": "STAGE15:SHELL execute"}]})
    followup = _response_for(
        {
            "messages": [
                {"role": "user", "content": "STAGE15:SHELL execute"},
                {"role": "tool", "content": "stage15"},
            ]
        }
    )
    assert b"stage15 deterministic response" in b"".join(text)
    assert b'"name":"bash"' in b"".join(shell)
    assert b"stage15 tool result recorded" in b"".join(followup)


def test_fault_scenario_manifest_is_complete_and_unique():
    path = Path(__file__).parents[1] / "benchmarks" / "deployment" / "scenarios.json"
    scenarios = json.loads(path.read_text(encoding="utf-8"))["scenarios"]
    identifiers = [item["id"] for item in scenarios]
    assert len(identifiers) == len(set(identifiers)) == 10
    assert {"worker-loss", "postgres-unavailable", "sandboxd-restart"} <= set(identifiers)
