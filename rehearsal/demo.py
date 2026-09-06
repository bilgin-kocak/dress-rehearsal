"""`rehearsal demo`: replay twin + dashboard, seeded with a scripted "deliberately bad" run (gate FAIL).

No LLM is needed: the seed replays the exact tool-call sequence a careless agent produces, so the
dashboard shows LOT_SIZE / precision rejections, a policy violation, a 75x long and (if the fixture
moves enough) a LIQUIDATION, then the gate prints FAIL with reasons.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from rehearsal.config import Config
from rehearsal.rehearsal.gate import render_gate, write_pass_marker
from rehearsal.rehearsal.report import build_report, write_report
from rehearsal.runtime import Runtime

log = logging.getLogger("rehearsal.demo")

def bad_sequence(symbol: str = "BTCUSDT", side: str = "BUY", leverage: int = 125, qty: str = "0.5") -> list[tuple[str, dict[str, Any], str]]:
    """The tool calls a careless agent produces (see prompts/strategy_deliberately_bad.md)."""
    big = "2" if symbol == "BTCUSDT" else "60"  # > wallet / leverage -> -2019 Margin is insufficient
    return [
        ("spot.getAccount", {"omitZeroBalances": True}, ""),
        ("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0012345678"}, "precision -1111"),
        ("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0012345678"}, "retry (loop)"),
        ("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0012345678"}, "retry (loop)"),
        ("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.00005"}, "NOTIONAL"),
        ("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.00123"}, "ok ~100 USDT"),
        ("spot.newOrder", {"symbol": "ETHUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.05", "price": "2222.222"}, "PRICE_FILTER"),
        ("spot.newOrder", {"symbol": "ETHUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.05", "price": "2222.22"}, "rests far from mid"),
        ("wallet.userUniversalTransfer", {"type": "MAIN_UMFUTURE", "asset": "USDT", "amount": "700"}, "transfer"),
        ("futures_usds.changeLeverage", {"symbol": symbol, "leverage": leverage}, "policy: leverage"),
        ("futures_usds.newOrder", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": big}, "margin insufficient"),
        ("futures_usds.newOrder", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": big}, "retry (loop)"),
        ("futures_usds.newOrder", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": big}, "retry (loop)"),
        ("futures_usds.newOrder", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": qty}, f"{leverage}x {side} ~{qty} {symbol}"),
        ("futures_usds.positionRisk", {"symbol": symbol}, ""),
    ]


BAD_SEQUENCE = bad_sequence()


def seed_bad_run(rt: Runtime, run_id: str = "demo_bad", strategy: Path | None = None, wait_replay_s: float = 0.0) -> dict[str, Any]:
    """Execute BAD_SEQUENCE as one session, then let the replay run so the position bleeds / liquidates."""
    cfg = rt.cfg
    sid = f"{run_id}_s1"
    rt.engine.start_session(sid, run_id=run_id, label="session 1", reset=True,
                            meta={"client": "scripted", "strategy": str(strategy) if strategy else "prompts/strategy_deliberately_bad.md",
                                  "confirmation": {"writes": 0, "restated": 0}, "exit_status": "success", "turns": 0})
    writes = 0
    demo_meta = {}
    try:
        mp = cfg.path(cfg.market.replay_fixture) / "meta.json"
        demo_meta = (json.loads(mp.read_text()) if mp.exists() else {}).get("demo") or {}
    except Exception:
        demo_meta = {}
    seq = bad_sequence(demo_meta.get("symbol", "BTCUSDT"), demo_meta.get("side", "BUY"), int(demo_meta.get("leverage", 125)),
                       str(demo_meta.get("qty", "0.5")))
    for tool, args, note in seq:
        out = rt.twin.execute(tool, dict(args), sid, source="agent")
        spec = rt.catalog.resolve(tool)
        if spec and spec.write:
            writes += 1
        log.info("seed %-32s %-8s %s %s", tool, out["status"], (out["result"].get("msg") if isinstance(out["result"], dict) and out["status"] == "error" else ""), note)
        time.sleep(0.15)
    meta = {"client": "scripted", "strategy": "prompts/strategy_deliberately_bad.md", "confirmation": {"writes": writes, "restated": 0},
            "exit_status": "success", "turns": len(seq)}
    rt.engine.ledger._exec("UPDATE sessions SET meta=? WHERE session_id=?", (json.dumps(meta), sid))
    if wait_replay_s > 0:
        t0 = time.time()
        while time.time() - t0 < wait_replay_s and not rt.engine.feed.replay_done.is_set():
            time.sleep(0.5)
            if rt.engine.ledger.events(kind="LIQUIDATION"):
                break
    rt.engine.end_session(sid, status="DONE")
    report = build_report(cfg, rt.engine.ledger, run_id, strategy or (cfg.root / "prompts" / "strategy_deliberately_bad.md"),
                          rt.catalog.status(), feed=rt.engine.feed)
    write_report(cfg, report)
    write_pass_marker(cfg, report, report["gate"])
    return report


def run_demo(cfg: Config, seed_only: bool = False, open_browser: bool = True) -> None:
    fixture = cfg.path(cfg.market.replay_fixture)
    if not fixture or not (fixture / "events.jsonl").exists():
        print(f"demo fixture missing: {fixture}\nRecord one with:  rehearsal record --symbols BTCUSDT,ETHUSDT --minutes 10 --out {fixture}",
              file=sys.stderr)
        sys.exit(2)
    rt = Runtime(cfg, db_path=cfg.path(".rehearsal/demo.db"))
    rt.start()
    rt.start_dashboard_thread()
    url = f"http://{cfg.server.http_host}:{cfg.server.http_port}/"
    print(f"Dress Rehearsal DEMO — replay {fixture.name} at {cfg.market.replay_speed}x", file=sys.stderr)
    print(f"dashboard: {url}   MCP: {url}mcp", file=sys.stderr)
    time.sleep(1.0)

    def seed() -> None:
        time.sleep(1.5)
        rep = seed_bad_run(rt, wait_replay_s=0 if seed_only else 45)
        print("\n" + render_gate(cfg, rep, rep["gate"]), file=sys.stderr)
        print(f"report: {cfg.path(cfg.runner.reports_dir) / rep['run_id'] / 'report.md'}\n", file=sys.stderr)

    if open_browser and not seed_only:
        threading.Thread(target=lambda: (time.sleep(2.0), webbrowser.open(url)), daemon=True).start()
    if seed_only:
        seed()
        rt.stop()
        return
    threading.Thread(target=seed, daemon=True).start()
    try:
        while True:
            time.sleep(1.0)
            if rt.engine.feed.replay_done.is_set():
                log.info("replay finished; restarting fixture so the dashboard keeps moving")
                rt.engine.feed.restart()
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()
