import json
from decimal import Decimal
from pathlib import Path

import pytest

from rehearsal.engine.filters import validate_leverage, validate_spot_order, validate_usdm_order
from rehearsal.server.errors import BinanceError

FIX = Path(__file__).parent / "fixtures"
SPOT = {s["symbol"]: s for s in json.loads((FIX / "exchange_info_spot.json").read_text())["symbols"]}
USDM = {s["symbol"]: s for s in json.loads((FIX / "exchange_info_usdm.json").read_text())["symbols"]}
REF = Decimal("80000")


def rej(fn, *a):
    with pytest.raises(BinanceError) as ei:
        fn(*a)
    return ei.value.code, ei.value.msg


def test_market_ok():
    o = validate_spot_order(SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"}, REF)
    assert o.qty == Decimal("0.001") and o.type == "MARKET"


def test_lot_size_step():
    code, msg = rej(validate_spot_order, SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.000015"}, REF)
    assert (code, msg) == (-1013, "Filter failure: LOT_SIZE")


def test_lot_size_min():
    code, msg = rej(validate_spot_order, SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.000001"}, REF)
    assert code == -1013 and "LOT_SIZE" in msg


def test_precision_over_max():
    code, _ = rej(validate_spot_order, SPOT["ETHUSDT"], {"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.123456789"}, Decimal("2500"))
    assert code == -1111


def test_price_filter_tick():
    code, msg = rej(validate_spot_order, SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
                                                          "quantity": "0.001", "price": "79000.005"}, REF)
    assert (code, msg) == (-1013, "Filter failure: PRICE_FILTER")


def test_notional_min():
    code, msg = rej(validate_spot_order, SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.00001"}, REF)
    assert (code, msg) == (-1013, "Filter failure: NOTIONAL")


def test_percent_price_by_side():
    code, msg = rej(validate_spot_order, SPOT["ETHUSDT"], {"symbol": "ETHUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC",
                                                          "quantity": "0.01", "price": "1000.00"}, Decimal("2500"))
    assert (code, msg) == (-1013, "Filter failure: PERCENT_PRICE_BY_SIDE")


def test_invalid_symbol():
    assert rej(validate_spot_order, None, {"symbol": "FOOBAR", "side": "BUY", "type": "MARKET", "quantity": "1"}, REF) == (-1121, "Invalid symbol.")


def test_missing_time_in_force():
    code, msg = rej(validate_spot_order, SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "0.001", "price": "79000"}, REF)
    assert code == -1102 and "timeInForce" in msg


def test_bad_side_and_type():
    assert rej(validate_spot_order, SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "LONG", "type": "MARKET", "quantity": "0.001"}, REF)[0] == -1117
    assert rej(validate_spot_order, SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MKT", "quantity": "0.001"}, REF)[0] == -1116


def test_quote_order_qty_market():
    o = validate_spot_order(SPOT["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "20"}, REF)
    assert o.quote_qty == Decimal("20") and o.qty is None


def test_usdm_ok_and_notional():
    o = validate_usdm_order(USDM["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.002"}, REF)
    assert o.qty == Decimal("0.002")
    code, msg = rej(validate_usdm_order, USDM["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"}, Decimal("50"))
    assert code == -4164 and "notional" in msg


def test_usdm_precision():
    code, _ = rej(validate_usdm_order, USDM["BTCUSDT"], {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0015"}, REF)
    assert code == -1111


def test_usdm_stop_market_requires_stop_price():
    code, msg = rej(validate_usdm_order, USDM["BTCUSDT"], {"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "quantity": "0.002"}, REF)
    assert code == -1102 and "stopPrice" in msg


def test_leverage_bounds():
    assert validate_leverage(USDM["BTCUSDT"], "BTCUSDT", "20", 125) == 20
    assert rej(validate_leverage, USDM["BTCUSDT"], "BTCUSDT", "500", 125)[0] == -4028
