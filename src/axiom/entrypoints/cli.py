from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from axiom import __version__
from axiom.agent import QueryEngine
from axiom.bootstrap import build_tool_registry
from axiom.config import get_config_paths, load_config
from axiom.entrypoints.repl import start_repl
from axiom.evaluation import (
    BadCaseCollector,
    BadCaseError,
    BadCaseStore,
    DurableEvaluationExecutor,
    EvaluationComparison,
    EvaluationRunner,
    EvaluationSuiteResult,
    RegressionThresholds,
    ReviewStatus,
    compare_results,
    evaluate_regression_gate,
    load_dataset,
    load_result,
    promote_badcase,
    save_result,
)
from axiom.llm import create_llm_client
from axiom.mcp import load_mcp_server_specs, serve_http, serve_stdio, write_chrome_devtools_config
from axiom.rl import RewardConfig, RewardPipeline, RLRolloutRunner, RolloutDataset
from axiom.runtime import (
    ObservabilityService,
    RuntimeApiServer,
    SQLiteCheckpointStore,
    SQLiteObservabilityStore,
)
from axiom.runtime.api import runtime_api_key
from axiom.runtime.observability import RunMetrics, Span, SpanType, TraceBundle

app = typer.Typer(
    name="axiom",
    help="Axiom Agent Runtime - Terminal AI Agent in Python",
    invoke_without_command=True,
    no_args_is_help=False,
)
mcp_app = typer.Typer(help="MCP server management")
runs_app = typer.Typer(help="Inspect persisted Runtime runs")
eval_app = typer.Typer(help="Run and compare Agent evaluation datasets")
rl_app = typer.Typer(help="Collect RL trajectories from the durable Runtime")
badcase_app = typer.Typer(help="Collect, review, and promote evaluation badcases")
app.add_typer(mcp_app, name="mcp")
app.add_typer(runs_app, name="runs")
app.add_typer(eval_app, name="eval")
app.add_typer(rl_app, name="rl")
eval_app.add_typer(badcase_app, name="badcase")
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"axiom {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option("-p", "--prompt", help="Print mode: single prompt, non-interactive"),
    ] = None,
    model: Annotated[str | None, typer.Option("-m", "--model", help="Override model name")] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Override LLM provider"),
    ] = None,
    plain: Annotated[bool, typer.Option("--plain", help="Use plain text rendering")] = False,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version"),
    ] = False,
) -> None:
    _ = version
    if ctx.invoked_subcommand is not None:
        return
    root = (cwd or Path.cwd()).resolve()
    overrides: dict = {}
    if provider or model or plain:
        overrides = {
            "llm": {"provider": provider, "model": model},
            "render_mode": "plain" if plain else None,
        }
    config = load_config(project_root=root, overrides=overrides)
    if plain:
        config.render_mode = "plain"
    if prompt is not None:
        asyncio.run(_run_prompt(prompt, str(root), config))
    else:
        asyncio.run(start_repl(str(root), config))


@app.command("doctor")
def doctor(
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    root = (cwd or Path.cwd()).resolve()
    config = load_config(project_root=root)
    checks = {
        "python": sys.version.split()[0],
        "uv": shutil.which("uv") or "missing",
        "node": _version_of("node"),
        "npx": shutil.which("npx") or "missing",
        "rg": shutil.which("rg") or "missing",
        "api_key": "configured" if config.llm.api_key else "missing",
        "provider": config.llm.provider,
        "model": config.llm.model,
        "cwd": str(root),
        "config_paths": [str(path) for path in get_config_paths(root)],
    }
    console.print_json(json.dumps(checks, ensure_ascii=False))


@app.command("serve")
def runtime_serve(
    http: Annotated[bool, typer.Option("--http", help="Serve Runtime API over HTTP")] = True,
    port: Annotated[int, typer.Option("--port", help="HTTP port")] = 8080,
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Runtime API key. Defaults to AXIOM_RUNTIME_API_KEY."),
    ] = None,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Runtime data directory"),
    ] = None,
) -> None:
    _ = http
    root = (cwd or Path.cwd()).resolve()
    config = load_config(project_root=root)
    try:
        key = runtime_api_key(api_key)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    RuntimeApiServer(
        cwd=str(root),
        config=config,
        api_key=key,
        port=port,
        data_dir=data_dir,
    ).serve_forever()


