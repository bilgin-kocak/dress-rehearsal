from decimal import Decimal

import pytest

from rehearsal.server.errors import BinanceError
from tests.conftest import D, push_depth, push_mark, push_trade


def fund(engine, amt="500"):
    engine.transfer("MAIN_UMFUTURE", "USDT", amt)


def test_open_long_margin_and_unrealized(engine):
    fund(engine)
    engine.usdm.change_leverage("BTCUSDT", 10)
    r = engine.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.010"})
    assert r["status"] == "FILLED" and r["avgPrice"] == "80000.10"
    pos = engine.usdm.position_view("BTCUSDT")[0]
    assert pos["positionAmt"] == "0.010" and pos["leverage"] == "10"
    notional = D("80000.10") * D("0.010")
    assert D(pos["isolatedMargin"]) == (notional / 10).quantize(D("0.00000001"))
    free, locked = engine.ledger.balance("usdm", "USDT")
    assert locked == D(0)
    fee = (notional * D("0.0005")).quantize(D("0.00000001"))
    assert free == D(500) - notional / 10 - fee
    push_mark(engine, "BTCUSDT", "81000.00")
    pos = engine.usdm.position_view("BTCUSDT")[0]
    assert D(pos["unRealizedProfit"]) == (D("81000") - D("80000.10")) * D("0.010")


def test_reduce_and_close_realizes_pnl(engine):
    fund(engine)
    engine.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.010"})
    push_depth(engine, "usdm", "BTCUSDT", [["81000.00", "5.000"]], [["81000.10", "5.000"]])
    push_mark(engine, "BTCUSDT", "81000.00")
    r = engine.usdm.place({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.005", "reduceOnly": "true"})
    assert r["status"] == "FILLED" and r["reduceOnly"] is True
    pos = engine.usdm.position_view("BTCUSDT")[0]
    assert pos["positionAmt"] == "0.005"
    fills = engine.ledger.fills("usdm", "BTCUSDT")
    assert D(fills[0]["realized_pnl"]) == (D("81000") - D("80000.10")) * D("0.005")
    r = engine.usdm.place({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.005"})
    assert engine.usdm.position_view("BTCUSDT")[0]["positionAmt"] == "0.000"
    free, locked = engine.ledger.balance("usdm", "USDT")
    assert locked == D(0)
    gross = (D("81000") - D("80000.10")) * D("0.010")
    fees = sum(D(f["commission"]) for f in engine.ledger.fills("usdm"))
    assert free == D(500) + gross - fees
    incomes = {i["income_type"] for i in engine.ledger.incomes()}
    assert {"REALIZED_PNL", "COMMISSION"} <= incomes


def test_reduce_only_rejected_without_position(engine):
    fund(engine)
    with pytest.raises(BinanceError) as ei:
        engine.usdm.place({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.002", "reduceOnly": "true"})
    assert ei.value.code == -2022


def test_margin_insufficient(engine):
    fund(engine, "10")
    with pytest.raises(BinanceError) as ei:
        engine.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.010"})
    assert ei.value.code == -2019


def test_liquidation_at_expected_mark(engine):
    fund(engine)
    engine.usdm.change_leverage("BTCUSDT", 20)
    engine.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.010"})
    pos = engine.usdm.position_view("BTCUSDT")[0]
    liq = D(pos["liquidationPrice"])
    # 20x long, mmr 0.5%: liq ≈ entry * (1 - 1/20) / (1 - 0.005)
    entry = D("80000.10")
    expected = (entry * D("0.010") - entry * D("0.010") / 20) / (D("0.010") * (1 - D("0.005")))
    assert abs(liq - expected) < D("0.01")
    push_mark(engine, "BTCUSDT", str((liq + 50).quantize(D("0.01"))))
    assert engine.usdm.position_view("BTCUSDT")[0]["positionAmt"] == "0.010"
    push_mark(engine, "BTCUSDT", str((liq - 1).quantize(D("0.01"))))
    assert engine.usdm.position_view("BTCUSDT")[0]["positionAmt"] == "0.000"
    ev = engine.ledger.events(kind="LIQUIDATION")
    assert ev and ev[0]["symbol"] == "BTCUSDT"
    free, locked = engine.ledger.balance("usdm", "USDT")
    assert locked == D(0)
    assert free < D(500) - D(40)  # lost (almost) the whole isolated margin


def test_funding_applied_on_boundary(engine):
    fund(engine)
    engine.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.010"})
    now = engine.clock.now_ms()
    boundary = ((now // 28_800_000) + 1) * 28_800_000
    push_mark(engine, "BTCUSDT", "80000.00", rate="0.0001", ts=boundary - 1000)
    assert not engine.ledger.incomes(income_type="FUNDING_FEE")
    push_mark(engine, "BTCUSDT", "80000.00", rate="0.0001", ts=boundary + 1000)
    inc = engine.ledger.incomes(income_type="FUNDING_FEE")
    assert len(inc) == 1
    assert D(inc[0]["income"]) == -(D("0.010") * D("80000") * D("0.0001"))


def test_stop_market_trigger_and_close_position(engine):
    fund(engine)
    engine.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.010"})
    r = engine.usdm.place({"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "stopPrice": "79000.00", "closePosition": "true"})
    assert r["status"] == "NEW" and r["closePosition"] is True
    push_depth(engine, "usdm", "BTCUSDT", [["78990.00", "5.000"]], [["78990.10", "5.000"]])
    push_trade(engine, "usdm", "BTCUSDT", "78995.00", "1")
    assert engine.ledger.get_order("usdm", r["orderId"])["status"] == "FILLED"
    assert engine.usdm.position_view("BTCUSDT")[0]["positionAmt"] == "0.000"


def test_limit_rests_and_locks_margin(engine):
    fund(engine)
    r = engine.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.010", "price": "79000.00"})
    assert r["status"] == "NEW"
    free, locked = engine.ledger.balance("usdm", "USDT")
    assert locked == (D("79000") * D("0.010") / 5).quantize(D("0.00000001"))
    engine.usdm.cancel("BTCUSDT", r["orderId"])
    assert engine.ledger.balance("usdm", "USDT") == (D(500), D(0))
