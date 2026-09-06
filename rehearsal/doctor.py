"""`rehearsal doctor`: onboarding checks with the next command to run."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from rehearsal.config import Config


def run_doctor(cfg: Config) -> int:
    ok_all = True
    rows: list[tuple[bool, str, str]] = []

    def check(ok: bool, what: str, detail: str = "") -> None:
        nonlocal ok_all
        ok_all = ok_all and ok
        rows.append((ok, what, detail))

    check(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]}", "needs >= 3.11")
    claude = shutil.which("claude")
    check(bool(claude), "Claude Code CLI", claude or "not on PATH: npm install -g @anthropic-ai/claude-code")
    if claude:
        try:
            v = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
            rows[-1] = (True, "Claude Code CLI", v)
        except Exception:
            pass
    check(bool(shutil.which("codex")), "Codex CLI (optional)", shutil.which("codex") or "not found (only needed for --client codex)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        check(True, "Anthropic auth", "ANTHROPIC_API_KEY set (use --no-api-key to bill your claude.ai login instead)")
    else:
        check(True, "Anthropic auth", "no API key; headless sessions use your claude.ai login")

    tools = cfg.path(cfg.schema_.tools_file)
    if tools and tools.exists():
        meta = (json.loads(tools.read_text()).get("_meta") or {})
        cat = cfg.path("schemas/catalog.json")
        n_cat = len(json.loads(cat.read_text()).get("tools", [])) if cat and cat.exists() else 0
        check(True, "Schema mirrored", f"{meta.get('exposed', '?')} exposed tools + {n_cat} catalog tools, dumped {meta.get('dumped_at')}")
    else:
        check(False, "Schema mirrored", "serving fallback_tools.json → authenticate binance-mcp-server in Claude Code (/mcp) then `rehearsal schema dump`")
    for name in ("demo", "holdout"):
        d = cfg.path(f"fixtures/replay/{name}")
        ok = bool(d and (d / "events.jsonl").exists())
        m = json.loads((d / "meta.json").read_text()) if ok and (d / "meta.json").exists() else {}
        check(ok, f"Replay fixture '{name}'", f"{m.get('events', '?')} events, {m.get('symbols')}" if ok else f"missing: rehearsal record --out fixtures/replay/{name}")
    try:
        r = httpx.get(f"http://{cfg.server.http_host}:{cfg.server.http_port}/api/health", timeout=2.0)
        h = r.json()
        check(True, f"Twin on :{cfg.server.http_port}", f"running, mode={h.get('mode')}, schema={h.get('schema')}")
    except Exception:
        check(True, f"Twin on :{cfg.server.http_port}", "not running (rehearsal run/coach/demo start their own)")
    try:
        r = httpx.get("https://api.binance.com/api/v3/ping", timeout=6.0)
        check(r.status_code == 200, "Binance public API", "reachable" if r.status_code == 200 else f"HTTP {r.status_code}")
    except Exception as e:
        check(False, "Binance public API", f"unreachable ({e.__class__.__name__}); live mode needs it, replay mode does not. VPN?")
    live = cfg.path("reports/GATE_PASS.json")
    if live and live.exists():
        m = json.loads(live.read_text())
        check(True, "Fresh gate PASS", f"run {m.get('run_id')} at {m.get('passed_at_iso')} for {(m.get('strategy') or {}).get('name')}")
    else:
        check(True, "Fresh gate PASS", "none yet (the skill refuses live orders until one exists)")

    width = max(len(w) for _, w, _ in rows)
    for ok, what, detail in rows:
        print(f"{'✔' if ok else '✘'} {what.ljust(width)}  {detail}")
    print()
    if not ok_all:
        print("Fix the ✘ items above, then:")
    print("Next:")
    print("  ./scripts/demo.sh                                      # 2-minute walkthrough on the recorded window")
    print("  rehearsal coach --strategy prompts/strategy_momentum_v1.md   # let the Rehearsal Agent find and fix a failure")
    print("  rehearsal coach --strategy <your_strategy.md>          # bring your own")
    return 0 if ok_all else 1
