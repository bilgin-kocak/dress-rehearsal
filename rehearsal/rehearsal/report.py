"""Rehearsal report: reports/<run_id>/report.md + report.json (+ reports/latest.json)."""

from __future__ import annotations

import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from rehearsal.config import Config
from rehearsal.engine.ledger import Ledger
from rehearsal.rehearsal.gate import evaluate, flip_commands
from rehearsal.rehearsal.metrics import aggregate, session_metrics

SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 60) -> str:
    if not values:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return SPARK[3] * len(values)
    return "".join(SPARK[min(7, int((v - lo) / (hi - lo) * 7.999))] for v in values)


def strategy_info(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {"path": str(path) if path else None, "sha256": None, "name": None}
    data = path.read_bytes()
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()[:16], "name": path.stem}


# ---------------------------------------------------------------------------- recommendations
def recommendations(sessions: list[dict[str, Any]], summary: dict[str, Any], cfg: Config, ledger: Ledger | None = None) -> list[str]:
    recs: list[str] = []
    by_symbol: dict[str, dict[str, int]] = {}
    for s in sessions:
        for sym, codes in s["robustness"]["rejections_by_symbol"].items():
            for code, n in codes.items():
                by_symbol.setdefault(sym, {}).setdefault(code, 0)
                by_symbol[sym][code] += n

    def filt(sym: str, market: str, name: str) -> dict[str, Any] | None:
        if ledger is None:
            return None
        try:
            from rehearsal.engine.filters import filter_of
            info = None
            feed = getattr(ledger, "_feed", None)
            if feed is not None:
                info = feed.symbol_info(market, sym)
            return filter_of(info, name) if info else None
        except Exception:
            return None

    for sym, codes in sorted(by_symbol.items()):
        for code, n in sorted(codes.items(), key=lambda kv: -kv[1]):
            if code == "LOT_SIZE":
                f = filt(sym, "spot", "LOT_SIZE") or {}
                step = f.get("stepSize", "the symbol's stepSize")
                recs.append(f"{n} LOT_SIZE rejections on {sym} — the agent is not rounding quantity to stepSize {step}. "
                            f"Read spot.exchangeInfo(symbol={sym}) once and quantize before ordering.")
            elif code == "PRICE_FILTER":
                f = filt(sym, "spot", "PRICE_FILTER") or {}
                recs.append(f"{n} PRICE_FILTER rejections on {sym} — prices must be multiples of tickSize {f.get('tickSize', '(see exchangeInfo)')}.")
            elif code in ("NOTIONAL", "MIN_NOTIONAL"):
                f = filt(sym, "spot", "NOTIONAL") or {}
                recs.append(f"{n} NOTIONAL rejections on {sym} — orders below minNotional {f.get('minNotional', '(see exchangeInfo)')} USDT. Size up or skip.")
            elif code == "PRECISION":
                recs.append(f"{n} precision rejections on {sym} (-1111) — too many decimals in quantity/price; format numbers to the asset precision.")
            elif code == "INVALID_SYMBOL":
                recs.append(f"{n} calls used invalid symbol {sym} — the agent is hallucinating symbols; validate against exchangeInfo.")
            elif code == "INSUFFICIENT_BALANCE":
                recs.append(f"{n} insufficient-balance rejections on {sym} — size orders from spot.getAccount free balance, not from assumptions.")
            elif code == "MARGIN_INSUFFICIENT":
                recs.append(f"{n} margin-insufficient rejections on {sym} — transfer USDT to the futures wallet first (wallet.userUniversalTransfer MAIN_UMFUTURE) or reduce size/leverage.")
            elif code == "PERCENT_PRICE_BY_SIDE":
                recs.append(f"{n} PERCENT_PRICE_BY_SIDE rejections on {sym} — limit prices too far from the market; Binance caps them at ±20-50% of the 5-min average price.")
            elif code == "MIN_NOTIONAL":
                recs.append(f"{n} futures min-notional rejections on {sym} (-4164) — BTCUSDT perp needs ≥ 100 USDT notional (ETHUSDT ≥ 20).")
    if summary.get("twin_unsupported"):
        recs.append(f"{summary['twin_unsupported']} calls hit tools the twin does not simulate (TWIN_UNSUPPORTED) — see the tool-call log; "
                    "they will work live but were not rehearsed.")
    if summary.get("loops"):
        recs.append(f"{summary['loops']} retry loop(s): the same failing call was repeated ≥3 times. Tell the agent to read the error message and change the parameters instead of retrying.")
    viol: dict[str, int] = {}
    for s in sessions:
        for v in s["safety"]["violations"]:
            viol[v.get("rule", "?")] = viol.get(v.get("rule", "?"), 0) + 1
    for rule, n in viol.items():
        pol = cfg.policy
        detail = {"max_order_notional_usdt": f"max {pol.max_order_notional_usdt} USDT per order",
                  "max_gross_exposure_usdt": f"max {pol.max_gross_exposure_usdt} USDT gross exposure",
                  "max_leverage": f"max {pol.max_leverage}x leverage", "symbol_allowlist": f"allowlist {pol.symbol_allowlist}",
                  "max_orders_per_minute": f"max {pol.max_orders_per_minute} orders/min"}.get(rule, rule)
        recs.append(f"{n} policy violation(s) of {rule} ({detail}) — put the limit in the strategy prompt, or set policy.enforce: true.")
    if summary.get("liquidations"):
        recs.append(f"{summary['liquidations']} liquidation(s) in the twin — leverage/sizing is unsafe. Cap leverage (futures_usds.changeLeverage ≤ {cfg.policy.max_leverage}) and use a stop.")
    lf = summary.get("limit_fill_rate")
    if lf is not None and lf < cfg.gate.min_limit_fill_rate:
        recs.append(f"Limit fill rate {lf:.0%} is below {cfg.gate.min_limit_fill_rate:.0%} — limit orders rest too far from the touch or are cancelled too early.")
    cc = summary.get("confirmation_compliance")
    if cc is not None and cc < cfg.gate.min_confirmation_compliance:
        recs.append(f"Confirmation compliance {cc:.0%}: the agent placed writes without restating symbol/side/qty first. Add 'restate every order before calling the tool' to the prompt.")
    if sessions and summary.get("flattened_sessions", 0) < len(sessions):
        recs.append(f"{len(sessions) - summary.get('flattened_sessions', 0)} session(s) ended with open positions/holdings — add an explicit 'flatten before finishing' step.")
    if sessions and summary.get("canceled_stale_sessions", 0) < len(sessions):
        recs.append("Some sessions ended with resting orders — cancel stale orders (spot.deleteOpenOrders) before finishing.")
    dd = summary.get("max_drawdown_pct")
    if dd is not None and dd > cfg.gate.max_drawdown_pct:
        recs.append(f"Max drawdown {dd:.1f}% exceeds {cfg.gate.max_drawdown_pct}% — reduce position size per trade.")
    zero = [s for s in sessions if s["behaviour"]["successful_writes"] == 0]
    if zero:
        why = []
        for s in zero:
            a = s.get("agent") or {}
            why.append(f"{s['label'] or s['session_id']} (turns={a.get('turns')}, status={a.get('exit_status')})")
        recs.append(f"{len(zero)} session(s) placed no order or transfer: {', '.join(why)}. The agent refused, errored out or ran out of turns — "
                    "read the transcript in the run directory; a strategy that never acts cannot be certified.")
    if not recs:
        recs.append("No issues detected. Consider more sessions or a longer session window before going live.")
    return recs


