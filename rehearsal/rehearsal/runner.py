"""`rehearsal run`: N headless agent sessions against the twin -> report -> gate."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from rehearsal.config import Config
from rehearsal.rehearsal.adapters.base import Adapter
from rehearsal.rehearsal.gate import render_gate, write_pass_marker
from rehearsal.rehearsal.report import build_report, write_report
from rehearsal.runtime import Runtime

log = logging.getLogger("rehearsal.runner")


def get_adapter(cfg: Config) -> Adapter:
    name = cfg.runner.client
    if name == "claude-code":
        from rehearsal.rehearsal.adapters.claude_code import ClaudeCodeAdapter
        return ClaudeCodeAdapter(cfg)
    if name == "codex":
        from rehearsal.rehearsal.adapters.codex import CodexAdapter
        return CodexAdapter(cfg)
    from rehearsal.rehearsal.adapters.manual import ManualAdapter
    return ManualAdapter(cfg)


def _wait_health(url: str, timeout: float = 20.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(url + "/api/health", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def run_rehearsal(cfg: Config, strategy: Path, sessions: int, run_id: str | None = None, rt: Runtime | None = None) -> int:
    run_id = run_id or time.strftime("run_%Y%m%d_%H%M%S")
    prompt = strategy.read_text()
    if cfg.runner.client in ("claude-code", "codex") and cfg.runner.preamble:
        prompt = cfg.runner.preamble + prompt
    out_dir = cfg.path(cfg.runner.reports_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    own_runtime = rt is None
    if rt is None:
        rt = Runtime(cfg)
        rt.start()
        rt.start_dashboard_thread()
    base = f"http://{cfg.server.http_host}:{cfg.server.http_port}"
    if not _wait_health(base):
        log.error("twin did not come up on %s", base)
        return 2
    mcp_url = base + "/mcp"
    adapter = get_adapter(cfg)
    log.info("run %s: %d session(s), client=%s, mode=%s, twin=%s, dashboard=%s/", run_id, sessions, adapter.name, cfg.market.mode, mcp_url, base)
    if rt.catalog.source == "fallback":
        log.warning("SCHEMA NOT MIRRORED: rehearsing against schemas/fallback_tools.json")

    results: list[dict[str, Any]] = []
    for i in range(1, sessions + 1):
        sid = f"{run_id}_s{i}"
        if cfg.market.mode == "replay" and i > 1:
            rt.engine.feed.restart()
        rt.engine.start_session(sid, run_id=run_id, label=f"session {i}", reset=True,
                                meta={"client": adapter.name, "strategy": str(strategy)})
        log.info("=== session %d/%d (%s) ===", i, sessions, sid)
        res = adapter.run_session(prompt, mcp_url, sid, out_dir, cfg.runner.session_minutes, cfg.runner.max_turns)
        meta = res.meta() | {"client": adapter.name, "strategy": str(strategy)}
        rt.engine.ledger._exec("UPDATE sessions SET meta=? WHERE session_id=?", (json.dumps(meta), sid))
        rt.engine.end_session(sid, status=res.exit_status.upper())
        results.append(meta)
        (out_dir / f"session_{i}.json").write_text(json.dumps({"session_id": sid, **meta, "tool_uses": res.tool_uses,
                                                                "final_text": res.final_text}, indent=2, default=str))

    report = build_report(cfg, rt.engine.ledger, run_id, strategy, rt.catalog.status(), feed=rt.engine.feed)
    md, js = write_report(cfg, report)
    write_pass_marker(cfg, report, report["gate"])
    print(render_gate(cfg, report, report["gate"]))
    print(f"report: {md}")
    if own_runtime:
        rt.stop()
    return 0 if report["gate"]["passed"] else 1
