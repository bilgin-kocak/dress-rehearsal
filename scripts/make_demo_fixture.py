#!/usr/bin/env python3
"""Carve a compact demo fixture out of a long recording.

Picks the window (default 10 min) in which BTCUSDT's usdm mark price falls the most from its
starting value (the demo opens a high-leverage long, so a drop is what makes the LIQUIDATION
moment happen), thins depth events to <= 2/s per stream, and writes fixtures/replay/demo/.

usage: scripts/make_demo_fixture.py fixtures/replay/long_2026-09-06 fixtures/replay/demo --minutes 10
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--depth-hz", type=float, default=1.0)
    ap.add_argument("--start-ms", type=int, default=None, help="force a window start")
    a = ap.parse_args()
    src, dst = Path(a.src), Path(a.dst)
    events = [json.loads(l) for l in (src / "events.jsonl").read_text().splitlines() if l.strip()]
    events.sort(key=lambda e: int(e["ts"]))
    win = int(a.minutes * 60 * 1000)
    symbols = sorted({e["symbol"] for e in events if e["type"] == "markPrice"})
    if not symbols:
        raise SystemExit("no mark price events")
    best = None  # (move, start, symbol, side, p0, extreme)
    if a.start_ms is None:
        for sym in symbols:
            marks = [(int(e["ts"]), float(e["data"]["p"])) for e in events if e["type"] == "markPrice" and e["symbol"] == sym]
            j = 0
            for i, (t0, p0) in enumerate(marks):
                while j < len(marks) and marks[j][0] <= t0 + win:
                    j += 1
                seg = marks[i:j]
                if not seg or seg[-1][0] - t0 < win * 0.9:
                    break
                lo = min(p for _, p in seg)
                hi = max(p for _, p in seg)
                drop, rise = (p0 - lo) / p0, (hi - p0) / p0
                # a LONG blows up on a drop, a SHORT on a rise: keep whichever is larger
                if best is None or drop > best[0]:
                    best = (drop, t0, sym, "BUY", p0, lo)
                if best is None or rise > best[0]:
                    best = (rise, t0, sym, "SELL", p0, hi)
        move, start, sym, side, p0, ext = best
        print(f"best window: {sym} {side} start={start} adverse move={move*100:.2f}% ({p0:.2f} -> {ext:.2f})")
    else:
        start, sym, side, move = a.start_ms, a.symbol, "BUY", 0.0
    end = start + win
    dst.mkdir(parents=True, exist_ok=True)
    last_depth: dict[tuple, int] = defaultdict(int)
    seeded: set[tuple] = set()
    kept = 0
    min_gap = int(1000 / a.depth_hz)
    with (dst / "events.jsonl").open("w") as f:
        # seed: last known depth/mark for each stream before the window
        latest: dict[tuple, dict] = {}
        for e in events:
            if int(e["ts"]) >= start:
                break
            if e["type"] in ("depth", "markPrice"):
                latest[(e["market"], e["symbol"], e["type"])] = e
        for e in latest.values():
            e2 = dict(e, ts=start)
            f.write(json.dumps(e2) + "\n")
            kept += 1
        for e in events:
            ts = int(e["ts"])
            if ts < start or ts > end:
                continue
            key = (e["market"], e["symbol"], e["type"])
            if e["type"] == "depth":
                if ts - last_depth[key] < min_gap:
                    continue
                last_depth[key] = ts
            f.write(json.dumps(e) + "\n")
            kept += 1
    for name in ("exchange_info_spot.json", "exchange_info_usdm.json", "snapshots.json"):
        if (src / name).exists():
            shutil.copy(src / name, dst / name)
    meta = json.loads((src / "meta.json").read_text()) if (src / "meta.json").exists() else {}
    qty = "0.5" if sym == "BTCUSDT" else "15"
    meta.update({"events": kept, "started": start, "ended": end, "derived_from": str(src),
                 "note": "demo window: largest adverse mark-price move for the scripted bad agent",
                 "demo": {"symbol": sym, "side": side, "leverage": 125, "qty": qty, "adverse_move_pct": round(move * 100, 3)}})
    (dst / "meta.json").write_text(json.dumps(meta, indent=2))
    size = (dst / "events.jsonl").stat().st_size
    print(f"wrote {dst}: {kept} events, {size/1e6:.1f} MB, window {a.minutes} min")


if __name__ == "__main__":
    main()
