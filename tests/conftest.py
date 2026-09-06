"""Shared fixtures: an engine in replay mode fed with synthetic events (no network)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from rehearsal.config import Config
from rehearsal.engine.engine import Engine, build_engine

FIXTURES = Path(__file__).parent / "fixtures"
T0 = 1_788_000_000_000  # virtual clock start (ms)


def make_config(**engine_overrides: Any) -> Config:
    data: dict[str, Any] = {
        "market": {"mode": "replay", "replay_fixture": str(FIXTURES), "replay_speed": 0},
        "engine": {"initial_balances": {"spot": {"USDT": 1000}, "usdm": {"USDT": 0}},
                   "latency": {"mean_ms": 0, "sd_ms": 0, "fixed_ms": 0}, "queue_factor": 1.0,
                   "usdm": {"default_leverage": 5, "default_margin_type": "ISOLATED", "maintenance_rate": 0.005, "liquidation_fee": 0.0},
                   **engine_overrides},
        "policy": {"enforce": False, "symbol_allowlist": ["BTCUSDT", "ETHUSDT"], "max_order_notional_usdt": 500,
                   "max_gross_exposure_usdt": 2000, "max_leverage": 10, "max_orders_per_minute": 100},
    }
    cfg = Config.model_validate(data)
    cfg.root = FIXTURES.parent.parent
    return cfg


SPOT_BOOK = {
    "bids": [["80000.00", "0.50000000"], ["79999.00", "1.00000000"], ["79990.00", "2.00000000"]],
    "asks": [["80001.00", "0.00100000"], ["80002.00", "0.00200000"], ["80010.00", "5.00000000"]],
}
USDM_BOOK = {
    "bids": [["80000.00", "5.000"], ["79999.90", "10.000"]],
    "asks": [["80000.10", "5.000"], ["80000.20", "10.000"]],
}


def push_depth(engine: Engine, market: str, symbol: str, bids: list[list[str]], asks: list[list[str]], ts: int | None = None) -> None:
    ts = ts or engine.clock.now_ms()
    engine.clock.set_ms(ts)
    engine.feed.apply_event({"ts": ts, "market": market, "symbol": symbol, "type": "depth", "data": {"bids": bids, "asks": asks}})


def push_trade(engine: Engine, market: str, symbol: str, price: str, qty: str, ts: int | None = None, bim: bool = False) -> None:
    ts = ts or engine.clock.now_ms() + 100
    engine.clock.set_ms(ts)
    engine.feed.apply_event({"ts": ts, "market": market, "symbol": symbol, "type": "aggTrade", "data": {"p": price, "q": qty, "m": bim}})


def push_mark(engine: Engine, symbol: str, mark: str, rate: str = "0.0001", ts: int | None = None) -> None:
    ts = ts or engine.clock.now_ms() + 1000
    engine.clock.set_ms(ts)
    engine.feed.apply_event({"ts": ts, "market": "usdm", "symbol": symbol, "type": "markPrice",
                             "data": {"p": mark, "i": mark, "r": rate, "T": ((ts // 28_800_000) + 1) * 28_800_000}})


@pytest.fixture
def engine() -> Engine:
    cfg = make_config()
    eng = build_engine(cfg, db_path=":memory:")
    eng.clock.set_ms(T0)
    push_depth(eng, "spot", "BTCUSDT", SPOT_BOOK["bids"], SPOT_BOOK["asks"], T0)
    push_depth(eng, "spot", "ETHUSDT", [["2500.00", "10.0000"]], [["2500.10", "10.0000"]], T0)
    push_trade(eng, "spot", "BTCUSDT", "80000.50", "0.01", T0 + 1)
    push_depth(eng, "usdm", "BTCUSDT", USDM_BOOK["bids"], USDM_BOOK["asks"], T0 + 2)
    push_trade(eng, "usdm", "BTCUSDT", "80000.00", "0.5", T0 + 3)
    push_mark(eng, "BTCUSDT", "80000.00", ts=T0 + 4)
    eng.reset()
    yield eng
    eng.close()


def D(x: Any) -> Decimal:
    return Decimal(str(x))
