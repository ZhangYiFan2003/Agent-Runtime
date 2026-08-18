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
    DurableEvaluationExecutor,
    EvaluationComparison,
    EvaluationRunner,
    EvaluationSuiteResult,
    compare_results,
    load_dataset,
    load_result,
    save_result,
)
from axiom.llm import create_llm_client
from axiom.mcp import load_mcp_server_specs, serve_http, serve_stdio, write_chrome_devtools_config
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
app.add_typer(mcp_app, name="mcp")
app.add_typer(runs_app, name="runs")
app.add_typer(eval_app, name="eval")
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
) -> None:
    root = (cwd or Path.cwd()).resolve()
    try:
        result = asyncio.run(
            _execute_evaluation_dataset(
                dataset,
                cwd=root,
                data_dir=data_dir,
            )
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        typer.echo(f"Evaluation failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(_format_evaluation_suite(result, verbose=verbose))
    if output is not None:
        target = save_result(result, output)
        typer.echo(f"Result: {target}")


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
) -> None:
    try:
        comparison = compare_results(
            load_result(old),
            load_result(new),
            token_warning_percent=token_warning_percent,
            latency_warning_percent=latency_warning_percent,
            step_warning_delta=step_warning_delta,
        )
    except ValueError as exc:
        typer.echo(f"Comparison failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(_format_evaluation_comparison(comparison))


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
    return await EvaluationRunner(executor).run(dataset)


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
        "",
        f"Avg Steps: {result.avg_steps:.2f}",
        f"Avg Tokens: {result.avg_tokens:.1f}",
        f"Avg Latency: {_format_duration(result.avg_latency_ms)}",
        "",
    ]
    for case in result.results:
        lines.append(f"{'✓' if case.passed else '✗'} {case.case_id}")
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
    if comparison.performance_warnings:
        lines.append("Performance warnings:")
        lines.extend(f"  {warning.message}" for warning in comparison.performance_warnings)
    return "\n".join(lines)


def _format_change(change, *, percent_value: bool = False, duration: bool = False) -> str:
    if percent_value:
        old = f"{change.old * 100:.1f}%"
        new = f"{change.new * 100:.1f}%"
        label = "Pass Rate"
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
    if span.name == "resume":
        return "Resume"
    return "Agent step" if span.name == "agent.step" else span.name


def _format_duration(value: float | None) -> str:
    if value is None:
        return "running"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.1f}ms"
