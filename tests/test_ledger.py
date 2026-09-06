from decimal import Decimal

import pytest

from rehearsal.engine.ledger import InsufficientBalance
from rehearsal.server.errors import BinanceError
from tests.conftest import D


def test_reset_sets_initial_balances(engine):
    assert engine.ledger.balance("spot", "USDT") == (D(1000), D(0))
    assert engine.ledger.balance("usdm", "USDT") == (D(0), D(0))


def test_lock_unlock_conserves_total(engine):
    led = engine.ledger
    led.lock_funds("spot", "USDT", D(100), "T")
    assert led.balance("spot", "USDT") == (D(900), D(100))
    assert led.total("spot", "USDT") == D(1000)
    led.unlock_funds("spot", "USDT", D(100), "T")
    assert led.balance("spot", "USDT") == (D(1000), D(0))
    with pytest.raises(InsufficientBalance):
        led.debit("spot", "USDT", D(5000), "T")


def test_place_cancel_conserves(engine):
    led = engine.ledger
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.001", "price": "70000.00"})
    assert r["status"] == "NEW"
    free, locked = led.balance("spot", "USDT")
    assert locked == D("70") and free == D("930")
    engine.spot.cancel("BTCUSDT", r["orderId"])
    assert led.balance("spot", "USDT") == (D(1000), D(0))
    assert led.get_order("spot", r["orderId"])["status"] == "CANCELED"


def test_transfer_between_wallets(engine):
    out = engine.transfer("MAIN_UMFUTURE", "USDT", "250")
    assert out["tranId"] >= 1
    assert engine.ledger.balance("spot", "USDT") == (D(750), D(0))
    assert engine.ledger.balance("usdm", "USDT") == (D(250), D(0))
    with pytest.raises(BinanceError) as ei:
        engine.transfer("UMFUTURE_MAIN", "USDT", "1000")
    assert ei.value.code == -3020
    with pytest.raises(BinanceError):
        engine.transfer("MAIN_UMFUTURE", "BTC", "1")


def test_reset_clears_state(engine):
    engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"})
    assert engine.ledger.balances("spot", omit_zero=True)[0]["asset"] == "BTC"
    engine.reset()
    assert engine.ledger.balances("spot", omit_zero=True) == [{"asset": "USDT", "free": "1000.00000000", "locked": "0.00000000"}]
    assert engine.ledger.fills() == []