@runs_app.command("show")
def runs_show(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Runtime data directory"),
    ] = None,
) -> None:
    service = ObservabilityService(SQLiteObservabilityStore(_runtime_db(data_dir)))
    metrics = asyncio.run(service.metrics(run_id))
    if metrics is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(_format_run_metrics(metrics))


@runs_app.command("trace")
def runs_trace(
    run_id: Annotated[str, typer.Argument(help="Run ID")],
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Runtime data directory"),
    ] = None,
) -> None:
    service = ObservabilityService(SQLiteObservabilityStore(_runtime_db(data_dir)))
    bundle = asyncio.run(service.trace(run_id))
    if bundle is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(_format_run_trace(bundle))


@eval_app.command("run")
def eval_run(
    dataset: Annotated[Path, typer.Argument(help="Evaluation dataset JSON file")],
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Agent working directory")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Runtime data directory"),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write structured result JSON"),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show every scorer result"),
    ] = False,
    trials: Annotated[
        int,
        typer.Option("--trials", min=1, help="Independent executions per case"),
    ] = 1,
) -> None:
    root = (cwd or Path.cwd()).resolve()
    try:
        kwargs = {"cwd": root, "data_dir": data_dir}
        if trials != 1:
            kwargs["trials"] = trials
        result = asyncio.run(_execute_evaluation_dataset(dataset, **kwargs))
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        typer.echo(f"Evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(_format_evaluation_suite(result, verbose=verbose))
    if output is not None:
        target = save_result(result, output)
        typer.echo(f"Result: {target}")


@rl_app.command("export")
def rl_export(
    dataset: Annotated[Path, typer.Argument(help="Evaluation dataset JSON file")],
    output: Annotated[Path, typer.Option("--output", help="Trajectory JSONL output")],
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Agent working directory")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Runtime data directory"),
    ] = None,
    trials: Annotated[
        int,
        typer.Option("--trials", min=1, help="Independent executions per case"),
    ] = 1,
    split: Annotated[str, typer.Option(help="Dataset split provenance")] = "train",
    reward_config: Annotated[
        Path | None,
        typer.Option("--reward-config", help="Optional RewardConfig JSON object"),
    ] = None,
) -> None:
    root = (cwd or Path.cwd()).resolve()
    try:
        reward = RewardConfig()
        if reward_config is not None:
            raw_reward = json.loads(reward_config.read_text(encoding="utf-8"))
            if not isinstance(raw_reward, dict):
                raise ValueError("reward configuration must be a JSON object")
            reward = RewardConfig(**raw_reward)
        rollout = asyncio.run(
            _execute_rl_dataset(
                dataset,
                cwd=root,
                data_dir=data_dir,
                trials=trials,
                split=split,
                reward_pipeline=RewardPipeline(reward),
            )
        )
        rollout.export_jsonl(output)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        typer.echo(f"RL rollout export failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(rollout.summary(), ensure_ascii=False, indent=2))
    typer.echo(f"Trajectories: {output.resolve()}")


@eval_app.command("compare")
def eval_compare(
    old: Annotated[Path, typer.Argument(help="Baseline evaluation result JSON")],
    new: Annotated[Path, typer.Argument(help="New evaluation result JSON")],
    token_warning_percent: Annotated[
        float,
        typer.Option(help="Warn when average tokens increase by this percentage"),
    ] = 20.0,
    latency_warning_percent: Annotated[
        float,
        typer.Option(help="Warn when average latency increases by this percentage"),
    ] = 30.0,
    step_warning_delta: Annotated[
        float,
        typer.Option(help="Warn when average steps increase by this amount"),
    ] = 2.0,
    cost_warning_percent: Annotated[
        float,
        typer.Option(help="Warn when Cost per Success increases by this percentage"),
    ] = 20.0,
) -> None:
    try:
        comparison = compare_results(
            load_result(old),
            load_result(new),
            token_warning_percent=token_warning_percent,
            latency_warning_percent=latency_warning_percent,
            step_warning_delta=step_warning_delta,
            cost_warning_percent=cost_warning_percent,
        )
    except ValueError as exc:
        typer.echo(f"Comparison failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(_format_evaluation_comparison(comparison))


@eval_app.command("gate")
def eval_gate(
    baseline: Annotated[Path, typer.Argument(help="Baseline evaluation result JSON")],
    candidate: Annotated[Path, typer.Argument(help="Candidate evaluation result JSON")],
    max_success_rate_drop: Annotated[
        float, typer.Option(help="Maximum per-case trial success-rate drop")
    ] = 0.10,
    max_token_increase_ratio: Annotated[
        float | None, typer.Option(help="Hard maximum fractional token increase")
    ] = None,
    max_latency_increase_ratio: Annotated[
        float | None, typer.Option(help="Hard maximum fractional latency increase")
    ] = None,
    max_step_increase: Annotated[
        float | None, typer.Option(help="Hard maximum average step increase")
    ] = None,
    max_cost_per_success_increase_ratio: Annotated[
        float | None,
        typer.Option(help="Hard maximum fractional Cost per Success increase"),
    ] = None,
) -> None:
    try:
        result = evaluate_regression_gate(
            load_result(baseline),
            load_result(candidate),
            thresholds=RegressionThresholds(
                max_success_rate_drop=max_success_rate_drop,
                max_token_increase_ratio=max_token_increase_ratio,
                max_latency_increase_ratio=max_latency_increase_ratio,
                max_step_increase=max_step_increase,
                max_cost_per_success_increase_ratio=(max_cost_per_success_increase_ratio),
            ),
        )
    except ValueError as exc:
        typer.echo(f"Regression gate failed to run: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    if not result.passed:
        raise typer.Exit(1)


@badcase_app.command("collect")
def badcase_collect(
    result: Annotated[Path | None, typer.Option("--result", help="Evaluation result JSON")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Terminal Runtime run ID")] = None,
    store: Annotated[Path | None, typer.Option("--store", help="Badcase store JSON")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    if (result is None) == (run_id is None):
        raise typer.BadParameter("provide exactly one of --result or --run-id")
    badcases = BadCaseStore(store or _badcase_store_path(data_dir))
    try:
        if result is not None:
            records = BadCaseCollector(badcases).collect_suite(load_result(result))
        else:
            database = _runtime_db(data_dir)
            collector = BadCaseCollector(
                badcases,
                runtime_store=SQLiteCheckpointStore(database),
                observability_store=SQLiteObservabilityStore(database),
            )
            records = [asyncio.run(collector.collect_run(str(run_id)))]
    except (ValueError, BadCaseError) as exc:
        typer.echo(f"Badcase collection failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))


@badcase_app.command("list")
def badcase_list(
    store: Annotated[Path | None, typer.Option("--store", help="Badcase store JSON")] = None,
    status: Annotated[str | None, typer.Option(help="Filter by review status")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    try:
        selected = ReviewStatus(status.upper()) if status else None
        records = BadCaseStore(store or _badcase_store_path(data_dir)).list(status=selected)
    except (ValueError, BadCaseError) as exc:
        typer.echo(f"Badcase list failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps([record.to_dict() for record in records], ensure_ascii=False, indent=2))


@badcase_app.command("show")
def badcase_show(
    badcase_id: Annotated[str, typer.Argument(help="Badcase ID")],
    store: Annotated[Path | None, typer.Option("--store", help="Badcase store JSON")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    record = BadCaseStore(store or _badcase_store_path(data_dir)).get(badcase_id)
    if record is None:
        typer.echo(f"Badcase not found: {badcase_id}", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))


def _review_badcase(
    badcase_id: str,
    status: ReviewStatus,
    *,
    note: str,
    store: Path | None,
    data_dir: Path | None,
) -> None:
    try:
        record = BadCaseStore(store or _badcase_store_path(data_dir)).update_review_status(
            badcase_id, status, note=note
        )
    except BadCaseError as exc:
        typer.echo(f"Badcase review failed [{exc.code}]: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(record.to_dict(), ensure_ascii=False, indent=2))


@badcase_app.command("approve")
def badcase_approve(
    badcase_id: Annotated[str, typer.Argument(help="Badcase ID")],
    note: Annotated[str, typer.Option(help="Human review note")] = "",
    store: Annotated[Path | None, typer.Option("--store")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    _review_badcase(badcase_id, ReviewStatus.APPROVED, note=note, store=store, data_dir=data_dir)


@badcase_app.command("ignore")
def badcase_ignore(
    badcase_id: Annotated[str, typer.Argument(help="Badcase ID")],
    note: Annotated[str, typer.Option(help="Human review note")] = "",
    store: Annotated[Path | None, typer.Option("--store")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    _review_badcase(badcase_id, ReviewStatus.IGNORED, note=note, store=store, data_dir=data_dir)


@badcase_app.command("promote")
def badcase_promote(
    badcase_id: Annotated[str, typer.Argument(help="Badcase ID")],
    dataset: Annotated[Path, typer.Option("--dataset", help="Regression dataset JSON")],
    store: Annotated[Path | None, typer.Option("--store")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    expected_json: Annotated[
        str | None, typer.Option("--expected-json", help="Expected JSON object override")
    ] = None,
    scorers_json: Annotated[
        str | None, typer.Option("--scorers-json", help="Scorer JSON list override")
    ] = None,
) -> None:
    try:
        expected = json.loads(expected_json) if expected_json else None
        scorers = json.loads(scorers_json) if scorers_json else None
        if expected is not None and not isinstance(expected, dict):
            raise ValueError("--expected-json must be an object")
        if scorers is not None and not isinstance(scorers, list):
            raise ValueError("--scorers-json must be a list")
        record, case = promote_badcase(
            BadCaseStore(store or _badcase_store_path(data_dir)),
            badcase_id,
            dataset,
            expected_override=expected,
            scorers_override=scorers,
        )
    except (ValueError, BadCaseError) as exc:
        code = f" [{exc.code}]" if isinstance(exc, BadCaseError) else ""
        typer.echo(f"Badcase promotion failed{code}: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps({"badcase": record.to_dict(), "case": case.to_dict()}, indent=2))


@mcp_app.command("serve")
def mcp_serve(
    transport: Annotated[
        str,
        typer.Option("--transport", help="Transport type: stdio or http"),
    ] = "stdio",
    port: Annotated[int, typer.Option("--port", help="HTTP port")] = 3000,
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    root = str((cwd or Path.cwd()).resolve())
    if transport == "http":
        serve_http(port=port, cwd=root)
    elif transport == "stdio":
        asyncio.run(serve_stdio(cwd=root))
    else:
        raise typer.BadParameter("transport must be stdio or http")


@mcp_app.command("init-chrome")
def mcp_init_chrome(
    scope: Annotated[
        str,
        typer.Option("--scope", help="Config scope: user or project"),
    ] = "project",
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
    browser_url: Annotated[
        str | None,
        typer.Option("--browser-url", help="Connect to an existing Chrome remote debugging URL"),
    ] = None,
    headless: Annotated[bool, typer.Option("--headless", help="Start Chrome headless")] = False,
    slim: Annotated[bool, typer.Option("--slim", help="Use Chrome DevTools slim mode")] = False,
) -> None:
    if scope not in {"user", "project"}:
        raise typer.BadParameter("scope must be user or project")
    root = None if scope == "user" else (cwd or Path.cwd()).resolve()
    path = write_chrome_devtools_config(
        scope_root=root,
        browser_url=browser_url,
        headless=headless,
        slim=slim,
    )
    typer.echo(f"Wrote Chrome DevTools MCP config to {path}")


@mcp_app.command("list")
def mcp_list(
    cwd: Annotated[Path | None, typer.Option("--cwd", help="Working directory")] = None,
) -> None:
    root = (cwd or Path.cwd()).resolve()
    specs = load_mcp_server_specs(root)
    if not specs:
        typer.echo("No MCP servers configured.")
        return
    for spec in specs.values():
        target = spec.url or f"{spec.command} {' '.join(spec.args)}".strip()
        typer.echo(f"{spec.name}\t{spec.type}\t{target}")


async def _run_prompt(prompt: str, cwd: str, config) -> None:
    config.render_mode = "plain"
    if not config.llm.api_key:
        typer.echo(
            "Fatal error: AXIOM_API_KEY is not configured. Set it in env, "
            "~/.axiom/config.json, or project .axiom/config.json.",
            err=True,
        )
        raise typer.Exit(1)
    registry, manager = await build_tool_registry(config=config, cwd=cwd)
    if manager and manager.last_errors:
        for name, error in manager.last_errors.items():
            typer.echo(f"MCP server {name} failed to load: {error}", err=True)
    engine = QueryEngine(
        llm_client=create_llm_client(config.llm),
        tool_registry=registry,
        config=config,
        cwd=cwd,
    )
    try:
        result = await engine.ask_complete_async(prompt)
    except Exception as exc:  # noqa: BLE001 - CLI should report model/config errors cleanly
        typer.echo(f"Fatal error: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(result.text)


async def _execute_evaluation_dataset(
    dataset_path: Path,
    *,
    cwd: Path,
    data_dir: Path | None,
    trials: int = 1,
) -> EvaluationSuiteResult:
    dataset = load_dataset(dataset_path)
    config = load_config(project_root=cwd)
    config.render_mode = "plain"
    if not config.llm.api_key:
        raise ValueError("AXIOM_API_KEY is not configured")
    registry, manager = await build_tool_registry(config=config, cwd=str(cwd))
    if manager and manager.last_errors:
        for name, error in manager.last_errors.items():
            typer.echo(f"MCP server {name} failed to load: {error}", err=True)
    llm_client = create_llm_client(config.llm)

    def engine_factory(_case):
        return QueryEngine(
            llm_client=llm_client,
            tool_registry=registry,
            config=config,
            cwd=str(cwd),
        )

    database = _runtime_db(data_dir)
    executor = DurableEvaluationExecutor(
        engine_factory=engine_factory,
        checkpoint_store=SQLiteCheckpointStore(database),
        observability_store=SQLiteObservabilityStore(database),
    )
    return await EvaluationRunner(executor).run(dataset, trials=trials)


async def _execute_rl_dataset(
    dataset_path: Path,
    *,
    cwd: Path,
    data_dir: Path | None,
    trials: int,
    split: str,
    reward_pipeline: RewardPipeline,
) -> RolloutDataset:
    dataset = load_dataset(dataset_path)
    config = load_config(project_root=cwd)
    config.render_mode = "plain"
    if not config.llm.api_key:
        raise ValueError("AXIOM_API_KEY is not configured")
    registry, manager = await build_tool_registry(config=config, cwd=str(cwd))
    if manager and manager.last_errors:
        for name, error in manager.last_errors.items():
            typer.echo(f"MCP server {name} failed to load: {error}", err=True)
    llm_client = create_llm_client(config.llm)

    def engine_factory(_case):
        return QueryEngine(
            llm_client=llm_client,
            tool_registry=registry,
            config=config,
            cwd=str(cwd),
        )

    database = _runtime_db(data_dir)
    runtime_store = SQLiteCheckpointStore(database)
    observability_store = SQLiteObservabilityStore(database)
    evaluator = EvaluationRunner(
        DurableEvaluationExecutor(
            engine_factory=engine_factory,
            checkpoint_store=runtime_store,
            observability_store=observability_store,
        )
    )
    return await RLRolloutRunner(
        evaluator,
        runtime_store=runtime_store,
        observability_store=observability_store,
        reward_pipeline=reward_pipeline,
    ).collect(dataset, trials=trials, split=split)


def _version_of(command: str) -> str:
    if not shutil.which(command):
        return "missing"
    try:
        result = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:  # noqa: BLE001
        return "unknown"
    return (result.stdout or result.stderr).strip() or "unknown"


def _runtime_db(data_dir: Path | None) -> Path:
    root = data_dir.expanduser() if data_dir else Path.home() / ".axiom" / "runtime"
    return root / "runtime.db"


def _badcase_store_path(data_dir: Path | None) -> Path:
    root = data_dir.expanduser() if data_dir else Path.home() / ".axiom" / "runtime"
    return root / "evaluation-badcases.json"


def _format_run_metrics(metrics: RunMetrics) -> str:
    success_rate = (
        f"{metrics.tool_success_rate * 100:.1f}%"
        if metrics.tool_success_rate is not None
        else "n/a"
    )
    return "\n".join(
        [
            f"Run: {metrics.run_id}",
            f"Status: {metrics.status}",
            f"Latency: {_format_duration(metrics.duration_ms)}",
            f"Steps: {metrics.step_count}",
            f"LLM calls: {metrics.llm_calls}",
            f"Tool calls: {metrics.tool_calls}",
            f"Tokens: {metrics.total_tokens} "
            f"(input {metrics.prompt_tokens}, output {metrics.completion_tokens})",
            f"Tool success: {success_rate}",
            f"Checkpoints: {metrics.checkpoint_count}",
            f"Interrupts: {metrics.interrupt_count}",
            f"Resumes: {metrics.resume_count}",
            f"Retries: {metrics.retry_count}",
            f"Cost: ${metrics.cost_usd}" if metrics.cost_known else "Cost: unknown",
            f"Budget limits: {json.dumps(metrics.budget_policy, sort_keys=True)}",
            f"Budget remaining: {json.dumps(metrics.budget_remaining, sort_keys=True)}",
            "Aggregate budget remaining: "
            f"{json.dumps(metrics.aggregate_budget_remaining, sort_keys=True)}",
            f"Budget utilization: {json.dumps(metrics.budget_utilization, sort_keys=True)}",
        ]
    )


def _format_evaluation_suite(result: EvaluationSuiteResult, *, verbose: bool) -> str:
    lines = [
        f"Dataset: {result.dataset} ({result.dataset_version})",
        "",
        f"Cases: {result.cases_total}",
        f"Passed: {result.cases_passed}",
        f"Failed: {result.cases_failed}",
        f"Pass Rate: {result.pass_rate * 100:.1f}%",
        f"Trials: {result.trial_count} ({result.trials_per_case} per case)",
        f"Trial Success Rate: {result.trial_success_rate * 100:.1f}%",
        "",
        f"Avg Steps: {result.avg_steps:.2f}",
        f"Avg Tokens: {result.avg_tokens:.1f}",
        (
            f"Total Cost: ${result.total_cost_usd} "
            f"({result.cost_known_trial_count}/{result.trial_count} trials priced)"
            if result.total_cost_usd is not None
            else "Total Cost: unknown"
        ),
        (
            f"Cost per Success: ${result.cost_per_success}"
            if result.cost_per_success is not None
            else "Cost per Success: unknown/incomplete"
        ),
        f"Avg Latency: {_format_duration(result.avg_latency_ms)}",
        "",
    ]
    for case in result.results:
        trial = f" trial {case.trial_index}" if result.trials_per_case > 1 else ""
        lines.append(f"{'✓' if case.passed else '✗'} {case.case_id}{trial}")
        if verbose:
            for score in case.scores:
                required = "" if score.required else " (optional)"
                lines.append(
                    f"    {'PASS' if score.passed else 'FAIL'} {score.scorer}{required}: "
                    f"{score.reason}"
                )
            if case.error:
                lines.append(f"    ERROR: {case.error}")
    return "\n".join(lines)


def _format_evaluation_comparison(comparison: EvaluationComparison) -> str:
    changes = {change.metric: change for change in comparison.metric_changes}
    lines = [
        f"Comparison: {comparison.old_dataset} → {comparison.new_dataset}",
        "",
        _format_change(changes["pass_rate"], percent_value=True),
        _format_change(changes["trial_success_rate"], percent_value=True),
        _format_change(changes["avg_tokens"]),
        _format_change(changes["avg_latency_ms"], duration=True),
        _format_change(changes["avg_steps"]),
        "",
        "Regressions:",
    ]
    lines.extend(f"  {case_id} PASS → FAIL" for case_id in comparison.regressions)
    if not comparison.regressions:
        lines.append("  (none)")
    lines.append("Improvements:")
    lines.extend(f"  {case_id} FAIL → PASS" for case_id in comparison.improvements)
    if not comparison.improvements:
        lines.append("  (none)")
    if comparison.stochastic_regressions:
        lines.append("Stochastic quality regressions:")
        lines.extend(f"  {case_id}" for case_id in comparison.stochastic_regressions)
    if comparison.performance_warnings:
        lines.append("Performance warnings:")
        lines.extend(f"  {warning.message}" for warning in comparison.performance_warnings)
    return "\n".join(lines)


def _format_change(change, *, percent_value: bool = False, duration: bool = False) -> str:
    if percent_value:
        old = f"{change.old * 100:.1f}%"
        new = f"{change.new * 100:.1f}%"
        label = "Trial Success Rate" if change.metric == "trial_success_rate" else "Pass Rate"
    elif duration:
        old = _format_duration(change.old)
        new = _format_duration(change.new)
        label = "Latency"
    else:
        old = f"{change.old:.2f}"
        new = f"{change.new:.2f}"
        label = "Tokens" if change.metric == "avg_tokens" else "Steps"
    return f"{label}: {old} → {new}"


def _format_run_trace(bundle: TraceBundle) -> str:
    children: dict[str | None, list[Span]] = {}
    for span in bundle.spans:
        children.setdefault(span.parent_span_id, []).append(span)
    for values in children.values():
        values.sort(key=lambda span: (span.started_at, span.span_id))

    root = next(
        (span for span in bundle.spans if span.span_type == SpanType.AGENT and span.name == "run"),
        None,
    )
    lines = [
        f"Run {bundle.trace.run_id} {_format_duration(bundle.trace.total_latency_ms)} "
        f"[{bundle.trace.status}]"
    ]
    parent_id = root.span_id if root is not None else None
    _append_trace_children(lines, children, parent_id, prefix="")
    return "\n".join(lines)


def _append_trace_children(
    lines: list[str],
    children: dict[str | None, list[Span]],
    parent_id: str | None,
    *,
    prefix: str,
) -> None:
    values = children.get(parent_id, [])
    for index, span in enumerate(values):
        last = index == len(values) - 1
        connector = "└──" if last else "├──"
        lines.append(
            f"{prefix}{connector} {_span_label(span):<24} "
            f"{_format_duration(span.latency_ms):>9} [{span.status.value}]"
        )
        child_prefix = prefix + ("    " if last else "│   ")
        _append_trace_children(lines, children, span.span_id, prefix=child_prefix)


def _span_label(span: Span) -> str:
    if span.span_type == SpanType.LLM:
        model = str(span.attributes.get("model") or "unknown")
        return f"LLM {model}"
    if span.span_type == SpanType.TOOL:
        return f"Tool {span.attributes.get('tool_name') or span.name}"
    if span.span_type == SpanType.CHECKPOINT:
        return "Checkpoint"
    if span.span_type == SpanType.INTERRUPT:
        return f"Interrupt {span.attributes.get('kind') or ''}".rstrip()
    if span.span_type == SpanType.POLICY:
        decision = span.attributes.get("decision") or "unknown"
        return f"Policy {span.attributes.get('tool_name') or span.name} {decision}"
    if span.name == "resume":
        return "Resume"
    return "Agent step" if span.name == "agent.step" else span.name


def _format_duration(value: float | None) -> str:
    if value is None:
        return "running"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.1f}ms"
