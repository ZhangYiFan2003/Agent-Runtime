from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from benchmarks.deployment.client import RuntimeClient, RuntimeHttpError, event_timings
from benchmarks.deployment.common import (
    DEFAULT_OUTPUT,
    ComposeController,
    environment_metadata,
    latency_summary,
    load_profile,
    profile_dict,
    profile_environment,
    utc_now,
    write_report,
)

PROFILE_FILE = Path(__file__).with_name("profiles.json")


async def run_profile(
    controller: ComposeController,
    profile_name: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    profile = load_profile(PROFILE_FILE, profile_name, force=force)
    started_at = utc_now()
    started = time.perf_counter()
    async with RuntimeClient(
        controller.base_url,
        api_key="stage15-runtime-key",
        timeout=max(20.0, profile.run_timeout_seconds),
    ) as client:
        health = await client.health()
        warmups: list[dict[str, Any]] = []
        for index in range(profile.warmups):
            warmups.append(
                await _run_one(
                    client,
                    index=-(index + 1),
                    timeout=profile.run_timeout_seconds,
                    prompt="STAGE15:LOAD warmup deterministic response",
                )
            )

        semaphore = asyncio.Semaphore(profile.concurrency)

        async def limited(index: int) -> dict[str, Any]:
            async with semaphore:
                return await _run_one(
                    client,
                    index=index,
                    timeout=profile.run_timeout_seconds,
                    prompt=f"STAGE15:LOAD run {index} deterministic response",
                )

        load_started = time.perf_counter()
        runs = await asyncio.gather(*(limited(index) for index in range(profile.runs)))
        load_seconds = time.perf_counter() - load_started
        consistency = await _control_plane_consistency(client, runs)
        drained = await _wait_for_capacity_drain(client, timeout=15.0)
        provider = await asyncio.to_thread(_provider_summary, controller)

    counts = Counter(str(item["outcome"]) for item in runs)
    accepted = [item for item in runs if item.get("accepted")]
    server_timings = {
        key: latency_summary(
            float(item[key]) for item in accepted if isinstance(item.get(key), (int, float))
        )
        for key in ("queue_wait_ms", "run_ms", "server_end_to_end_ms")
    }
    report = {
        "schema_version": 1,
        "kind": "axiom-deployment-load",
        "started_at": started_at,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "environment": environment_metadata(profile=profile.name, workers=profile.workers),
        "profile": profile_dict(profile),
        "health": _health_summary(health),
        "summary": {
            "requested": profile.runs,
            "accepted": len(accepted),
            "rejected": sum(item["outcome"] == "rejected" for item in runs),
            "completed": counts["completed"],
            "failed": counts["failed"],
            "timed_out": counts["timed_out"],
            "transport_errors": counts["transport_error"],
            "http_errors": counts["http_error"],
            "malformed_responses": counts["malformed_response"],
            "throughput_runs_per_second": round(counts["completed"] / load_seconds, 3)
            if load_seconds > 0
            else None,
            "load_window_seconds": round(load_seconds, 3),
            "admission": latency_summary(
                float(item["admission_ms"])
                for item in runs
                if isinstance(item.get("admission_ms"), (int, float))
            ),
            "client_end_to_end": latency_summary(
                float(item["client_end_to_end_ms"])
                for item in accepted
                if isinstance(item.get("client_end_to_end_ms"), (int, float))
            ),
            **server_timings,
            "rejection_codes": dict(
                Counter(
                    str(item.get("error_code") or item.get("http_status") or "unknown")
                    for item in runs
                    if item["outcome"] == "rejected"
                )
            ),
        },
        "control_plane": consistency,
        "capacity_drained": drained,
        "provider_gateway": provider,
        "warmups": [_compact_run(item) for item in warmups],
        "runs": runs,
        "interpretation": (
            "Machine-specific evidence only; values are not CI gates or universal "
            "service-level objectives."
        ),
    }
    summary = report["summary"]
    report["validation_passed"] = bool(
        consistency.get("verified")
        and drained
        and summary["accepted"] > 0
        and summary["completed"] > 0
        and summary["failed"] == 0
        and summary["timed_out"] == 0
        and summary["transport_errors"] == 0
        and summary["http_errors"] == 0
        and summary["malformed_responses"] == 0
        and (
            profile.name != "saturation"
            or {"QUEUE_CAPACITY_EXCEEDED", "RATE_LIMITED"} <= set(summary["rejection_codes"])
        )
    )
    return report


async def _run_one(
    client: RuntimeClient,
    *,
    index: int,
    timeout: float,
    prompt: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    thread_id: str | None = None
    run_id: str | None = None
    try:
        thread_id = await client.create_thread()
        admission_started = time.perf_counter()
        submitted = await client.submit_turn(thread_id, prompt)
        admission_ms = round((time.perf_counter() - admission_started) * 1000, 3)
        run_id = submitted.get("run_id") if isinstance(submitted.get("run_id"), str) else None
        if run_id is None:
            return {
                "index": index,
                "thread_id": thread_id,
                "run_id": None,
                "accepted": False,
                "outcome": "malformed_response",
                "admission_ms": admission_ms,
            }
        state = await client.wait_for_run(run_id, timeout=timeout)
        with contextlib.suppress(TimeoutError):
            await client.wait_for_event(
                thread_id,
                run_id,
                {"run.completed", "run.failed", "run.cancelled"},
                timeout=2,
            )
        events = await client.events(thread_id, run_id=run_id)
        timing = event_timings(events)
        status = str(state.get("status") or "UNKNOWN")
        return {
            "index": index,
            "thread_id": thread_id,
            "run_id": run_id,
            "accepted": True,
            "outcome": "completed" if status == "COMPLETED" else "failed",
            "status": status,
            "admission_ms": admission_ms,
            "client_end_to_end_ms": round((time.perf_counter() - started) * 1000, 3),
            "event_count": len(events),
            "event_ids_unique": len({event.get("event_id") for event in events}) == len(events),
            **timing,
        }
    except RuntimeHttpError as exc:
        return {
            "index": index,
            "thread_id": thread_id,
            "run_id": run_id,
            "accepted": False,
            "outcome": "rejected" if exc.status_code in {409, 429, 503} else "http_error",
            "http_status": exc.status_code,
            "error_code": exc.code,
            "message": exc.message[:200],
            "client_end_to_end_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except TimeoutError as exc:
        return {
            "index": index,
            "thread_id": thread_id,
            "run_id": run_id,
            "accepted": run_id is not None,
            "outcome": "timed_out",
            "message": str(exc)[:200],
            "client_end_to_end_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except httpx.TransportError as exc:
        return {
            "index": index,
            "thread_id": thread_id,
            "run_id": run_id,
            "accepted": run_id is not None,
            "outcome": "transport_error",
            "message": type(exc).__name__,
            "client_end_to_end_ms": round((time.perf_counter() - started) * 1000, 3),
        }


async def _control_plane_consistency(
    client: RuntimeClient, runs: list[dict[str, Any]]
) -> dict[str, Any]:
    sample = next((item for item in runs if item.get("accepted") and item.get("run_id")), None)
    if sample is None:
        return {"verified": False, "reason": "no accepted Run available"}
    run_id = str(sample["run_id"])
    thread_id = str(sample["thread_id"])
    run_view = await client.run(run_id)
    replay = await client.events(thread_id, run_id=run_id)
    last_id = max(
        (int(item["event_id"]) for item in replay if isinstance(item.get("event_id"), int)),
        default=0,
    )
    exclusive = await client.events(thread_id, run_id=run_id, after_id=last_id)
    listed = any(item.get("run_id") == run_id for item in await client.list_runs())
    return {
        "verified": True,
        "run_detail_round_trip": run_view.get("run_id") == run_id,
        "run_list_contains_sample": listed,
        "event_replay_count": len(replay),
        "event_ids_unique": len({item.get("event_id") for item in replay}) == len(replay),
        "exclusive_cursor_empty_at_tip": exclusive == [],
    }


async def _wait_for_capacity_drain(client: RuntimeClient, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            capacity = (await client.health()).get("capacity")
            if isinstance(capacity, dict):
                queued = capacity.get("queued_runs")
                active = capacity.get("active_runs")
                if queued == 0 and active == 0:
                    return True
        except (httpx.TransportError, RuntimeHttpError):
            pass
        await asyncio.sleep(0.25)
    return False


def _health_summary(health: dict[str, Any]) -> dict[str, Any]:
    distributed = health.get("distributed_runtime")
    return {
        "status": health.get("status"),
        "database": health.get("database"),
        "storage_backend": health.get("storage_backend"),
        "api_local_task_workers": health.get("workers"),
        "api_local_task_workers_note": (
            "Legacy API-local task workers; not the distributed Worker replica count."
        ),
        "capacity": health.get("capacity"),
        "distributed_runtime": distributed if isinstance(distributed, dict) else None,
    }


def _provider_summary(controller: ComposeController) -> dict[str, Any]:
    try:
        payload = controller.provider_health()
    except Exception as exc:  # noqa: BLE001 - diagnostics must not hide the load result
        return {"available": False, "error": type(exc).__name__}
    targets = payload.get("targets")
    summaries = []
    if isinstance(targets, list):
        for item in targets:
            if isinstance(item, dict):
                summaries.append(
                    {
                        key: item.get(key)
                        for key in (
                            "id",
                            "circuit_state",
                            "active",
                            "pending",
                            "attempts",
                            "failures",
                            "rate_limited",
                            "circuit_rejections",
                            "busy_rejections",
                            "admission_wait_ms",
                        )
                    }
                )
    return {"available": True, "targets": summaries}


def _compact_run(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item.get(key) for key in ("outcome", "status", "admission_ms", "client_end_to_end_ms")
    }


def markdown_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    return "\n".join(
        [
            f"# Deployment load: {report['profile']['name']}",
            "",
            "> Machine-specific evidence. Not a CI threshold or universal SLO.",
            "",
            f"- Requested: {summary['requested']}",
            f"- Accepted / rejected: {summary['accepted']} / {summary['rejected']}",
            "- Completed / failed / timed out: "
            f"{summary['completed']} / {summary['failed']} / {summary['timed_out']}",
            f"- Throughput: {summary['throughput_runs_per_second']} completed Runs/s",
            f"- Client p95: {summary['client_end_to_end']['p95_ms']} ms",
            f"- Queue p95: {summary['queue_wait_ms']['p95_ms']} ms",
            f"- Capacity drained: {report['capacity_drained']}",
            "",
        ]
    )


async def async_main(args: argparse.Namespace) -> int:
    controller = ComposeController(args.project, web_port=args.web_port)
    outputs: list[Path] = []
    all_valid = True
    try:
        for index, profile_name in enumerate(args.profiles):
            profile = load_profile(PROFILE_FILE, profile_name, force=args.force)
            if args.manage_stack:
                controller.up(
                    workers=profile.workers,
                    build=args.build and index == 0,
                    env_overrides=profile_environment(profile),
                )
            report = await run_profile(controller, profile_name, force=args.force)
            output = args.output / f"load-{profile_name}.json"
            write_report(report, output, markdown_report(report))
            outputs.append(output)
            all_valid = all_valid and report["validation_passed"]
            print(f"{profile_name}: {report['summary']}")
    finally:
        if args.manage_stack and not args.leave_running:
            controller.cleanup()
    print("reports:", ", ".join(str(path) for path in outputs))
    return 0 if all_valid else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded real-Compose Axiom load evidence")
    parser.add_argument(
        "--profiles",
        nargs="+",
        default=["smoke"],
        choices=[
            "smoke",
            "moderate-1-worker",
            "moderate-2-workers",
            "saturation",
            "soak-2-workers",
        ],
    )
    parser.add_argument("--project", default="axiom-stage15-load")
    parser.add_argument("--web-port", type=int, default=18080)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manage-stack", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--leave-running", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    raise SystemExit(asyncio.run(async_main(parse_args())))


if __name__ == "__main__":
    main()
