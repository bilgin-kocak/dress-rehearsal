"""Record live public streams to a replay fixture.

Layout of fixtures/replay/<name>/:
  meta.json               {symbols, started, ended, events, streams}
  events.jsonl            merged, time-ordered feed events (depth / aggTrade / markPrice)
  exchange_info_spot.json exchangeInfo snapshot (spot), filtered to the recorded symbols
  exchange_info_usdm.json exchangeInfo snapshot (usdm), filtered to the recorded symbols
  snapshots.json          REST snapshots used by passthrough tools in replay (klines, 24hr, ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from rehearsal.market.binance_public import BinancePublic

log = logging.getLogger("rehearsal.recorder")
KLINE_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")


def _filter_exinfo(info: dict[str, Any], symbols: set[str]) -> dict[str, Any]:
    out = dict(info)
    out["symbols"] = [s for s in info.get("symbols", []) if s.get("symbol") in symbols]
    return out


def take_snapshots(api: BinancePublic, symbols: list[str], out_dir: Path) -> dict[str, Any]:
    """REST snapshots for passthrough tools + exchangeInfo, written to out_dir."""
    syms = {s.upper() for s in symbols}
    out_dir.mkdir(parents=True, exist_ok=True)
    spot_info = _filter_exinfo(api.spot_exchange_info(), syms)
    usdm_info = _filter_exinfo(api.usdm_exchange_info(), syms)
    (out_dir / "exchange_info_spot.json").write_text(json.dumps(spot_info))
    (out_dir / "exchange_info_usdm.json").write_text(json.dumps(usdm_info))
    snaps: dict[str, dict[str, Any]] = {
        "spot_ticker_24hr": {}, "spot_klines": {}, "spot_ticker_price": {}, "spot_depth": {},
        "usdm_ticker_24hr": {}, "usdm_klines": {}, "usdm_premium_index": {}, "usdm_funding_rate": {},
        "usdm_depth": {}, "usdm_ticker_price": {},
    }
    for s in sorted(syms):
        try:
            snaps["spot_ticker_24hr"][s] = api.spot_ticker_24hr(s)
            snaps["spot_ticker_price"][s] = api.spot_ticker_price(s)
            snaps["spot_klines"][s] = {iv: api.spot_klines(s, iv, limit=200) for iv in KLINE_INTERVALS}
            snaps["spot_depth"][s] = api.spot_depth(s, 100)
            snaps["usdm_ticker_24hr"][s] = api.usdm_ticker_24hr(s)
            snaps["usdm_ticker_price"][s] = api.usdm_ticker_price(s)
            snaps["usdm_klines"][s] = {iv: api.usdm_klines(s, iv, limit=200) for iv in KLINE_INTERVALS}
            snaps["usdm_premium_index"][s] = api.usdm_premium_index(s)
            snaps["usdm_funding_rate"][s] = api.usdm_funding_rate(s, limit=10)
            snaps["usdm_depth"][s] = api.usdm_depth(s, 100)
        except Exception as e:  # pragma: no cover - network
            log.warning("snapshot %s failed: %s", s, e)
        time.sleep(0.2)
    (out_dir / "snapshots.json").write_text(json.dumps(snaps))
    return snaps


async def record(symbols: list[str], minutes: float, out_dir: Path, spot_ws: str, usdm_ws: str,
                 api: BinancePublic, depth_ms: int = 1000, progress: Any = None) -> dict[str, Any]:
    import websockets

    symbols = [s.upper() for s in symbols]
    out_dir.mkdir(parents=True, exist_ok=True)
    take_snapshots(api, symbols, out_dir)
    events_path = out_dir / "events.jsonl"
    f = events_path.open("w")
    count = 0
    started = int(time.time() * 1000)
    end_at = time.time() + minutes * 60
    depth_suffix = "@100ms" if depth_ms <= 100 else ""

    def spot_streams() -> list[str]:
        return [x for s in symbols for x in (f"{s.lower()}@depth20{depth_suffix}", f"{s.lower()}@aggTrade")]

    def usdm_streams() -> list[str]:
        return [x for s in symbols for x in (f"{s.lower()}@depth20{depth_suffix}", f"{s.lower()}@trade")]

    async def poll_mark() -> None:
        """fstream markPrice streams are unreliable; poll premiumIndex once per second instead."""
        nonlocal count
        loop = asyncio.get_event_loop()
        while time.time() < end_at:
            for s in symbols:
                try:
                    pi = await loop.run_in_executor(None, api.usdm_premium_index, s)
                    f.write(json.dumps({"ts": int(pi.get("time", time.time() * 1000)), "market": "usdm", "symbol": s,
                                        "type": "markPrice", "data": {"p": pi.get("markPrice"), "i": pi.get("indexPrice"),
                                                                      "r": pi.get("lastFundingRate"), "T": pi.get("nextFundingTime", 0)}}) + "\n")
                    count += 1
                except Exception as e:  # pragma: no cover - network
                    log.warning("premiumIndex %s: %s", s, e)
            await asyncio.sleep(1.0)

    async def pump(market: str, url: str, streams: list[str]) -> None:
        nonlocal count
        backoff = 1.0
        while time.time() < end_at:
            try:
                full = url + "?streams=" + "/".join(streams)
                async with websockets.connect(full, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                    backoff = 1.0
                    # Seed with a REST depth snapshot so replay starts with a book.
                    for s in symbols:
                        d = api.spot_depth(s, 100) if market == "spot" else api.usdm_depth(s, 100)
                        f.write(json.dumps({"ts": int(time.time() * 1000), "market": market, "symbol": s, "type": "depth",
                                            "data": {"bids": d["bids"][:20], "asks": d["asks"][:20]}}) + "\n")
                        count += 1
                    while time.time() < end_at:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        msg = json.loads(raw)
                        stream, data = msg.get("stream"), msg.get("data")
                        if not stream or data is None:
                            continue
                        sym, _, kind = stream.partition("@")
                        symbol = sym.upper()
                        ts = int(data.get("E") or data.get("T") or time.time() * 1000)
                        if kind.startswith("depth"):
                            ev = {"ts": ts, "market": market, "symbol": symbol, "type": "depth",
                                  "data": {"bids": data.get("bids", data.get("b")), "asks": data.get("asks", data.get("a"))}}
                        elif kind in ("aggTrade", "trade"):
                            ev = {"ts": int(data.get("T", ts)), "market": market, "symbol": symbol, "type": "aggTrade",
                                  "data": {"p": data["p"], "q": data["q"], "m": data.get("m", False), "a": data.get("a", data.get("t"))}}
                        elif kind.startswith("markPrice"):
                            ev = {"ts": ts, "market": market, "symbol": symbol, "type": "markPrice",
                                  "data": {"p": data["p"], "i": data.get("i"), "r": data.get("r"), "T": data.get("T", 0)}}
                        else:
                            continue
                        f.write(json.dumps(ev) + "\n")
                        count += 1
                        if progress and count % 500 == 0:
                            progress(count)
            except Exception as e:  # pragma: no cover - network
                log.warning("recorder ws %s: %s; retry in %.0fs", market, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    await asyncio.gather(pump("spot", spot_ws, spot_streams()), pump("usdm", usdm_ws, usdm_streams()), poll_mark())
    f.close()
    ended = int(time.time() * 1000)
    meta = {"symbols": symbols, "started": started, "ended": ended, "events": count,
            "streams": ["depth20", "aggTrade/trade", "markPrice(premiumIndex poll)"], "depth_ms": depth_ms}
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def sort_events(out_dir: Path) -> int:
    """Ensure events.jsonl is time-ordered (streams from two sockets interleave)."""
    p = out_dir / "events.jsonl"
    lines = [json.loads(x) for x in p.read_text().splitlines() if x.strip()]
    lines.sort(key=lambda e: int(e["ts"]))
    with p.open("w") as f:
        for e in lines:
            f.write(json.dumps(e) + "\n")
    return len(lines)
