from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from benchmarks.deployment.client import RuntimeClient, RuntimeHttpError
from benchmarks.deployment.common import (
    DEFAULT_OUTPUT,
    ComposeController,
    environment_metadata,
    utc_now,
    write_report,
)

SCENARIO_FILE = Path(__file__).with_name("scenarios.json")
Scenario = Callable[[ComposeController, RuntimeClient, float], Awaitable[dict[str, Any]]]


async def _submit(client: RuntimeClient, prompt: str) -> tuple[str, str]:
    thread_id = await client.create_thread()
    response = await client.submit_turn(thread_id, prompt)
    run_id = response.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("distributed Turn did not return a Run id")
    return thread_id, run_id


async def _wait_terminal_with_approval(
    client: RuntimeClient,
    run_id: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    approved: set[str] = set()
    last: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            last = await client.run(run_id)
            status = str(last.get("status") or "")
            if status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return last
            if status == "WAITING_APPROVAL":
                interrupt = last.get("interrupt")
                if isinstance(interrupt, dict):
                    invocation_id = interrupt.get("invocation_id")
                    if isinstance(invocation_id, str) and invocation_id not in approved:
                        await client.resume(
                            run_id,
                            decision="approve",
                            invocation_id=invocation_id,
                        )
                        approved.add(invocation_id)
        except (httpx.TransportError, RuntimeHttpError):
            pass
        await asyncio.sleep(0.25)
    status = last.get("status") if last else "unavailable"
    raise TimeoutError(f"Run {run_id} did not terminate; last status={status}")


async def _run_and_wait(
    client: RuntimeClient,
    prompt: str,
    *,
    timeout: float,
) -> tuple[str, str, dict[str, Any]]:
    thread_id, run_id = await _submit(client, prompt)
    state = await _wait_terminal_with_approval(client, run_id, timeout=timeout)
    return thread_id, run_id, state


async def _probe_success(client: RuntimeClient, *, timeout: float = 35) -> dict[str, Any]:
    _thread_id, run_id, state = await _run_and_wait(
        client,
        "STAGE15:RECOVERY deterministic response",
        timeout=timeout,
    )
    return {"run_id": run_id, "status": state.get("status")}


async def worker_loss(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    thread_id, run_id = await _submit(client, "STAGE15:SLOW worker ownership loss")
    claimed = await client.wait_for_event(
        thread_id,
        run_id,
        {"run.claimed"},
        timeout=15,
    )
    worker_id = str(claimed.get("worker_id") or "")
    hostname = worker_id.split(":", 1)[0]
    worker_containers = await asyncio.to_thread(controller.service_container_ids, "worker")
    owner = next((item for item in worker_containers if item.startswith(hostname)), None)
    if owner is None:
        raise RuntimeError("could not map the claimed Worker id to a Stage15 container")
    recovery_started = time.perf_counter()
    await asyncio.to_thread(controller.kill_container, owner, expected_service="worker")
    state = await client.wait_for_run(run_id, timeout=timeout)
    recovery_ms = _elapsed_ms(recovery_started)
    events = await client.events(thread_id, run_id=run_id)
    takeover = [item for item in events if item.get("event_type") == "run.taken_over"]
    await asyncio.to_thread(controller.up, workers=2)
    return {
        "run_id": run_id,
        "status": state.get("status"),
        "recovery_ms": recovery_ms,
        "takeover_observed": bool(takeover),
        "duplicate_event_ids": len(events) - len({item.get("event_id") for item in events}),
        "verified": state.get("status") == "COMPLETED" and bool(takeover),
    }


async def providerd_unavailable(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    await asyncio.to_thread(controller.stop, "providerd")
    try:
        _thread_id, run_id, state = await _run_and_wait(
            client,
            "STAGE15:DEPENDENCY providerd unavailable",
            timeout=timeout,
        )
    finally:
        recovery_started = time.perf_counter()
        await asyncio.to_thread(controller.start, "providerd")
    recovered = await _probe_success(client)
    return {
        "run_id": run_id,
        "fault_status": state.get("status"),
        "recovery_probe": recovered,
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": state.get("status") == "FAILED" and recovered["status"] == "COMPLETED",
    }


async def provider_target_500(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    await asyncio.to_thread(controller.recreate, "fake-provider", STAGE15_PROVIDER_MODE="500")
    await asyncio.to_thread(controller.restart, "providerd")
    try:
        _thread_id, run_id, state = await _run_and_wait(
            client,
            "STAGE15:DEPENDENCY upstream 500",
            timeout=timeout,
        )
    finally:
        recovery_started = time.perf_counter()
        await asyncio.to_thread(
            controller.recreate, "fake-provider", STAGE15_PROVIDER_MODE="healthy"
        )
        await asyncio.to_thread(controller.restart, "providerd")
    recovered = await _probe_success(client)
    return {
        "run_id": run_id,
        "fault_status": state.get("status"),
        "recovery_probe": recovered,
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": state.get("status") == "FAILED" and recovered["status"] == "COMPLETED",
    }


async def sandboxd_unavailable(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    thread_id, run_id = await _submit(client, "STAGE15:SHELL sandboxd unavailable")
    waiting = await client.wait_for_run(run_id, timeout=20, statuses={"WAITING_APPROVAL"})
    interrupt = waiting.get("interrupt")
    if not isinstance(interrupt, dict) or not isinstance(interrupt.get("invocation_id"), str):
        raise RuntimeError("Shell Run did not expose an approval invocation")
    await asyncio.to_thread(controller.stop, "sandboxd")
    try:
        await client.resume(
            run_id,
            decision="approve",
            invocation_id=str(interrupt["invocation_id"]),
        )
        state = await client.wait_for_run(run_id, timeout=timeout)
        events = await client.events(thread_id, run_id=run_id)
    finally:
        recovery_started = time.perf_counter()
        await asyncio.to_thread(controller.start, "sandboxd")
    failed_closed = any(
        item.get("event_type") == "tool.completed" and item.get("is_error") is True
        for item in events
    )
    _probe_thread, probe_run, probe = await _run_and_wait(
        client,
        "STAGE15:SHELL sandbox recovered",
        timeout=45,
    )
    return {
        "run_id": run_id,
        "fault_status": state.get("status"),
        "failed_closed": failed_closed,
        "recovery_probe": {"run_id": probe_run, "status": probe.get("status")},
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": failed_closed and probe.get("status") == "COMPLETED",
    }


async def minio_unavailable(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    await asyncio.to_thread(controller.stop, "minio")
    try:
        _thread_id, run_id, state = await _run_and_wait(
            client,
            "STAGE15:ARTIFACT MinIO unavailable",
            timeout=timeout,
        )
        artifacts = await client.artifacts(run_id)
    finally:
        recovery_started = time.perf_counter()
        await asyncio.to_thread(controller.start, "minio")
        await asyncio.to_thread(
            controller.run,
            ["up", "-d", "--no-deps", "--force-recreate"],
            services=["minio-init"],
            capture_output=False,
        )
    _thread_id, probe_run, probe = await _run_and_wait(
        client,
        "STAGE15:ARTIFACT recovered publish",
        timeout=45,
    )
    recovered_artifacts = await client.artifacts(probe_run)
    digest_ok = False
    if recovered_artifacts:
        artifact_id = recovered_artifacts[0].get("artifact_id")
        if isinstance(artifact_id, str):
            content = await client.artifact_content(artifact_id)
            digest_ok = hashlib.sha256(content).hexdigest() == recovered_artifacts[0].get("sha256")
    return {
        "run_id": run_id,
        "fault_status": state.get("status"),
        "fault_artifact_count": len(artifacts),
        "recovery_probe": {
            "run_id": probe_run,
            "status": probe.get("status"),
            "artifact_count": len(recovered_artifacts),
            "content_digest_matches": digest_ok,
        },
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": len(artifacts) == 0 and bool(recovered_artifacts) and digest_ok,
    }


async def postgres_unavailable(
    controller: ComposeController, client: RuntimeClient, _timeout: float
) -> dict[str, Any]:
    before = await client.health()
    before_runs = len(await client.list_runs())
    await asyncio.to_thread(controller.stop, "postgres")
    unavailable_observed = False
    try:
        try:
            await client.create_thread()
        except (httpx.TransportError, RuntimeHttpError, httpx.HTTPError):
            unavailable_observed = True
    finally:
        recovery_started = time.perf_counter()
        await asyncio.to_thread(controller.start, "postgres")
    await _wait_runtime_health(client, timeout=40)
    after = await client.health()
    after_runs = len(await client.list_runs())
    return {
        "storage_backend_before": before.get("storage_backend"),
        "storage_backend_after": after.get("storage_backend"),
        "authority_unavailable_observed": unavailable_observed,
        "run_count_before": before_runs,
        "run_count_after": after_runs,
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": (
            before.get("storage_backend") == "postgres"
            and after.get("storage_backend") == "postgres"
            and unavailable_observed
            and after_runs >= before_runs
        ),
    }


async def runtime_api_restart(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    thread_id, run_id = await _submit(client, "STAGE15:SLOW runtime API restart")
    await client.wait_for_event(thread_id, run_id, {"run.claimed"}, timeout=15)
    recovery_started = time.perf_counter()
    await asyncio.to_thread(controller.restart, "runtime-api")
    await _wait_runtime_health(client, timeout=40)
    state = await client.wait_for_run(run_id, timeout=timeout)
    return {
        "run_id": run_id,
        "status": state.get("status"),
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": state.get("status") == "COMPLETED",
    }


async def web_restart(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    thread_id, run_id = await _submit(client, "STAGE15:SLOW web restart")
    await client.wait_for_event(thread_id, run_id, {"run.claimed"}, timeout=15)
    recovery_started = time.perf_counter()
    await asyncio.to_thread(controller.restart, "web")
    await _wait_runtime_health(client, timeout=30)
    state = await client.wait_for_run(run_id, timeout=timeout)
    return {
        "run_id": run_id,
        "status": state.get("status"),
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": state.get("status") == "COMPLETED",
    }


async def providerd_state_reset(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    await asyncio.to_thread(controller.recreate, "fake-provider", STAGE15_PROVIDER_MODE="500")
    await asyncio.to_thread(controller.restart, "providerd")
    try:
        _thread_id, _run_id, _state = await _run_and_wait(
            client,
            "STAGE15:DEPENDENCY open provider circuit",
            timeout=timeout,
        )
        before = await asyncio.to_thread(_provider_target_summary, controller)
        await asyncio.to_thread(controller.restart, "providerd")
        after = await asyncio.to_thread(_provider_target_summary, controller)
    finally:
        recovery_started = time.perf_counter()
        await asyncio.to_thread(
            controller.recreate, "fake-provider", STAGE15_PROVIDER_MODE="healthy"
        )
        await asyncio.to_thread(controller.restart, "providerd")
    recovered = await _probe_success(client)
    return {
        "before_restart": before,
        "after_restart": after,
        "recovery_probe": recovered,
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": (
            int(before.get("attempts") or 0) > 0
            and int(after.get("attempts") or 0) == 0
            and str(after.get("circuit_state") or "").casefold() == "closed"
            and recovered["status"] == "COMPLETED"
        ),
    }


async def sandboxd_restart(
    controller: ComposeController, client: RuntimeClient, timeout: float
) -> dict[str, Any]:
    thread_id, run_id = await _submit(client, "STAGE15:SHELL sandboxd restart")
    waiting = await client.wait_for_run(run_id, timeout=20, statuses={"WAITING_APPROVAL"})
    interrupt = waiting.get("interrupt")
    if not isinstance(interrupt, dict) or not isinstance(interrupt.get("invocation_id"), str):
        raise RuntimeError("Shell Run did not expose an approval invocation")
    await client.resume(
        run_id,
        decision="approve",
        invocation_id=str(interrupt["invocation_id"]),
    )
    await client.wait_for_event(thread_id, run_id, {"tool.started"}, timeout=20)
    active_before = await asyncio.to_thread(controller.sandbox_container_ids, run_id)
    recovery_started = time.perf_counter()
    await asyncio.to_thread(controller.restart, "sandboxd")
    state = await client.wait_for_run(run_id, timeout=timeout)
    await asyncio.sleep(0.5)
    remaining = await asyncio.to_thread(controller.sandbox_container_ids, run_id)
    _probe_thread, probe_run, probe = await _run_and_wait(
        client,
        "STAGE15:SHELL sandboxd rediscovery probe",
        timeout=45,
    )
    return {
        "run_id": run_id,
        "status": state.get("status"),
        "managed_container_observed": bool(active_before),
        "leaked_containers_after_terminal": len(remaining),
        "recovery_probe": {"run_id": probe_run, "status": probe.get("status")},
        "recovery_ms": _elapsed_ms(recovery_started),
        "verified": (
            state.get("status") == "COMPLETED"
            and bool(active_before)
            and not remaining
            and probe.get("status") == "COMPLETED"
        ),
    }


SCENARIOS: dict[str, Scenario] = {
    "worker-loss": worker_loss,
    "providerd-unavailable": providerd_unavailable,
    "provider-target-500": provider_target_500,
    "sandboxd-unavailable": sandboxd_unavailable,
    "minio-unavailable": minio_unavailable,
    "postgres-unavailable": postgres_unavailable,
    "runtime-api-restart": runtime_api_restart,
    "web-restart": web_restart,
    "providerd-state-reset": providerd_state_reset,
    "sandboxd-restart": sandboxd_restart,
}


async def _wait_runtime_health(client: RuntimeClient, *, timeout: float) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            health = await client.health()
            if health.get("status") == "ok" and health.get("database") == "ok":
                return health
        except (httpx.TransportError, RuntimeHttpError):
            pass
        await asyncio.sleep(0.5)
    raise TimeoutError("Runtime health did not recover")


def _provider_target_summary(controller: ComposeController) -> dict[str, Any]:
    targets = controller.provider_health().get("targets")
    if not isinstance(targets, list) or not targets or not isinstance(targets[0], dict):
        raise RuntimeError("providerd target health is unavailable")
    return {
        key: targets[0].get(key)
        for key in (
            "id",
            "circuit_state",
            "active",
            "pending",
            "attempts",
            "failures",
            "circuit_rejections",
            "busy_rejections",
        )
    }


async def _consistency_samples(client: RuntimeClient) -> dict[str, Any]:
    _thread_id, artifact_run, artifact_state = await _run_and_wait(
        client,
        "STAGE15:ARTIFACT consistency sample",
        timeout=45,
    )
    artifacts = await client.artifacts(artifact_run)
    _thread_id, claim_run, claim_state = await _run_and_wait(
        client,
        "STAGE15:PROVENANCE consistency sample",
        timeout=45,
    )
    claims = await client.claims(claim_run)
    return {
        "artifact_run_status": artifact_state.get("status"),
        "artifact_count": len(artifacts),
        "claim_run_status": claim_state.get("status"),
        "claim_count": len(claims),
        "consistent": (
            artifact_state.get("status") == "COMPLETED"
            and len(artifacts) == 1
            and claim_state.get("status") == "COMPLETED"
            and len(claims) == 1
        ),
    }


async def async_main(args: argparse.Namespace) -> int:
    definitions = json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))["scenarios"]
    by_id = {item["id"]: item for item in definitions}
    selected = args.scenarios or list(SCENARIOS)
    unknown = [item for item in selected if item not in SCENARIOS]
    if unknown:
        raise ValueError(f"unknown scenarios: {', '.join(unknown)}")
    controller = ComposeController(args.project, web_port=args.web_port)
    results: list[dict[str, Any]] = []
    consistency: dict[str, Any] = {"consistent": False, "error": "not run"}
    capacity: Any = None
    provider: dict[str, Any] = {"available": False}
    started = time.perf_counter()
    started_at = utc_now()
    try:
        if args.manage_stack:
            await asyncio.to_thread(controller.up, workers=2, build=args.build)
        controller.exec(
            "runtime-api",
            [
                "python",
                "-c",
                (
                    "from pathlib import Path; "
                    "Path('/workspace/stage15.txt').write_text('stage15 evidence\\n')"
                ),
            ],
        )
        async with RuntimeClient(
            controller.base_url,
            api_key="stage15-runtime-key",
            timeout=20,
        ) as client:
            await _wait_runtime_health(client, timeout=30)
            for scenario_id in selected:
                definition = by_id[scenario_id]
                scenario_started = time.perf_counter()
                try:
                    evidence = await SCENARIOS[scenario_id](
                        controller,
                        client,
                        float(definition["timeout_seconds"]),
                    )
                    outcome = "passed" if evidence.get("verified") else "failed"
                    error = None
                except Exception as exc:  # noqa: BLE001 - retain evidence and restore stack
                    evidence = {}
                    outcome = "error"
                    error = {"type": type(exc).__name__, "message": _safe_message(exc)}
                results.append(
                    {
                        "scenario_id": scenario_id,
                        "fault": definition["fault"],
                        "expected_outcome": definition["expected_outcome"],
                        "actual_outcome": (
                            definition["expected_outcome"] if outcome == "passed" else "failure"
                        ),
                        "outcome": outcome,
                        "recovery_ms": evidence.get("recovery_ms"),
                        "duration_seconds": round(time.perf_counter() - scenario_started, 3),
                        "evidence": evidence,
                        "error": error,
                    }
                )
                try:
                    await asyncio.to_thread(controller.up, workers=2)
                    await _wait_runtime_health(client, timeout=40)
                except Exception as exc:  # noqa: BLE001 - retain scenario evidence
                    results[-1]["outcome"] = "error"
                    results[-1]["recovery_error"] = {
                        "type": type(exc).__name__,
                        "message": _safe_message(exc),
                    }
                print(f"{scenario_id}: {results[-1]['outcome']}", flush=True)
            try:
                consistency = await _consistency_samples(client)
            except Exception as exc:  # noqa: BLE001 - final sample is reported, not fatal to cleanup
                consistency = {
                    "consistent": False,
                    "error": {"type": type(exc).__name__, "message": _safe_message(exc)},
                }
            try:
                capacity = (await client.health()).get("capacity")
                provider = await asyncio.to_thread(_provider_target_summary, controller)
            except Exception as exc:  # noqa: BLE001 - diagnostics remain bounded
                provider = {
                    "available": False,
                    "error": {"type": type(exc).__name__, "message": _safe_message(exc)},
                }
    finally:
        if args.manage_stack and not args.leave_running:
            await asyncio.to_thread(controller.cleanup)

    report = {
        "schema_version": 1,
        "kind": "axiom-deployment-fault-injection",
        "started_at": started_at,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "environment": environment_metadata(profile="faults", workers=2),
        "summary": {
            "requested": len(selected),
            "passed": sum(item["outcome"] == "passed" for item in results),
            "failed": sum(item["outcome"] != "passed" for item in results),
        },
        "scenarios": results,
        "post_fault_consistency": consistency,
        "post_fault_capacity": capacity,
        "post_fault_provider": provider,
        "interpretation": (
            "Machine-specific evidence only; deterministic crash-window semantics remain "
            "covered by benchmarks/recovery."
        ),
    }
    write_report(report, args.output, markdown_report(report))
    return 0 if report["summary"]["failed"] == 0 and consistency["consistent"] else 1


def _safe_message(exc: Exception) -> str:
    message = str(exc)
    for marker in (str(Path.cwd()), str(Path.home())):
        message = message.replace(marker, "<local>")
    message = re.sub(r"(?i)\b[A-Z]:\\[^\r\n'\"]+", "<local>", message)
    message = re.sub(r"/home/[^\s'\"]+", "<local>", message)
    return message[:300]


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def markdown_report(report: dict[str, Any]) -> str:
    rows = [
        "| Scenario | Expected | Actual | Recovery ms | Result | Seconds |",
        "|---|---|---|---:|---:|---:|",
    ]
    for item in report["scenarios"]:
        rows.append(
            f"| {item['scenario_id']} | {item['expected_outcome']} | "
            f"{item['actual_outcome']} | {item['recovery_ms']} | "
            f"{item['outcome']} | {item['duration_seconds']} |"
        )
    return "\n".join(
        [
            "# Deployment failure injection",
            "",
            "> Machine-specific evidence. Deterministic crash-window proofs remain in "
            "`benchmarks/recovery/`.",
            "",
            *rows,
            "",
            "Post-fault Artifact/Provenance consistency: "
            f"{report['post_fault_consistency']['consistent']}",
            "",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded real-Compose Axiom failure injection")
    parser.add_argument("--scenarios", nargs="*", choices=sorted(SCENARIOS))
    parser.add_argument("--project", default="axiom-stage15-faults")
    parser.add_argument("--web-port", type=int, default=18081)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT / "faults.json")
    parser.add_argument("--manage-stack", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--leave-running", action="store_true")
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(async_main(parse_args())))


if __name__ == "__main__":
    main()
