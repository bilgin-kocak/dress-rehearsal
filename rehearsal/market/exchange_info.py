"""exchangeInfo cache with TTL and symbol lookup helpers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from rehearsal.market.binance_public import BinancePublic


class ExchangeInfoCache:
    def __init__(self, api: BinancePublic | None, ttl_s: int = 60, fixture_dir: Path | None = None):
        self.api = api
        self.ttl = ttl_s
        self.fixture_dir = fixture_dir
        self._data: dict[str, dict[str, Any]] = {}
        self._fetched: dict[str, float] = {}
        self._lock = threading.Lock()

    def load_fixture(self, market: str) -> dict[str, Any] | None:
        if not self.fixture_dir:
            return None
        p = self.fixture_dir / f"exchange_info_{market}.json"
        if p.exists():
            return json.loads(p.read_text())
        return None

    def get(self, market: str) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            if market in self._data and (self.api is None or now - self._fetched.get(market, 0) < self.ttl):
                return self._data[market]
            data = None
            if self.api is not None:
                try:
                    data = self.api.spot_exchange_info() if market == "spot" else self.api.usdm_exchange_info()
                except Exception:
                    data = self._data.get(market) or self.load_fixture(market)
            else:
                data = self.load_fixture(market)
            if data is None:
                data = {"timezone": "UTC", "serverTime": int(now * 1000), "symbols": [], "rateLimits": []}
            self._data[market] = data
            self._fetched[market] = now
            return data

    def set(self, market: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._data[market] = data
            self._fetched[market] = time.time()

    def symbol(self, market: str, symbol: str | None) -> dict[str, Any] | None:
        if not symbol:
            return None
        info = self.get(market)
        s = str(symbol).upper()
        for x in info.get("symbols", []):
            if x.get("symbol") == s:
                return x
        return None

    def filtered(self, market: str, symbol: str | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
        info = self.get(market)
        if symbol:
            syms = [x for x in info.get("symbols", []) if x.get("symbol") == symbol.upper()]
        elif symbols:
            want = {s.upper() for s in symbols}
            syms = [x for x in info.get("symbols", []) if x.get("symbol") in want]
        else:
            return info
        out = dict(info)
        out["symbols"] = syms
        return out
