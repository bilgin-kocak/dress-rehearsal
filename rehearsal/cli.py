"""`rehearsal` command line (typer)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console

from rehearsal import __version__
from rehearsal.config import Config, load_config

app = typer.Typer(help="Dress Rehearsal: paper twin of the Binance Agent OS MCP + go-live harness.", no_args_is_help=True)
schema_app = typer.Typer(help="Mirror / validate the real server's tool schema.", no_args_is_help=True)
shadow_app = typer.Typer(help="Shadow mode: mirror live tool calls into the twin.", no_args_is_help=True)
app.add_typer(schema_app, name="schema")
app.add_typer(shadow_app, name="shadow")
console = Console(stderr=True)
err = console.print


def _cfg(config: Optional[str], **overrides: Any) -> Config:
    ov: dict[str, Any] = {}
    for k, v in overrides.items():
        if v is None:
            continue
        section, _, key = k.partition("__")
        ov.setdefault(section, {})[key] = v
    return load_config(config, ov)


def _setup_logging(verbose: bool, stdio: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # In stdio mode stdout is the MCP channel: everything goes to stderr.
    logging.basicConfig(level=level, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)


@app.callback()
def main() -> None:
    pass


@app.command()
def version() -> None:
    """Print the version."""
    print(__version__)


# ---------------------------------------------------------------------------- serve
@app.command()
def serve(
    transport: str = typer.Option(None, help="stdio | http"),
    port: int = typer.Option(None, help="Dashboard / MCP HTTP port"),
    mode: str = typer.Option(None, help="live | replay"),
    fixture: str = typer.Option(None, help="Replay fixture dir (fixtures/replay/<name>)"),
    speed: float = typer.Option(None, help="Replay speed (1.0 = realtime, 0 = as fast as possible)"),
    config: str = typer.Option(None, "--config", "-c", help="rehearsal.yaml path"),
    db: str = typer.Option(None, help="SQLite path (default .rehearsal/ledger.db)"),
    no_dashboard: bool = typer.Option(False, help="Do not start the dashboard thread (stdio only)"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Run the twin MCP server (+ dashboard on http://127.0.0.1:<port>/)."""
    cfg = _cfg(config, server__transport=transport, server__http_port=port, market__mode=mode,
               market__replay_fixture=fixture, market__replay_speed=speed, engine__db_path=db)
    _setup_logging(verbose, stdio=cfg.server.transport == "stdio")
    from rehearsal.runtime import Runtime

    rt = Runtime(cfg)
    rt.start()
    err(f"[bold]Dress Rehearsal twin[/] v{__version__}  mode={rt.mode_label()}  schema={rt.catalog.source}  "
        f"tools={len(rt.catalog.tools)}  db={rt.db_path}")
    if rt.catalog.source == "fallback":
        err("[red bold]SCHEMA NOT MIRRORED[/] — serving schemas/fallback_tools.json. Run `rehearsal schema dump` after "
            "authenticating binance-mcp-server in Claude Code.")
    try:
        if cfg.server.transport == "http":
            err(f"MCP streamable HTTP: http://{cfg.server.http_host}:{cfg.server.http_port}/mcp")
            err(f"Dashboard:           http://{cfg.server.http_host}:{cfg.server.http_port}/")
            rt.serve_http_blocking()
        else:
            if not no_dashboard:
                rt.start_dashboard_thread()
                err(f"Dashboard: http://{cfg.server.http_host}:{cfg.server.http_port}/   (MCP HTTP also on /mcp)")
            err("MCP stdio: ready")
            asyncio.run(rt.twin.run_stdio())
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()


# ---------------------------------------------------------------------------- reset / record
@app.command()
def reset(config: str = typer.Option(None, "--config", "-c"), db: str = typer.Option(None)) -> None:
    """Recreate the ledger from engine.initial_balances."""
    cfg = _cfg(config, engine__db_path=db)
    _setup_logging(False)
    from rehearsal.engine.engine import build_engine

    cfg.market.mode = "replay"  # no network needed
    eng = build_engine(cfg)
    eng.reset()
    err(f"ledger reset: {cfg.path(cfg.engine.db_path)}  balances={cfg.engine.initial_balances}")


