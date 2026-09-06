"""Market data feed: live (REST + WebSocket) or replay (recorded .jsonl with a virtual clock).

The engine reads from this feed only. All time comes from `feed.clock`.

Event format (recorded and replayed):
  {"ts": <ms>, "market": "spot"|"usdm", "symbol": "BTCUSDT",
   "type": "depth"|"aggTrade"|"markPrice"|"bookTicker", "data": {...raw stream payload...}}
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from rehearsal.config import MarketConfig
from rehearsal.market.binance_public import BinancePublic
from rehearsal.market.clock import Clock
from rehearsal.market.exchange_info import ExchangeInfoCache

log = logging.getLogger("rehearsal.feed")
D = Decimal

TradeCallback = Callable[[str, str, Decimal, Decimal, int, bool], None]  # market, symbol, price, qty, ts, buyer_is_maker
MarkCallback = Callable[[str, Decimal, int], None]  # symbol, mark, ts
DepthCallback = Callable[[str, str], None]  # market, symbol


class SymbolState:
    def __init__(self) -> None:
        self.bids: list[list[str]] = []
        self.asks: list[list[str]] = []
        self.depth_ts: int = 0
        self.depth_levels: int = 0
        self.last_price: Decimal | None = None
        self.last_ts: int = 0
        self.trades: deque[tuple[int, Decimal, Decimal, bool]] = deque(maxlen=50000)  # ts, price, qty, buyer_is_maker
        self.mark: Decimal | None = None
        self.mark_ts: int = 0
        self.index: Decimal | None = None
        self.funding_rate: Decimal | None = None
        self.next_funding: int = 0
        self.last_used: float = time.time()


class MarketFeed:
    """Sync accessors backed by a background thread (live WS loop or replay driver)."""

    def __init__(self, cfg: MarketConfig, clock: Clock, root: Path | None = None):
        self.cfg = cfg
        self.clock = clock
        self.root = root or Path.cwd()
        self.mode = cfg.mode
        self.state: dict[tuple[str, str], SymbolState] = {}
        self._lock = threading.RLock()
        self._trade_cbs: list[TradeCallback] = []
        self._mark_cbs: list[MarkCallback] = []
        self._depth_cbs: list[DepthCallback] = []
        self._subs: set[tuple[str, str]] = set()
        self._pending_subs: deque[tuple[str, str]] = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.replay_done = threading.Event()
        self.replay_progress: dict[str, Any] = {}
        self._clock_cv = threading.Condition()
        fixture = Path(cfg.replay_fixture) if cfg.replay_fixture else None
        if fixture and not fixture.is_absolute():
            fixture = self.root / fixture
        self.fixture_dir = fixture
        if self.mode == "replay":
            self.api: BinancePublic | None = None
            self.exinfo = ExchangeInfoCache(None, fixture_dir=self.fixture_dir)
            self._replay_snapshots = self._load_replay_snapshots()
        else:
            self.api = BinancePublic(cfg.spot_rest, cfg.usdm_rest)
            self.exinfo = ExchangeInfoCache(self.api, ttl_s=60)
            self._replay_snapshots = {}
        self.ws_status: dict[str, str] = {"spot": "idle", "usdm": "idle"}
        self.errors: deque[str] = deque(maxlen=20)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        target = self._run_replay if self.mode == "replay" else self._run_live
        self._thread = threading.Thread(target=target, name="market-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(lambda: None)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def restart(self) -> None:
        """Replay: rewind the fixture (new session). Live: no-op."""
        if self.mode != "replay":
            return
        self.stop()
        with self._lock:
            self.state.clear()
        self.replay_done.clear()
        self.replay_progress = {}
        self.start()

    def on_trade(self, cb: TradeCallback) -> None:
        self._trade_cbs.append(cb)

    def on_mark(self, cb: MarkCallback) -> None:
        self._mark_cbs.append(cb)

    def on_depth(self, cb: DepthCallback) -> None:
        self._depth_cbs.append(cb)

    def _st(self, market: str, symbol: str) -> SymbolState:
        key = (market, symbol.upper())
        with self._lock:
            st = self.state.get(key)
            if st is None:
                st = self.state[key] = SymbolState()
            st.last_used = time.time()
            return st

    def subscribe(self, market: str, symbol: str) -> None:
        key = (market, symbol.upper())
        if self.exinfo.symbol(market, key[1]) is None:
            return  # unknown symbol: handlers reject it with -1121; never hit the network for it
        self._st(*key)
        with self._lock:
            if key in self._subs:
                return
            self._subs.add(key)
            self._pending_subs.append(key)
        if self.mode == "live" and self.api is not None:
            self._prime(market, symbol.upper())

    def _prime(self, market: str, symbol: str) -> None:
        """Fetch a REST snapshot so the first read never waits for the WS."""
        try:
            self.refresh_depth(market, symbol, force=True)
            if market == "usdm":
                pi = self.api.usdm_premium_index(symbol)
                self._apply_mark(symbol, pi.get("markPrice"), pi.get("indexPrice"), pi.get("lastFundingRate"),
                                 int(pi.get("nextFundingTime", 0)), int(pi.get("time", self.clock.now_ms())), fire=False)
            tp = self.api.spot_ticker_price(symbol) if market == "spot" else self.api.usdm_ticker_price(symbol)
            st = self._st(market, symbol)
            if st.last_price is None and tp and tp.get("price"):
                st.last_price = D(tp["price"])
                st.last_ts = self.clock.now_ms()
        except Exception as e:  # pragma: no cover - network
            self.errors.append(f"prime {market}:{symbol}: {e}")
            log.warning("prime failed %s %s: %s", market, symbol, e)

    # ------------------------------------------------------------------ accessors
    def depth(self, market: str, symbol: str, limit: int = 100) -> dict[str, Any]:
        symbol = symbol.upper()
        self.subscribe(market, symbol)
        if self.mode == "live":
            self.refresh_depth(market, symbol)
        st = self._st(market, symbol)
        return {"lastUpdateId": st.depth_ts, "bids": st.bids[:limit], "asks": st.asks[:limit], "ts": st.depth_ts}

    def refresh_depth(self, market: str, symbol: str, force: bool = False) -> None:
        """Live mode: refresh REST depth (100 levels) if the cached one is older than 500 ms."""
        if self.api is None:
            return
        st = self._st(market, symbol)
        age = self.clock.now_ms() - st.depth_ts
        if not force and st.depth_levels >= min(self.cfg.depth_levels, 100) and age < 500:
            return
        if not force and st.depth_levels >= 20 and age < 500:
            return
        try:
            d = self.api.spot_depth(symbol, self.cfg.depth_levels) if market == "spot" \
                else self.api.usdm_depth(symbol, self.cfg.depth_levels)
            with self._lock:
                st.bids = [[str(p), str(q)] for p, q in d["bids"]]
                st.asks = [[str(p), str(q)] for p, q in d["asks"]]
                st.depth_ts = self.clock.now_ms()
                st.depth_levels = len(st.bids)
        except Exception as e:
            self.errors.append(f"depth {market}:{symbol}: {e}")
            log.warning("depth refresh failed %s %s: %s", market, symbol, e)

    def book_ticker(self, market: str, symbol: str) -> dict[str, str] | None:
        d = self.depth(market, symbol, 1)
        if not d["bids"] or not d["asks"]:
            return None
        return {"symbol": symbol.upper(), "bidPrice": d["bids"][0][0], "bidQty": d["bids"][0][1],
                "askPrice": d["asks"][0][0], "askQty": d["asks"][0][1]}

    def mid(self, market: str, symbol: str) -> Decimal | None:
        bt = self.book_ticker(market, symbol)
        if not bt:
            return self.last_price(market, symbol)
        return (D(bt["bidPrice"]) + D(bt["askPrice"])) / 2

    def last_price(self, market: str, symbol: str) -> Decimal | None:
        symbol = symbol.upper()
        self.subscribe(market, symbol)
        st = self._st(market, symbol)
        if st.last_price is None and self.mode == "live" and self.api is not None:
            self._prime(market, symbol)
        if st.last_price is None:
            m = self._mid_from_depth(st)
            return m
        return st.last_price

    def _mid_from_depth(self, st: SymbolState) -> Decimal | None:
        if st.bids and st.asks:
            return (D(st.bids[0][0]) + D(st.asks[0][0])) / 2
        return None

    def mark_price(self, symbol: str) -> Decimal | None:
        symbol = symbol.upper()
        self.subscribe("usdm", symbol)
        st = self._st("usdm", symbol)
        if st.mark is None and self.mode == "live" and self.api is not None:
            self._prime("usdm", symbol)
        if st.mark is None:
            return self.last_price("usdm", symbol)
        return st.mark

    def premium_index(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        mark = self.mark_price(symbol)
        st = self._st("usdm", symbol)
        return {
            "symbol": symbol,
            "markPrice": _fmt(mark, 8),
            "indexPrice": _fmt(st.index or mark, 8),
            "estimatedSettlePrice": _fmt(mark, 8),
            "lastFundingRate": _fmt(st.funding_rate or D(0), 8),
            "interestRate": "0.00010000",
            "nextFundingTime": st.next_funding or _next_funding_ts(self.clock.now_ms()),
            "time": self.clock.now_ms(),
        }

    def funding_rate(self, symbol: str) -> Decimal:
        st = self._st("usdm", symbol.upper())
        if st.funding_rate is None and self.mode == "live" and self.api is not None:
            self._prime("usdm", symbol.upper())
        return st.funding_rate or D(0)

    def trades_since(self, market: str, symbol: str, since_ts: int) -> list[tuple[int, Decimal, Decimal, bool]]:
        st = self._st(market, symbol.upper())
        with self._lock:
            return [t for t in st.trades if t[0] >= since_ts]

    def symbol_info(self, market: str, symbol: str | None) -> dict[str, Any] | None:
        return self.exinfo.symbol(market, symbol)

    def exchange_info(self, market: str, symbol: str | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
        return self.exinfo.filtered(market, symbol, symbols)

    # ---- passthroughs (live) / snapshots (replay)
    def passthrough(self, name: str, **kw: Any) -> Any:
        """Call a public REST endpoint by name. In replay mode, serve recorded snapshots when present."""
        if self.mode == "replay":
            snap = self._replay_snapshots.get(name)
            key = (kw.get("symbol") or "").upper()
            if isinstance(snap, dict) and key in snap:
                return snap[key]
            if isinstance(snap, dict) and "*" in snap:
                return snap["*"]
            return self._synth_passthrough(name, **kw)
        assert self.api is not None
        fn = getattr(self.api, name)
        return fn(**kw)

    def _synth_passthrough(self, name: str, **kw: Any) -> Any:
        """Replay fallback built from the feed's own state (no invented prices)."""
        symbol = (kw.get("symbol") or "").upper()
        market = "usdm" if name.startswith("usdm") else "spot"
        if "ticker_price" in name:
            p = self.last_price(market, symbol)
            return {"symbol": symbol, "price": _fmt(p, 8)}
        if "ticker_24hr" in name or "rolling" in name:
            p = self.last_price(market, symbol) or D(0)
            st = self._st(market, symbol)
            prices = [t[1] for t in st.trades] or [p]
            return {"symbol": symbol, "priceChange": "0", "priceChangePercent": "0", "weightedAvgPrice": _fmt(p, 8),
                    "lastPrice": _fmt(p, 8), "highPrice": _fmt(max(prices), 8), "lowPrice": _fmt(min(prices), 8),
                    "volume": _fmt(sum(t[2] for t in st.trades), 8), "openPrice": _fmt(prices[0], 8),
                    "openTime": self.clock.now_ms() - 86400000, "closeTime": self.clock.now_ms(), "count": len(st.trades),
                    "_twin_note": "replay fixture has no 24hr snapshot; derived from recorded trades"}
        if "klines" in name:
            return self._klines_from_trades(market, symbol, kw.get("interval") or "1m", int(kw.get("limit") or 100))
        if "premium_index" in name:
            return self.premium_index(symbol)
        if "funding_rate" in name:
            st = self._st("usdm", symbol)
            return [{"symbol": symbol, "fundingRate": _fmt(st.funding_rate or D(0), 8),
                     "fundingTime": st.next_funding - 8 * 3600 * 1000 if st.next_funding else self.clock.now_ms(),
                     "markPrice": _fmt(st.mark, 8)}]
        if "book_ticker" in name:
            return self.book_ticker(market, symbol)
        if "agg_trades" in name:
            st = self._st(market, symbol)
            return [{"a": i, "p": _fmt(t[1], 8), "q": _fmt(t[2], 8), "T": t[0], "m": t[3]} for i, t in
                    enumerate(list(st.trades)[-int(kw.get("limit") or 500):])]
        return {"_twin_note": f"no replay snapshot for {name}"}

    def _klines_from_trades(self, market: str, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        secs = _interval_seconds(interval)
        st = self._st(market, symbol)
        buckets: dict[int, list[Any]] = {}
        for ts, p, q, _ in list(st.trades):
            b = (ts // 1000 // secs) * secs * 1000
            k = buckets.get(b)
            if k is None:
                buckets[b] = [b, p, p, p, p, q, b + secs * 1000 - 1, p * q, 1, D(0), D(0), "0"]
            else:
                k[2] = max(k[2], p); k[3] = min(k[3], p); k[4] = p; k[5] += q; k[7] += p * q; k[8] += 1
        out = []
        for b in sorted(buckets)[-limit:]:
            k = buckets[b]
            out.append([k[0], _fmt(k[1], 8), _fmt(k[2], 8), _fmt(k[3], 8), _fmt(k[4], 8), _fmt(k[5], 8), k[6],
                        _fmt(k[7], 8), k[8], "0", "0", "0"])
        return out

    # ------------------------------------------------------------------ waiting (latency model)
    def wait_until(self, ts_ms: int, timeout_s: float = 30.0) -> None:
        """Block until clock >= ts_ms. Live: sleep. Replay: wait for the replay driver."""
        if self.mode != "replay":
            delta = (ts_ms - self.clock.now_ms()) / 1000.0
            if delta > 0:
                time.sleep(min(delta, timeout_s))
            return
        deadline = time.time() + timeout_s
        with self._clock_cv:
            while self.clock.now_ms() < ts_ms and not self.replay_done.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self._clock_cv.wait(timeout=min(remaining, 0.5))

    # ------------------------------------------------------------------ apply events (shared)
    def apply_event(self, ev: dict[str, Any], fire: bool = True) -> None:
        market, symbol, typ, data = ev["market"], ev["symbol"].upper(), ev["type"], ev["data"]
        ts = int(ev.get("ts") or self.clock.now_ms())
        st = self._st(market, symbol)
        if typ == "depth":
            with self._lock:
                st.bids = [[str(p), str(q)] for p, q in data.get("bids", data.get("b", []))]
                st.asks = [[str(p), str(q)] for p, q in data.get("asks", data.get("a", []))]
                st.depth_ts = ts
                st.depth_levels = len(st.bids)
            if fire:
                for cb in self._depth_cbs:
                    _safe(cb, market, symbol)
        elif typ == "aggTrade":
            price, qty = D(str(data["p"])), D(str(data["q"]))
            bim = bool(data.get("m", False))
            with self._lock:
                st.last_price = price
                st.last_ts = ts
                st.trades.append((ts, price, qty, bim))
            if fire:
                for cb in self._trade_cbs:
                    _safe(cb, market, symbol, price, qty, ts, bim)
        elif typ == "markPrice":
            self._apply_mark(symbol, data.get("p"), data.get("i"), data.get("r"), int(data.get("T", 0)), ts, fire=fire)
        elif typ == "bookTicker":
            with self._lock:
                if data.get("b") and data.get("a"):
                    if st.depth_levels <= 1 or ts >= st.depth_ts:
                        st.bids = [[str(data["b"]), str(data["B"])]] + st.bids[1:]
                        st.asks = [[str(data["a"]), str(data["A"])]] + st.asks[1:]

    def _apply_mark(self, symbol: str, mark: Any, index: Any, rate: Any, next_funding: int, ts: int, fire: bool) -> None:
        st = self._st("usdm", symbol)
        with self._lock:
            if mark is not None:
                st.mark = D(str(mark))
                st.mark_ts = ts
            if index is not None:
                st.index = D(str(index))
            if rate is not None:
                st.funding_rate = D(str(rate))
            if next_funding:
                st.next_funding = next_funding
        if fire and st.mark is not None:
            for cb in self._mark_cbs:
                _safe(cb, symbol, st.mark, ts)

    # ------------------------------------------------------------------ live loop
    def _run_live(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._live_main())
        finally:
            self._loop.close()

    async def _live_main(self) -> None:
        tasks = [asyncio.create_task(self._ws_loop("spot")), asyncio.create_task(self._ws_loop("usdm")),
                 asyncio.create_task(self._poll_loop())]
        while not self._stop.is_set():
            await asyncio.sleep(0.2)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _poll_loop(self) -> None:
        """REST polling fallback (1 s) for symbols whose WS data is stale, plus idle unsubscribe."""
        while not self._stop.is_set():
            await asyncio.sleep(max(0.5, self.cfg.rest_poll_ms / 1000.0))
            now = self.clock.now_ms()
            loop = asyncio.get_event_loop()
            for (market, symbol) in list(self._subs):
                st = self._st(market, symbol)
                if self.ws_status.get(market) != "connected" or now - st.depth_ts > 5000:
                    await loop.run_in_executor(None, self.refresh_depth, market, symbol, True)
                if market == "usdm":
                    await loop.run_in_executor(None, self._poll_mark, symbol)

    def _poll_mark(self, symbol: str) -> None:
        """Mark price / funding via REST premiumIndex (weight 1)."""
        if self.api is None:
            return
        try:
            pi = self.api.usdm_premium_index(symbol)
            self.apply_event({"ts": int(pi.get("time", self.clock.now_ms())), "market": "usdm", "symbol": symbol,
                              "type": "markPrice", "data": {"p": pi.get("markPrice"), "i": pi.get("indexPrice"),
                                                            "r": pi.get("lastFundingRate"), "T": pi.get("nextFundingTime", 0)}})
        except Exception as e:  # pragma: no cover - network
            self.errors.append(f"mark {symbol}: {e}")

    def _streams_for(self, market: str, symbol: str) -> list[str]:
        s = symbol.lower()
        if market == "spot":
            return [f"{s}@depth20@100ms", f"{s}@aggTrade"]
        # fstream: aggTrade/markPrice streams are unreliable; use trade + REST premiumIndex polling.
        return [f"{s}@depth20@100ms", f"{s}@trade"]

    async def _ws_loop(self, market: str) -> None:
        import websockets

        url = self.cfg.spot_ws if market == "spot" else self.cfg.usdm_ws
        backoff = 1.0
        while not self._stop.is_set():
            subs = [k for k in self._subs if k[0] == market]
            if not subs:
                await asyncio.sleep(0.5)
                continue
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                    self.ws_status[market] = "connected"
                    backoff = 1.0
                    sent: set[tuple[str, str]] = set()
                    req_id = 1

                    async def sync_subs() -> None:
                        nonlocal req_id
                        want = [k for k in self._subs if k[0] == market and k not in sent]
                        if want:
                            params = [st for k in want for st in self._streams_for(*k)]
                            await ws.send(json.dumps({"method": "SUBSCRIBE", "params": params, "id": req_id}))
                            req_id += 1
                            sent.update(want)

                    await sync_subs()
                    while not self._stop.is_set():
                        await sync_subs()
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        msg = json.loads(raw)
                        stream = msg.get("stream")
                        data = msg.get("data")
                        if not stream or data is None:
                            continue
                        sym, _, kind = stream.partition("@")
                        symbol = sym.upper()
                        ts = int(data.get("E") or data.get("T") or self.clock.now_ms())
                        if kind.startswith("depth"):
                            self.apply_event({"ts": ts, "market": market, "symbol": symbol, "type": "depth",
                                              "data": {"bids": data.get("bids", data.get("b")), "asks": data.get("asks", data.get("a"))}})
                        elif kind in ("aggTrade", "trade"):
                            self.apply_event({"ts": int(data.get("T", ts)), "market": market, "symbol": symbol,
                                              "type": "aggTrade", "data": {"p": data["p"], "q": data["q"], "m": data.get("m", False), "a": data.get("a", data.get("t"))}})
                        elif kind.startswith("markPrice"):
                            self.apply_event({"ts": ts, "market": market, "symbol": symbol, "type": "markPrice",
                                              "data": {"p": data["p"], "i": data.get("i"), "r": data.get("r"), "T": data.get("T", 0)}})
            except Exception as e:  # pragma: no cover - network
                self.ws_status[market] = f"reconnecting ({e.__class__.__name__})"
                self.errors.append(f"ws {market}: {e}")
                log.warning("ws %s error: %s; reconnecting in %.0fs", market, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
        self.ws_status[market] = "stopped"

    # ------------------------------------------------------------------ replay
    def _load_replay_snapshots(self) -> dict[str, Any]:
        if not self.fixture_dir or not (self.fixture_dir / "snapshots.json").exists():
            return {}
        try:
            return json.loads((self.fixture_dir / "snapshots.json").read_text())
        except Exception:
            return {}

    def replay_events_path(self) -> Path | None:
        if not self.fixture_dir:
            return None
        p = self.fixture_dir / "events.jsonl"
        return p if p.exists() else None

    def close(self) -> None:
        self.stop()
        if self.api:
            self.api.close()

    def _run_replay(self) -> None:
        path = self.replay_events_path()
        if not path:
            log.error("replay fixture missing: %s", self.fixture_dir)
            self.replay_done.set()
            return
        speed = self.cfg.replay_speed
        meta = {}
        mp = self.fixture_dir / "meta.json"
        if mp.exists():
            meta = json.loads(mp.read_text())
        total = int(meta.get("events", 0))
        wall_start = time.time()
        try:
            self._replay_loop(path, speed, total, wall_start)
        except Exception as e:  # pragma: no cover
            log.exception("replay driver crashed: %s", e)
            self.errors.append(f"replay: {e}")
        finally:
            self.replay_done.set()
            with self._clock_cv:
                self._clock_cv.notify_all()

    def _replay_loop(self, path: Path, speed: float, total: int, wall_start: float) -> None:
        n = 0
        first_ts: int | None = None
        with path.open() as f:
            for line in f:
                if self._stop.is_set():
                    break
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                    ts = int(ev["ts"])
                except (ValueError, KeyError, TypeError):
                    log.warning("replay: skipping malformed line")
                    continue
                if first_ts is None:
                    first_ts = ts
                    self.clock.force_ms(ts)  # fixtures are in the past: jump the virtual clock to them
                if speed > 0:
                    target_wall = wall_start + (ts - first_ts) / 1000.0 / speed
                    delay = target_wall - time.time()
                    if delay > 0:
                        time.sleep(min(delay, 5.0))
                self.clock.set_ms(ts)
                self.apply_event(ev)
                with self._clock_cv:
                    self._clock_cv.notify_all()
                n += 1
                if n % 200 == 0:
                    self.replay_progress = {"events": n, "total": total, "ts": ts,
                                            "pct": round(100 * n / total, 1) if total else None}
        self.replay_progress = {"events": n, "total": total, "ts": self.clock.now_ms(), "pct": 100.0}

    def replay_step_all(self) -> int:
        """Tests: apply the whole fixture synchronously."""
        path = self.replay_events_path()
        if not path:
            return 0
        n = 0
        with path.open() as f:
            for line in f:
                if line.strip():
                    ev = json.loads(line)
                    if n == 0:
                        self.clock.force_ms(int(ev["ts"]))
                    self.clock.set_ms(int(ev["ts"]))
                    self.apply_event(ev)
                    n += 1
        self.replay_done.set()
        return n

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fixture": str(self.fixture_dir) if self.fixture_dir else None,
            "replay_speed": self.cfg.replay_speed,
            "replay_progress": self.replay_progress,
            "replay_done": self.replay_done.is_set(),
            "ws": self.ws_status,
            "subscribed": sorted(f"{m}:{s}" for m, s in self._subs),
            "errors": list(self.errors)[-5:],
            "clock_ms": self.clock.now_ms(),
        }


# ---------------------------------------------------------------------------- utils
def _fmt(x: Decimal | None, places: int) -> str:
    if x is None:
        return "0"
    return format(x.quantize(D(1).scaleb(-places)), "f")


def _safe(cb: Callable[..., Any], *a: Any) -> None:
    try:
        cb(*a)
    except Exception as e:  # pragma: no cover
        log.exception("feed callback failed: %s", e)


def _next_funding_ts(now_ms: int) -> int:
    period = 8 * 3600 * 1000
    return (now_ms // period + 1) * period


def _interval_seconds(interval: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000}
    try:
        return int(interval[:-1]) * units[interval[-1]]
    except Exception:
        return 60
