"""Policy checks: max notional / gross exposure / symbol allowlist / leverage / order rate.

Counts violations for the report. Blocks only when `policy.enforce: true`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from decimal import Decimal
from typing import Any

from rehearsal.config import PolicyConfig
from rehearsal.engine.ledger import Ledger
from rehearsal.server import errors as E

D = Decimal


class Policy:
    def __init__(self, cfg: PolicyConfig, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger
        self._recent: deque[int] = deque(maxlen=1000)
        self._lock = threading.Lock()

    def check_order(self, market: str, symbol: str, notional: Decimal | None, leverage: int | None,
                    gross_exposure: Decimal | None, now_ms: int) -> list[dict[str, Any]]:
        """Return the list of violations (each a dict). Raises if enforce is on and any violation."""
        v: list[dict[str, Any]] = []
        allow = [s.upper() for s in self.cfg.symbol_allowlist]
        if allow and symbol.upper() not in allow:
            v.append({"rule": "symbol_allowlist", "symbol": symbol, "allowed": allow})
        if notional is not None and self.cfg.max_order_notional_usdt and notional > D(str(self.cfg.max_order_notional_usdt)):
            v.append({"rule": "max_order_notional_usdt", "notional": str(notional), "limit": self.cfg.max_order_notional_usdt})
        if leverage is not None and self.cfg.max_leverage and leverage > self.cfg.max_leverage:
            v.append({"rule": "max_leverage", "leverage": leverage, "limit": self.cfg.max_leverage})
        if gross_exposure is not None and self.cfg.max_gross_exposure_usdt and gross_exposure > D(str(self.cfg.max_gross_exposure_usdt)):
            v.append({"rule": "max_gross_exposure_usdt", "exposure": str(gross_exposure), "limit": self.cfg.max_gross_exposure_usdt})
        wall = int(time.time() * 1000)  # order rate is about the agent's real cadence, not the replay clock
        with self._lock:
            self._recent.append(wall)
            per_min = sum(1 for t in self._recent if wall - t <= 60_000)
        if self.cfg.max_orders_per_minute and per_min > self.cfg.max_orders_per_minute:
            v.append({"rule": "max_orders_per_minute", "count": per_min, "limit": self.cfg.max_orders_per_minute})
        for item in v:
            self.ledger.add_event("policy_violation", symbol, {"market": market, **item})
        if v and self.cfg.enforce:
            raise E.err(E.TWIN_POLICY_BLOCK, ", ".join(x["rule"] for x in v))
        return v

    def reset(self) -> None:
        with self._lock:
            self._recent.clear()

    def check_leverage(self, symbol: str, leverage: int) -> list[dict[str, Any]]:
        v = []
        if self.cfg.max_leverage and leverage > self.cfg.max_leverage:
            v.append({"rule": "max_leverage", "leverage": leverage, "limit": self.cfg.max_leverage})
            self.ledger.add_event("policy_violation", symbol, {"market": "usdm", **v[0]})
        if v and self.cfg.enforce:
            raise E.err(E.TWIN_POLICY_BLOCK, "max_leverage")
        return v
