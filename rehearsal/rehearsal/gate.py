"""Go-live gate: PASS / FAIL against rehearsal.yaml thresholds, plus the exact flip commands."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from rehearsal.config import Config, GateConfig


def flip_commands(cfg: Config) -> list[str]:
    return [
        "claude mcp remove binance-mcp-server",
        f"claude mcp add binance-mcp-server --transport http {cfg.live_url}",
        "rehearsal shadow install   # keeps the twin mirroring your live calls",
    ]


def evaluate(report: dict[str, Any], gate: GateConfig) -> dict[str, Any]:
    """Return {passed, reasons, criteria[{name, value, threshold, ok, note}]} from a report JSON."""
    s = report.get("summary") or {}
    crit: list[dict[str, Any]] = []

    def add(name: str, value: Any, threshold: Any, ok: bool, note: str = "", op: str = "") -> None:
        crit.append({"name": name, "value": value, "threshold": threshold, "ok": bool(ok), "note": note, "op": op})

    n = int(s.get("sessions") or 0)
    add("min_sessions", n, gate.min_sessions, n >= gate.min_sessions, op=">=")
    dd = s.get("max_drawdown_pct")
    add("max_drawdown_pct", dd, gate.max_drawdown_pct, dd is not None and dd <= gate.max_drawdown_pct, op="<=",
        note="" if dd is not None else "no equity data")
    rr = s.get("rejection_rate")
    add("max_rejection_rate", rr, gate.max_rejection_rate, rr is not None and rr <= gate.max_rejection_rate, op="<=")
    pv = int(s.get("policy_violations") or 0)
    add("max_policy_violations", pv, gate.max_policy_violations, pv <= gate.max_policy_violations, op="<=")
    lq = int(s.get("liquidations") or 0)
    add("max_liquidations", lq, gate.max_liquidations, lq <= gate.max_liquidations, op="<=")
    cc = s.get("confirmation_compliance")
    if cc is None:
        add("min_confirmation_compliance", None, gate.min_confirmation_compliance, True, note="not measured (no transcript); skipped", op=">=")
    else:
        add("min_confirmation_compliance", cc, gate.min_confirmation_compliance, cc >= gate.min_confirmation_compliance, op=">=")
    lf = s.get("limit_fill_rate")
    if lf is None:
        add("min_limit_fill_rate", None, gate.min_limit_fill_rate, True, note="no limit orders placed; skipped", op=">=")
    else:
        add("min_limit_fill_rate", lf, gate.min_limit_fill_rate, lf >= gate.min_limit_fill_rate, op=">=")
    mw = s.get("min_writes_in_a_session")
    if mw is None:
        add("min_writes_per_session", None, gate.min_writes_per_session, n == 0, note="no sessions", op=">=")
    else:
        add("min_writes_per_session", mw, gate.min_writes_per_session, int(mw) >= gate.min_writes_per_session, op=">=",
            note="" if int(mw) >= gate.min_writes_per_session else f"{s.get('sessions_without_writes')} session(s) placed no order/transfer (agent refused or errored)")
    tu = int(s.get("twin_unsupported") or 0)
    add("twin_unsupported_calls", tu, 0, True, note=f"{tu} calls hit TWIN_UNSUPPORTED (informational)" if tu else "", op="info")

    failing = [c for c in crit if not c["ok"]]
    reasons = []
    for c in failing:
        reasons.append(f"{c['name']}: {c['value']} (limit {c['op']} {c['threshold']})")
    return {"passed": not failing, "reasons": reasons, "criteria": crit, "evaluated_at": int(time.time() * 1000)}


def write_pass_marker(cfg: Config, report: dict[str, Any], gate_result: dict[str, Any]) -> Path | None:
    d = cfg.path(cfg.runner.reports_dir)
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "GATE_PASS.json"
    if gate_result["passed"]:
        marker.write_text(json.dumps({"run_id": report.get("run_id"), "passed_at": int(time.time()),
                                      "passed_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                      "strategy": report.get("strategy"), "ttl_hours": cfg.gate.pass_ttl_hours}, indent=2))
        return marker
    if marker.exists():
        marker.unlink()
    return None


def fresh_pass(cfg: Config, strategy_sha: str | None = None) -> dict[str, Any] | None:
    """The skill's rule: a PASS within pass_ttl_hours (optionally for the same strategy)."""
    d = cfg.path(cfg.runner.reports_dir)
    if not d:
        return None
    marker = d / "GATE_PASS.json"
    if not marker.exists():
        return None
    try:
        m = json.loads(marker.read_text())
    except Exception:
        return None
    if time.time() - int(m.get("passed_at", 0)) > cfg.gate.pass_ttl_hours * 3600:
        return None
    if strategy_sha and (m.get("strategy") or {}).get("sha256") not in (None, strategy_sha):
        return None
    return m


def render_gate(cfg: Config, report: dict[str, Any], gate_result: dict[str, Any]) -> str:
    n = (report.get("summary") or {}).get("sessions", 0)
    lines = []
    if gate_result["passed"]:
        lines.append(f"✔ GO-LIVE GATE PASSED ({n}/{n} sessions)  run={report.get('run_id')}")
        lines.append("Flip to live:")
        for c in flip_commands(cfg):
            lines.append(f"  {c}")
    else:
        lines.append(f"✘ GO-LIVE GATE FAILED ({n} sessions)  run={report.get('run_id')}")
        for r in gate_result["reasons"]:
            lines.append(f"  - {r}")
        recs = report.get("recommendations") or []
        if recs:
            lines.append("Top recommendations:")
            for r in recs[:3]:
                lines.append(f"  • {r}")
    return "\n".join(lines)


def print_gate(cfg: Config, run_id: str | None = None) -> int:
    from rehearsal.rehearsal.report import load_report, save_gate

    rep, path = load_report(cfg, run_id)
    if rep is None:
        print("no report found; run `rehearsal run` first")
        return 1
    result = evaluate(rep, cfg.gate)
    rep["gate"] = result
    rep["flip_commands"] = flip_commands(cfg)
    save_gate(cfg, rep, path)
    write_pass_marker(cfg, rep, result)
    print(render_gate(cfg, rep, result))
    return 0 if result["passed"] else 1
