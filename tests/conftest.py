from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_user_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOMEDRIVE", home.drive or "C:")
    monkeypatch.setenv("HOMEPATH", os.sep + str(home).split(":\\", 1)[-1])


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Make every skip fatal in the dedicated real-PostgreSQL CI contract."""
    del exitstatus
    if os.environ.get("AXIOM_POSTGRES_CI_REQUIRED") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", ()) if reporter is not None else ()
    if skipped:
        reporter.write_sep(
            "=",
            f"PostgreSQL CI contract failed: {len(skipped)} test(s) skipped",
            red=True,
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