@app.command()
def record(
    symbols: str = typer.Option("BTCUSDT,ETHUSDT", help="Comma-separated symbols"),
    minutes: float = typer.Option(10, help="How long to record"),
    out: str = typer.Option(None, help="Output dir (default fixtures/replay/<date>)"),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Record live public streams (depth / trades / mark price) to a replay fixture."""
    cfg = _cfg(config)
    _setup_logging(False)
    from rehearsal.market.binance_public import BinancePublic
    from rehearsal.market.recorder import record as _record, sort_events

    out_dir = Path(out) if out else (cfg.root / "fixtures" / "replay" / time.strftime("%Y-%m-%d_%H%M"))
    api = BinancePublic(cfg.market.spot_rest, cfg.market.usdm_rest)
    err(f"recording {symbols} for {minutes} min -> {out_dir}")
    meta = asyncio.run(_record([s.strip() for s in symbols.split(",") if s.strip()], minutes, out_dir, cfg.market.spot_ws,
                               cfg.market.usdm_ws, api, progress=lambda n: err(f"  {n} events")))
    n = sort_events(out_dir)
    err(f"done: {n} events, {json.dumps(meta)}")


# ---------------------------------------------------------------------------- schema
@schema_app.command("dump-instructions")
def schema_dump_instructions() -> None:
    """Print the Day-0 procedure for mirroring the real schema."""
    print((Path(__file__).resolve().parent.parent / "scripts" / "dump_tools.md").read_text())


@schema_app.command("dump")
def schema_dump(
    out: str = typer.Option("schemas/tools.json"),
    samples: bool = typer.Option(True, help="Also capture read-only samples into schemas/samples/"),
    token: str = typer.Option(None, help="Bearer token (default: reuse Claude Code's stored OAuth token)"),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Dump tools/list from the REAL server into schemas/tools.json (verbatim)."""
    cfg = _cfg(config)
    _setup_logging(False)
    from rehearsal.schema_tools import dump_schema

    rc = asyncio.run(dump_schema(cfg, cfg.path(out) or Path(out), samples=samples, token=token))
    raise typer.Exit(rc)


@schema_app.command("validate")
def schema_validate(
    live_url: str = typer.Option(None, help="Compare against the live server (needs OAuth token)"),
    token: str = typer.Option(None),
    config: str = typer.Option(None, "--config", "-c"),
) -> None:
    """Check that the twin serves exactly the mirrored schema (names + inputSchemas)."""
    cfg = _cfg(config)
    _setup_logging(False)
    from rehearsal.schema_tools import validate_schema

    rc = asyncio.run(validate_schema(cfg, live_url=live_url, token=token))
    raise typer.Exit(rc)


# ---------------------------------------------------------------------------- run / report / gate
@app.command()
def run(
    strategy: str = typer.Option(..., help="Strategy prompt file"),
    sessions: int = typer.Option(3),
    client: str = typer.Option(None, help="claude-code | codex | manual"),
    mode: str = typer.Option(None, help="live | replay"),
    fixture: str = typer.Option(None),
    speed: float = typer.Option(None, help="Replay speed"),
    session_minutes: int = typer.Option(None),
    max_turns: int = typer.Option(None),
    port: int = typer.Option(None),
    run_id: str = typer.Option(None),
    db: str = typer.Option(None, help="SQLite path (default .rehearsal/ledger.db)"),
    model: str = typer.Option(None, help="Model for the headless client (default: sonnet)"),
    use_api_key: bool = typer.Option(None, "--api-key/--no-api-key", help="--no-api-key strips ANTHROPIC_API_KEY so Claude Code uses your claude.ai login"),
    config: str = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """Drive the agent N sessions against the twin, then write the report and run the gate."""
    cfg = _cfg(config, market__mode=mode, market__replay_fixture=fixture, market__replay_speed=speed, runner__client=client,
               runner__session_minutes=session_minutes, runner__max_turns=max_turns, server__http_port=port, engine__db_path=db,
               runner__model=model, runner__use_api_key=use_api_key)
    _setup_logging(verbose)
    from rehearsal.rehearsal.runner import run_rehearsal

    report = run_rehearsal(cfg, Path(strategy), sessions, run_id=run_id)
    raise typer.Exit(0 if report["gate"]["passed"] else 1)


@app.command()
def coach(
    strategy: str = typer.Option(..., help="Strategy prompt file (v1)"),
    sessions: int = typer.Option(3, help="Sessions per rehearsal run"),
    dev_fixture: str = typer.Option("fixtures/replay/demo", help="Replay window used to find and fix failures"),
    holdout_fixture: str = typer.Option("fixtures/replay/holdout", help="Replay window used ONLY to verify the correction ('' to skip)"),
    max_iterations: int = typer.Option(2, help="Max corrections the agent may propose"),
    coach_model: str = typer.Option("opus", help="Model for the diagnose/correct step"),
    model: str = typer.Option(None, help="Model for the headless trading sessions (default: sonnet)"),
    session_minutes: int = typer.Option(None),
    max_turns: int = typer.Option(None),
    speed: float = typer.Option(4.0, help="Replay speed"),
    port: int = typer.Option(None, help="Base port; each run uses the next one"),
    run_id: str = typer.Option(None),
    use_api_key: bool = typer.Option(None, "--api-key/--no-api-key"),
    config: str = typer.Option(None, "--config", "-c"),
    verbose: bool = typer.Option(False, "-v"),
) -> None:
    """The Rehearsal Agent: rehearse → diagnose → propose a bounded fix → retest → verify on a held-out window."""
    cfg = _cfg(config, runner__session_minutes=session_minutes, runner__max_turns=max_turns, runner__model=model,
               runner__use_api_key=use_api_key, market__replay_speed=speed)
    _setup_logging(verbose)
    from rehearsal.rehearsal.coach import run_coach

    rc = run_coach(cfg, Path(strategy), sessions, dev_fixture, holdout_fixture or None, max_iterations=max_iterations,
                   run_id=run_id, coach_model=coach_model, base_port=port)
    raise typer.Exit(rc)


@app.command()
def doctor(config: str = typer.Option(None, "--config", "-c"), port: int = typer.Option(None)) -> None:
    """Check the environment: Python, Claude CLI, auth, schema mirror, fixtures, twin reachability."""
    cfg = _cfg(config, server__http_port=port)
    from rehearsal.doctor import run_doctor

    raise typer.Exit(run_doctor(cfg))


@app.command()
def report(run_id: str = typer.Option(None), fmt: str = typer.Option("md", "--format"),
           config: str = typer.Option(None, "--config", "-c")) -> None:
    """Print the report for a run (default: latest)."""
    cfg = _cfg(config)
    from rehearsal.rehearsal.report import load_report

    rep, path = load_report(cfg, run_id)
    if rep is None:
        err("no report found")
        raise typer.Exit(1)
    if fmt == "json":
        print(json.dumps(rep, indent=2))
    else:
        print((path.parent / "report.md").read_text())


@app.command()
def gate(run_id: str = typer.Option(None), config: str = typer.Option(None, "--config", "-c")) -> None:
    """Evaluate the go-live gate for a run (exit 0 = PASS, 1 = FAIL)."""
    cfg = _cfg(config)
    from rehearsal.rehearsal.gate import print_gate

    rc = print_gate(cfg, run_id)
    raise typer.Exit(rc)


# ---------------------------------------------------------------------------- shadow
@shadow_app.command("install")
def shadow_install(project: str = typer.Option(".", help="Project dir whose .claude/settings.json gets the hook"),
                   port: int = typer.Option(None), config: str = typer.Option(None, "--config", "-c")) -> None:
    """Install the Claude Code PostToolUse hook that mirrors live calls into the twin."""
    cfg = _cfg(config, server__http_port=port)
    from rehearsal.shadow.hook import install

    install(cfg, Path(project))


@shadow_app.command("uninstall")
def shadow_uninstall(project: str = typer.Option("."), config: str = typer.Option(None, "--config", "-c")) -> None:
    from rehearsal.shadow.hook import uninstall

    uninstall(_cfg(config), Path(project))


@shadow_app.command("status")
def shadow_status(project: str = typer.Option("."), config: str = typer.Option(None, "--config", "-c")) -> None:
    from rehearsal.shadow.hook import status

    status(_cfg(config), Path(project))


@shadow_app.command("hook", hidden=True)
def shadow_hook(port: int = typer.Option(8765)) -> None:
    """Hook entrypoint: reads the PostToolUse JSON on stdin and POSTs it to the twin."""
    from rehearsal.shadow.hook import forward_stdin

    forward_stdin(port)


# ---------------------------------------------------------------------------- demo
@app.command()
def demo(
    port: int = typer.Option(None),
    fixture: str = typer.Option("fixtures/replay/demo"),
    speed: float = typer.Option(10.0, help="Replay speed for the demo"),
    seed_only: bool = typer.Option(False, help="Only seed the FAIL run and exit"),
    config: str = typer.Option(None, "--config", "-c"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Replay-mode twin + dashboard, pre-seeded with the deliberately-bad strategy run (gate FAIL)."""
    cfg = _cfg(config, server__transport="http", server__http_port=port, market__mode="replay", market__replay_fixture=fixture,
               market__replay_speed=speed)
    _setup_logging(False)
    from rehearsal.demo import run_demo

    run_demo(cfg, seed_only=seed_only, open_browser=open_browser)


if __name__ == "__main__":
    app()
