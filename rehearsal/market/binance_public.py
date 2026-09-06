"""Thin client for the public (unauthenticated) Binance REST endpoints.

Spot: https://api.binance.com   USDⓈ-M: https://fapi.binance.com
Respects weight headers and backs off on 429/418.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("rehearsal.market")


class BinancePublic:
    def __init__(self, spot_base: str = "https://api.binance.com", usdm_base: str = "https://fapi.binance.com",
                 timeout: float = 10.0):
        self.bases = {"spot": spot_base.rstrip("/"), "usdm": usdm_base.rstrip("/")}
        self.client = httpx.Client(timeout=timeout, headers={"User-Agent": "dress-rehearsal/0.1"})
        self._backoff_until = 0.0
        self._lock = threading.Lock()
        self.last_weight: dict[str, int] = {}

    def close(self) -> None:
        self.client.close()

    def _get(self, market: str, path: str, params: dict[str, Any] | None = None) -> Any:
        now = time.time()
        if now < self._backoff_until:
            raise RuntimeError(f"rate-limited; backing off for {self._backoff_until - now:.0f}s")
        url = self.bases[market] + path
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        r = self.client.get(url, params=clean)
        w = r.headers.get("x-mbx-used-weight-1m")
        if w:
            self.last_weight[market] = int(w)
        if r.status_code in (429, 418):
            retry = float(r.headers.get("Retry-After", "30"))
            self._backoff_until = time.time() + retry
            log.warning("Binance %s rate limit (%s); backing off %ss", market, r.status_code, retry)
            raise RuntimeError(f"rate-limited ({r.status_code})")
        if r.status_code >= 400:
            # Surface the Binance error body verbatim (e.g. {"code":-1121,"msg":"Invalid symbol."}).
            try:
                body = r.json()
            except Exception:
                body = {"code": -1000, "msg": r.text[:200]}
            raise BinanceHTTPError(r.status_code, body)
        return r.json()

    # ---- spot
    def spot_exchange_info(self, symbol: str | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        elif symbols:
            params["symbols"] = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        return self._get("spot", "/api/v3/exchangeInfo", params)

    def spot_depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        return self._get("spot", "/api/v3/depth", {"symbol": symbol, "limit": limit})

    def spot_book_ticker(self, symbol: str | None = None) -> Any:
        return self._get("spot", "/api/v3/ticker/bookTicker", {"symbol": symbol})

    def spot_ticker_price(self, symbol: str | None = None, symbols: list[str] | None = None) -> Any:
        params: dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        elif symbols:
            params["symbols"] = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        return self._get("spot", "/api/v3/ticker/price", params)

    def spot_ticker_24hr(self, symbol: str | None = None, symbols: list[str] | None = None, type_: str | None = None) -> Any:
        params: dict[str, Any] = {"type": type_}
        if symbol:
            params["symbol"] = symbol
        elif symbols:
            params["symbols"] = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        return self._get("spot", "/api/v3/ticker/24hr", params)

    def spot_rolling_ticker(self, symbol: str, window: str | None = None, type_: str | None = None) -> Any:
        return self._get("spot", "/api/v3/ticker", {"symbol": symbol, "windowSize": window, "type": type_})

    def spot_klines(self, symbol: str, interval: str, start: int | None = None, end: int | None = None,
                    limit: int | None = None, ui: bool = False) -> Any:
        path = "/api/v3/uiKlines" if ui else "/api/v3/klines"
        return self._get("spot", path, {"symbol": symbol, "interval": interval, "startTime": start,
                                        "endTime": end, "limit": limit})

    def spot_agg_trades(self, symbol: str, limit: int | None = None, from_id: int | None = None,
                        start: int | None = None, end: int | None = None) -> Any:
        return self._get("spot", "/api/v3/aggTrades", {"symbol": symbol, "limit": limit, "fromId": from_id,
                                                        "startTime": start, "endTime": end})

    def spot_avg_price(self, symbol: str) -> Any:
        return self._get("spot", "/api/v3/avgPrice", {"symbol": symbol})

    def spot_time(self) -> Any:
        return self._get("spot", "/api/v3/time")

    # ---- usdm
    def usdm_exchange_info(self) -> dict[str, Any]:
        return self._get("usdm", "/fapi/v1/exchangeInfo")

    def usdm_depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        return self._get("usdm", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})

    def usdm_premium_index(self, symbol: str | None = None) -> Any:
        return self._get("usdm", "/fapi/v1/premiumIndex", {"symbol": symbol})

    def usdm_funding_rate(self, symbol: str | None = None, start: int | None = None, end: int | None = None,
                          limit: int | None = None) -> Any:
        return self._get("usdm", "/fapi/v1/fundingRate", {"symbol": symbol, "startTime": start, "endTime": end,
                                                          "limit": limit})

    def usdm_klines(self, symbol: str, interval: str, start: int | None = None, end: int | None = None,
                    limit: int | None = None) -> Any:
        return self._get("usdm", "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "startTime": start,
                                                     "endTime": end, "limit": limit})

    def usdm_ticker_price(self, symbol: str | None = None) -> Any:
        return self._get("usdm", "/fapi/v2/ticker/price", {"symbol": symbol})

    def usdm_ticker_24hr(self, symbol: str | None = None) -> Any:
        return self._get("usdm", "/fapi/v1/ticker/24hr", {"symbol": symbol})

    def usdm_book_ticker(self, symbol: str | None = None) -> Any:
        return self._get("usdm", "/fapi/v1/ticker/bookTicker", {"symbol": symbol})

    def usdm_open_interest(self, symbol: str) -> Any:
        return self._get("usdm", "/fapi/v1/openInterest", {"symbol": symbol})

    def usdm_agg_trades(self, symbol: str, limit: int | None = None) -> Any:
        return self._get("usdm", "/fapi/v1/aggTrades", {"symbol": symbol, "limit": limit})


class BinanceHTTPError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body
