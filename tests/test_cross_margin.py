from tests.conftest import D, make_config, push_depth, push_mark, push_trade, SPOT_BOOK, USDM_BOOK, T0
from rehearsal.engine.engine import build_engine


def cross_engine():
    cfg = make_config(usdm={"default_leverage": 20, "default_margin_type": "CROSSED", "maintenance_rate": 0.004, "liquidation_fee": 0.0})
    eng = build_engine(cfg, db_path=":memory:")
    eng.clock.set_ms(T0)
    push_depth(eng, "spot", "BTCUSDT", SPOT_BOOK["bids"], SPOT_BOOK["asks"], T0)
    push_depth(eng, "usdm", "BTCUSDT", USDM_BOOK["bids"], USDM_BOOK["asks"], T0 + 1)
    push_trade(eng, "usdm", "BTCUSDT", "80000.00", "0.5", T0 + 2)
    push_mark(eng, "BTCUSDT", "80000.00", ts=T0 + 3)
    eng.reset()
    return eng


def test_defaults_mirror_real_account():
    eng = cross_engine()
    row = eng.usdm.position_view("BTCUSDT")[0]
    assert row["marginType"] == "cross" and row["leverage"] == "20" and row["positionAmt"] == "0.000"
    assert row["liquidationPrice"] == "0" and row["isolatedMargin"] == "0.00000000"
    eng.close()


def test_cross_liquidation_wipes_wallet():
    eng = cross_engine()
    eng.transfer("MAIN_UMFUTURE", "USDT", "500")
    r = eng.usdm.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.100"})  # 8000 USDT notional at 20x
    assert r["status"] == "FILLED"
    row = eng.usdm.position_view("BTCUSDT")[0]
    assert row["marginType"] == "cross" and row["isolatedMargin"] == "0.00000000"
    liq = D(row["liquidationPrice"])
    # cross: the whole wallet (500 - fees) backs the position -> liq well below an isolated 20x position
    assert D("74000") < liq < D("76500")
    push_mark(eng, "BTCUSDT", str((liq + 20).quantize(D("0.01"))))
    assert eng.usdm.position_view("BTCUSDT")[0]["positionAmt"] == "0.100"
    push_mark(eng, "BTCUSDT", str((liq - 5).quantize(D("0.01"))))
    assert eng.usdm.position_view("BTCUSDT")[0]["positionAmt"] == "0.000"
    ev = eng.ledger.events(kind="LIQUIDATION")
    assert ev and ev[0]["detail"]["margin_type"] == "CROSSED"
    assert eng.ledger.balance("usdm", "USDT") == (D(0), D(0))
    eng.close()


def test_switch_to_isolated_then_back():
    eng = cross_engine()
    assert eng.usdm.change_margin_type("BTCUSDT", "ISOLATED") == {"code": 200, "msg": "success"}
    assert eng.usdm.position_view("BTCUSDT")[0]["marginType"] == "isolated"
    assert eng.usdm.change_margin_type("BTCUSDT", "CROSSED")["code"] == 200
    eng.close()
