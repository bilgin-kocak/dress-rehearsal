"""Real-vs-sim divergence for write tools, and twin calibration suggestions."""

from __future__ import annotations

import statistics
from decimal import Decimal
from typing import Any

from rehearsal.config import Config

D = Decimal


def _num(x: Any) -> Decimal | None:
    try:
        if x in (None, ""):
            return None
        return D(str(x))
    except Exception:
        return None


def compare(tool: str, args: dict[str, Any], real: Any, sim: Any, sim_latency_ms: int | None) -> dict[str, Any]:
    out: dict[str, Any] = {"tool": tool, "symbol": args.get("symbol")}
    rd = real if isinstance(real, dict) else {}
    sd = sim if isinstance(sim, dict) else {}
    real_err = "code" in rd and "msg" in rd and rd.get("code", 0) < 0
    sim_err = "code" in sd and "msg" in sd and sd.get("code", 0) < 0
    out["real_status"] = rd.get("status") if not real_err else f"ERROR {rd.get('code')}"
    out["sim_status"] = sd.get("status") if not sim_err else f"ERROR {sd.get('code')}"
    out["status_match"] = out["real_status"] == out["sim_status"]
    if real_err or sim_err:
        out["real_msg"], out["sim_msg"] = rd.get("msg"), sd.get("msg")
        return out
    # avg price: spot FULL response has fills; futures has avgPrice.
    rp = _num(rd.get("avgPrice")) or _avg_from_fills(rd)
    sp = _num(sd.get("avgPrice")) or _avg_from_fills(sd)
    if rp and sp and rp > 0:
        out["real_avg_price"], out["sim_avg_price"] = str(rp), str(sp)
        out["avg_price_diff_bps"] = float(((sp - rp) / rp) * 10000)
    rq, sq = _num(rd.get("executedQty")), _num(sd.get("executedQty"))
    if rq is not None and sq is not None:
        out["real_executed_qty"], out["sim_executed_qty"] = str(rq), str(sq)
        out["executed_qty_diff"] = str(sq - rq)
    rt = rd.get("transactTime") or rd.get("updateTime")
    out["real_transact_time"] = rt
    out["sim_latency_ms"] = sim_latency_ms
    return out


def _avg_from_fills(d: dict[str, Any]) -> Decimal | None:
    fills = d.get("fills")
    if not isinstance(fills, list) or not fills:
        cq, eq = _num(d.get("cummulativeQuoteQty")), _num(d.get("executedQty"))
        if cq and eq and eq > 0:
            return cq / eq
        return None
    tot_q = sum((D(str(f.get("qty", 0))) for f in fills), D(0))
    tot_v = sum((D(str(f.get("qty", 0))) * D(str(f.get("price", 0))) for f in fills), D(0))
    return tot_v / tot_q if tot_q > 0 else None


def calibrate(divs: list[dict[str, Any]], cfg: Config) -> dict[str, Any]:
    """Suggest latency / queue_factor updates from observed divergences."""
    bps = [d["avg_price_diff_bps"] for d in divs if isinstance(d.get("avg_price_diff_bps"), (int, float))]
    matches = [d.get("status_match") for d in divs if "status_match" in d]
    out: dict[str, Any] = {"samples": len(divs), "status_match_rate": (sum(1 for m in matches if m) / len(matches)) if matches else None}
    if bps:
        mean_bps = statistics.fmean(bps)
        out["mean_avg_price_diff_bps"] = round(mean_bps, 2)
        out["abs_mean_bps"] = round(statistics.fmean(abs(b) for b in bps), 2)
        cur = cfg.engine.latency.mean_ms
        # Heuristic: each 1 bps of systematic under-pricing ≈ 20 ms of extra latency on a liquid book.
        suggested = max(20, int(cur + (-mean_bps) * 20)) if mean_bps < 0 else max(20, int(cur - mean_bps * 10))
        out["suggested_latency_mean_ms"] = suggested
        out["suggestion"] = (f"Twin fills are {'better' if mean_bps < 0 else 'worse'} than live by {abs(mean_bps):.1f} bps on average; "
                             f"set engine.latency.mean_ms: {suggested} (currently {cur:.0f}).")
    fill_mismatch = [d for d in divs if d.get("executed_qty_diff") not in (None, "0", "0E-8") and _num(d.get("executed_qty_diff")) not in (None, D(0))]
    if fill_mismatch:
        qf = cfg.engine.queue_factor
        out["suggested_queue_factor"] = round(qf * 1.25, 2)
        out["queue_note"] = f"{len(fill_mismatch)} executedQty mismatches; consider engine.queue_factor: {round(qf * 1.25, 2)}"
    return out