# ---------------------------------------------------------------------------- build / write
def build_report(cfg: Config, ledger: Ledger, run_id: str, strategy: Path | None = None, catalog_status: dict[str, Any] | None = None,
                 feed=None) -> dict[str, Any]:
    if feed is not None:
        setattr(ledger, "_feed", feed)
    sess_rows = ledger.sessions(run_id=run_id)
    sessions = [session_metrics(ledger, s, cfg) for s in sess_rows]
    summary = aggregate(sessions)
    notable = []
    for s in sessions:
        for e in s["safety"]["liquidation_events"]:
            notable.append({"session": s["label"] or s["session_id"], "kind": "LIQUIDATION", **e})
        for l in s["robustness"]["loops"]:
            notable.append({"session": s["label"] or s["session_id"], "kind": "LOOP", **l})
    curves = {s["session_id"]: [[r["ts"], float(r["equity"])] for r in ledger.equity_curve(session_id=s["session_id"], limit=400)] for s in sess_rows}
    trades = []
    for s in sess_rows:
        for f in ledger.fills(session_id=s["session_id"], limit=200):
            trades.append({"session": s["label"] or s["session_id"], "ts": f["ts"], "market": f["market"], "symbol": f["symbol"], "side": f["side"],
                           "price": f["price"], "qty": f["qty"], "quote_qty": f["quote_qty"], "commission": f["commission"],
                           "commission_asset": f["commission_asset"], "maker": bool(f["is_maker"]), "slippage_bps": f["slippage_bps"],
                           "realized_pnl": f["realized_pnl"], "source": f["source"]})
    report = {
        "run_id": run_id, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "strategy": strategy_info(strategy), "mode": cfg.market.mode, "fixture": cfg.market.replay_fixture,
        "config": {"gate": cfg.gate.model_dump(), "policy": cfg.policy.model_dump(), "latency": cfg.engine.latency.model_dump(),
                   "queue_factor": cfg.engine.queue_factor, "initial_balances": cfg.engine.initial_balances, "confirm_mode": cfg.schema_.confirm_mode},
        "schema": catalog_status or {}, "sessions": sessions, "summary": summary, "equity_curves": curves, "trades": trades[:500],
        "notable_events": notable, "recommendations": recommendations(sessions, summary, cfg, ledger),
    }
    report["gate"] = evaluate(report, cfg.gate)
    report["flip_commands"] = flip_commands(cfg)
    return report


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    g = report["gate"]
    badge = "**✅ PASS**" if g["passed"] else "**❌ FAIL**"
    lines = [f"# Rehearsal report — `{report['run_id']}`", "",
             f"{badge} · strategy `{(report.get('strategy') or {}).get('name')}` (sha {(report.get('strategy') or {}).get('sha256')}) · mode `{report.get('mode')}` · "
             f"schema `{(report.get('schema') or {}).get('source')}` · generated {report['generated_at']}", ""]
    lines += ["## Summary", "", "| Metric | Value |", "|---|---|"]
    for k in ("sessions", "mean_return_pct", "worst_return_pct", "max_drawdown_pct", "tool_calls", "rejected", "rejection_rate",
              "policy_violations", "liquidations", "confirmation_compliance", "limit_orders", "limit_fill_rate", "avg_slippage_bps",
              "latency_p95_ms", "loops", "twin_unsupported", "flattened_sessions", "total_cost_usd"):
        lines.append(f"| {k} | {s.get(k)} |")
    lines += ["", "## Gate", ""]
    for c in g["criteria"]:
        mark = "✓" if c["ok"] else "✗"
        lines.append(f"- {mark} `{c['name']}` = {c['value']} (limit {c.get('op', '')} {c['threshold']}){(' — ' + c['note']) if c.get('note') else ''}")
    if g["passed"]:
        lines += ["", "Flip to live:", "", "```"] + report["flip_commands"] + ["```"]
    lines += ["", "## Equity curves", ""]
    for sess in report["sessions"]:
        curve = report["equity_curves"].get(sess["session_id"], [])
        vals = [v for _, v in curve]
        p = sess["pnl"]
        lines.append(f"- `{sess['label'] or sess['session_id']}`  {sparkline(vals)}  {p.get('initial')} → {p.get('final')} "
                     f"({p.get('return_pct')}%), max DD {p.get('max_drawdown_pct')}%")
    lines += ["", "## Trades", "", "| session | time | market | symbol | side | price | qty | fee | maker | slippage bps | realized |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in report["trades"][:60]:
        ts = time.strftime("%H:%M:%S", time.gmtime(t["ts"] / 1000)) if t.get("ts") else ""
        lines.append(f"| {t['session']} | {ts} | {t['market']} | {t['symbol']} | {t['side']} | {t['price']} | {t['qty']} | {t['commission']} {t['commission_asset']} | "
                     f"{'y' if t['maker'] else 'n'} | {t['slippage_bps'] or ''} | {t['realized_pnl']} |")
    if not report["trades"]:
        lines.append("| — | no fills | | | | | | | | | |")
    lines += ["", "## Rejections by code", ""]
    if s.get("rejections_by_code"):
        for k, v in sorted(s["rejections_by_code"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{k}`: {v}")
    else:
        lines.append("- none")
    lines += ["", "## Policy violations", ""]
    any_v = False
    for sess in report["sessions"]:
        for v in sess["safety"]["violations"]:
            any_v = True
            lines.append(f"- `{sess['label']}` {v}")
    if not any_v:
        lines.append("- none")
    lines += ["", "## Confirmation compliance", ""]
    for sess in report["sessions"]:
        c = sess["safety"]
        lines.append(f"- `{sess['label']}`: {c['confirmation_compliance'] if c['confirmation_compliance'] is not None else 'n/a'} {c.get('confirmation') or ''}")
    lines += ["", "## Notable events", ""]
    for e in report["notable_events"] or []:
        lines.append(f"- **{e['kind']}** ({e['session']}): { {k: v for k, v in e.items() if k not in ('kind', 'session')} }")
    if not report["notable_events"]:
        lines.append("- none")
    lines += ["", "## Recommendations", ""]
    for r in report["recommendations"]:
        lines.append(f"- {r}")
    lines += ["", "## Sessions", ""]
    for sess in report["sessions"]:
        lines.append(f"### {sess['label'] or sess['session_id']}")
        lines.append("```json")
        lines.append(json.dumps({k: sess[k] for k in ("pnl", "execution", "robustness", "safety", "behaviour", "agent")}, indent=1, default=str)[:4000])
        lines.append("```")
    return "\n".join(lines) + "\n"


def write_report(cfg: Config, report: dict[str, Any]) -> tuple[Path, Path]:
    d = cfg.path(cfg.runner.reports_dir) / report["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    jp = d / "report.json"
    jp.write_text(json.dumps(report, indent=2, default=str))
    mp = d / "report.md"
    mp.write_text(render_markdown(report))
    (d.parent / "latest.json").write_text(json.dumps(report, indent=2, default=str))
    (d / "gate.json").write_text(json.dumps(report["gate"], indent=2))
    return mp, jp


def save_gate(cfg: Config, report: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, default=str))
    (path.parent / "gate.json").write_text(json.dumps(report["gate"], indent=2))
    (path.parent / "report.md").write_text(render_markdown(report))
    (path.parent.parent / "latest.json").write_text(json.dumps(report, indent=2, default=str))


def load_report(cfg: Config, run_id: str | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    d = cfg.path(cfg.runner.reports_dir)
    if not d or not d.exists():
        return None, None
    if run_id:
        p = d / run_id / "report.json"
        return (json.loads(p.read_text()), p) if p.exists() else (None, None)
    latest = d / "latest.json"
    if latest.exists():
        rep = json.loads(latest.read_text())
        p = d / rep["run_id"] / "report.json"
        return rep, (p if p.exists() else latest)
    return None, None
