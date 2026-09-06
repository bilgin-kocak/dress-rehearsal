from tests.conftest import D


def test_session_reset_keeps_history(engine):
    s1 = engine.start_session("run_s1", run_id="run", label="session 1", reset=True)
    engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"})
    engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.001", "price": "70000.00"})
    engine.end_session(s1)
    s2 = engine.start_session("run_s2", run_id="run", label="session 2", reset=True)
    # fresh trading state
    assert engine.ledger.balance("spot", "USDT") == (D(1000), D(0))
    assert engine.ledger.open_orders("spot") == []
    assert engine.ledger.balance("spot", "BTC") == (D(0), D(0))
    # history kept
    assert len(engine.ledger.sessions(run_id="run")) == 2
    assert len(engine.ledger.fills(session_id=s1)) == 1
    assert engine.ledger.all_orders("spot", session_id=s1)[0]["status"] == "EXPIRED"
    engine.end_session(s2)
    from rehearsal.rehearsal.report import build_report
    rep = build_report(engine.cfg, engine.ledger, "run")
    assert rep["summary"]["sessions"] == 2
    assert rep["sessions"][0]["behaviour"]["flattened_before_exit"] is False   # session 1 ended holding BTC
    assert rep["sessions"][1]["behaviour"]["flattened_before_exit"] is True
