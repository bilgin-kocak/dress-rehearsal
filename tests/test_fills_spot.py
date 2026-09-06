from decimal import Decimal

import pytest

from rehearsal.server.errors import BinanceError
from tests.conftest import D, T0, push_depth, push_trade


def test_market_buy_walks_levels(engine):
    # asks: 0.001 @ 80001, 0.002 @ 80002, 5 @ 80010  -> 0.004 BTC fills across three levels
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.004"})
    assert r["status"] == "FILLED"
    assert [f["price"] for f in r["fills"]] == ["80001.00000000", "80002.00000000", "80010.00000000"]
    assert [f["qty"] for f in r["fills"]] == ["0.00100000", "0.00200000", "0.00100000"]
    spent = D("80001") * D("0.001") + D("80002") * D("0.002") + D("80010") * D("0.001")
    assert D(r["cummulativeQuoteQty"]) == spent
    free, locked = engine.ledger.balance("spot", "USDT")
    assert locked == D(0) and free == D(1000) - spent
    btc_free, _ = engine.ledger.balance("spot", "BTC")
    assert btc_free == D("0.004") * (1 - D("0.001"))  # commission in BTC (asset received)
    assert r["fills"][0]["commissionAsset"] == "BTC"


def test_market_partial_on_thin_book(engine):
    push_depth(engine, "spot", "BTCUSDT", [["80000.00", "1"]], [["80001.00", "0.00200000"]])
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.005"})
    assert r["status"] == "EXPIRED"
    assert r["executedQty"] == "0.00200000" and r["origQty"] == "0.00500000"
    assert engine.ledger.balance("spot", "USDT")[1] == D(0)  # nothing left locked


def test_market_by_quote_qty(engine):
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "100"})
    assert r["status"] == "FILLED"
    assert D(r["cummulativeQuoteQty"]) <= D(100)
    assert D(r["executedQty"]) > 0
    assert engine.ledger.balance("spot", "USDT")[1] == D(0)


def test_marketable_limit_fills_as_taker(engine):
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.004", "price": "80005.00"})
    # fills 0.001@80001 + 0.002@80002 (80010 is above the limit) -> partially filled, 0.001 rests
    assert r["status"] == "PARTIALLY_FILLED"
    assert D(r["executedQty"]) == D("0.003")
    assert len(engine.ledger.open_orders("spot", "BTCUSDT")) == 1
    assert all(not f["commissionAsset"] == "USDT" for f in r["fills"])
    fills = engine.ledger.fills("spot", "BTCUSDT")
    assert all(f["is_maker"] == 0 for f in fills)


def test_limit_ioc_expires_remainder(engine):
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "IOC", "quantity": "0.005", "price": "80002.00"})
    assert r["status"] == "EXPIRED"
    assert D(r["executedQty"]) == D("0.003")
    assert engine.ledger.balance("spot", "USDT")[1] == D(0)


def test_limit_fok_rejects_when_not_fully_fillable(engine):
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "FOK", "quantity": "0.005", "price": "80002.00"})
    assert r["status"] == "EXPIRED" and D(r["executedQty"]) == D(0)
    assert engine.ledger.balance("spot", "USDT") == (D(1000), D(0))


def test_limit_maker_rejected_when_marketable(engine):
    with pytest.raises(BinanceError) as ei:
        engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT_MAKER", "quantity": "0.001", "price": "80005.00"})
    assert ei.value.code == -2010 and "immediately match" in ei.value.msg


def test_resting_limit_fills_after_volume_through_price(engine):
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.010", "price": "79000.00"})
    assert r["status"] == "NEW"
    push_trade(engine, "spot", "BTCUSDT", "79500.00", "1")  # not through the price
    assert engine.ledger.get_order("spot", r["orderId"])["status"] == "NEW"
    push_trade(engine, "spot", "BTCUSDT", "79000.00", "0.004")  # at price, but queue not cleared (0.004 < 0.010)
    assert engine.ledger.get_order("spot", r["orderId"])["status"] == "NEW"
    push_trade(engine, "spot", "BTCUSDT", "79000.00", "0.006")  # cumulative 0.010 >= qty * queue_factor
    row = engine.ledger.get_order("spot", r["orderId"])
    assert row["status"] == "FILLED"
    fills = engine.ledger.fills("spot", "BTCUSDT", r["orderId"])
    assert fills[0]["is_maker"] == 1 and D(fills[0]["price"]) == D("79000")
    assert engine.ledger.balance("spot", "USDT") == (D(1000) - D("790"), D(0))


def test_resting_limit_partial_when_price_strictly_better(engine):
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.004", "price": "80050.00"}) \
        if False else None
    # need BTC first
    engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.004"})
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.003", "price": "81000.00"})
    assert r["status"] == "NEW"
    push_trade(engine, "spot", "BTCUSDT", "81005.00", "0.001")  # strictly through -> partial fill of 0.001
    row = engine.ledger.get_order("spot", r["orderId"])
    assert row["status"] == "PARTIALLY_FILLED" and D(row["executed_qty"]) == D("0.001")


def test_stop_loss_triggers_then_fills_market(engine):
    engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.004"})
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_LOSS", "quantity": "0.003", "stopPrice": "79500.00"})
    assert r["status"] == "NEW"
    push_trade(engine, "spot", "BTCUSDT", "79600.00", "0.1")
    assert engine.ledger.get_order("spot", r["orderId"])["status"] == "NEW"
    push_trade(engine, "spot", "BTCUSDT", "79499.00", "0.1")  # crosses the stop -> market sell into bids
    row = engine.ledger.get_order("spot", r["orderId"])
    assert row["status"] == "FILLED"
    assert engine.ledger.events(kind="stop_triggered")


def test_stop_would_trigger_immediately_rejected(engine):
    with pytest.raises(BinanceError) as ei:
        engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "STOP_LOSS", "quantity": "0.001", "stopPrice": "70000.00"})
    assert ei.value.code == -2010


def test_insufficient_balance(engine):
    with pytest.raises(BinanceError) as ei:
        engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"})
    assert ei.value.msg == "Account has insufficient balance for requested action."


def test_cancel_unknown_order(engine):
    with pytest.raises(BinanceError) as ei:
        engine.spot.cancel("BTCUSDT", 123456)
    assert ei.value.code == -2011


def test_replay_determinism():
    from tests.conftest import SPOT_BOOK, make_config
    from rehearsal.engine.engine import build_engine

    def run():
        eng = build_engine(make_config(), db_path=":memory:")
        eng.clock.set_ms(T0)
        push_depth(eng, "spot", "BTCUSDT", SPOT_BOOK["bids"], SPOT_BOOK["asks"], T0)
        eng.reset()
        eng.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.003"})
        eng.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.002", "price": "79000.00"})
        push_trade(eng, "spot", "BTCUSDT", "79000.00", "0.002", T0 + 500)
        out = [(f["price"], f["qty"], f["is_maker"]) for f in eng.ledger.fills("spot")]
        eq = eng.equity()["equity"]
        eng.close()
        return out, eq

    a, b = run(), run()
    assert a == b
