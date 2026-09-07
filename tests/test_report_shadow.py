import json

from rehearsal.rehearsal.report import build_report, render_markdown
from rehearsal.server.catalog import ToolCatalog
from rehearsal.server.mcp_server import Twin
from rehearsal.shadow.divergence import calibrate, compare
from rehearsal.shadow.receiver import ShadowReceiver
from tests.conftest import D, push_mark, push_trade


def _twin(engine):
    return Twin(engine.cfg, engine, ToolCatalog(engine.cfg))


def test_report_from_sessions(engine, tmp_path):
    twin = _twin(engine)
    engine.cfg.runner.reports_dir = str(tmp_path)
    sid = engine.start_session("r1_s1", run_id="r1", label="session 1", reset=True,
                               meta={"confirmation": {"writes": 2, "restated": 1}, "turns": 5, "cost_usd": 0.1})
    twin.execute("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0012345678"}, sid)
    twin.execute("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0012345678"}, sid)
    twin.execute("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0012345678"}, sid)
    twin.execute("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.002"}, sid)
    twin.execute("spot.newOrder", {"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "timeInForce": "GTC", "quantity": "0.001", "price": "81000.00"}, sid)
    push_trade(engine, "spot", "BTCUSDT", "81000.00", "0.5")
    twin.execute("margin.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"}, sid)
    engine.end_session(sid)
    rep = build_report(engine.cfg, engine.ledger, "r1", None, twin.catalog.status(), feed=engine.feed)
    s = rep["summary"]
    assert s["sessions"] == 1
    assert s["rejected"] == 4 and s["rejections_by_code"]["PRECISION"] == 3 and s["rejections_by_code"]["TWIN_UNSUPPORTED"] == 1
    assert s["loops"] == 1
    assert s["limit_orders"] == 1 and s["limit_fill_rate"] == 1.0
    assert s["confirmation_compliance"] == 0.5
    assert rep["gate"]["passed"] is False
    names = {c["name"] for c in rep["gate"]["criteria"] if not c["ok"]}
    assert {"min_sessions", "max_rejection_rate", "min_confirmation_compliance"} <= names
    md = render_markdown(rep)
    assert "precision rejections on BTCUSDT" in md and "retry loop" in md
    assert "TWIN_UNSUPPORTED" in md


def test_shadow_receiver_mirrors_write_and_computes_divergence(engine):
    twin = _twin(engine)
    rx = ShadowReceiver(twin, engine.cfg)
    real = {"symbol": "BTCUSDT", "orderId": 7, "status": "FILLED", "executedQty": "0.00100000", "cummulativeQuoteQty": "80.10000000",
            "fills": [{"price": "80100.00000000", "qty": "0.00100000"}]}
    out = rx.handle({"tool_name": "mcp__binance-mcp-server__spot_newOrder",
                     "tool_input": {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"},
                     "tool_response": {"content": [{"type": "text", "text": json.dumps(real)}]}})
    d = out["divergence"]
    assert out["mirrored"] and d["status_match"] and d["sim_avg_price"] == "80001.00000000"
    assert round(d["avg_price_diff_bps"], 2) == round(float((D("80001") - D("80100")) / D("80100") * 10000), 2)
    st = rx.status()
    assert st["active"] and len(st["divergences"]) == 1
    assert st["calibration"]["samples"] == 1 and "suggested_latency_mean_ms" in st["calibration"]
    # the paper ledger really executed the mirrored order
    assert engine.ledger.balance("spot", "BTC")[0] > 0


def test_shadow_balance_sync_on_first_read(engine):
    twin = _twin(engine)
    rx = ShadowReceiver(twin, engine.cfg)
    real = {"accountType": "SPOT", "balances": [{"asset": "USDT", "free": "250.00000000", "locked": "0"}, {"asset": "ETH", "free": "0.1", "locked": "0"}]}
    rx.handle({"tool_name": "mcp__binance-mcp-server__spot_getAccount", "tool_input": {"omitZeroBalances": True},
               "tool_response": {"content": [{"type": "text", "text": json.dumps(real)}]}})
    assert rx.synced_balances
    assert engine.ledger.balance("spot", "USDT") == (D(250), D(0))
    assert engine.ledger.balance("spot", "ETH") == (D("0.1"), D(0))
    assert engine.ledger.equity_curve(source="live")


def test_compare_error_shapes():
    d = compare("spot.newOrder", {"symbol": "X"}, {"code": -1121, "msg": "Invalid symbol."}, {"code": -1121, "msg": "Invalid symbol."}, 3)
    assert d["status_match"] and d["real_status"] == "ERROR -1121"
    cal = calibrate([{"avg_price_diff_bps": -2.0, "status_match": True}, {"avg_price_diff_bps": -1.0, "status_match": True}], None or __import__("rehearsal.config", fromlist=["Config"]).Config())
    assert cal["mean_avg_price_diff_bps"] == -1.5 and cal["suggested_latency_mean_ms"] > 80


def test_sell_by_quote_order_qty(engine):
    engine.spot.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.004"})
    r = engine.spot.place({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quoteOrderQty": "100"})
    assert r["status"] == "FILLED"
    assert D(r["cummulativeQuoteQty"]) >= D(100) and D(r["executedQty"]) > 0
    assert engine.ledger.balance("spot", "BTC")[1] == D(0)  # nothing left locked


async def test_numeric_params_below_0_001_are_rejected_like_the_real_gateway(engine):
    from mcp import Client
    from mcp.shared.exceptions import MCPError
    import pytest as _pytest
    twin = _twin(engine)
    async with Client(twin.make_server()) as client:
        with _pytest.raises(MCPError) as ei:
            await client.call_tool("spot.newOrder", {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": 0.0001})
        assert ei.value.error.message.startswith('{"code":-1100,"msg":"Illegal characters found in parameter \'quantity\'')
        # strings are fine (that is what the twin's own docs recommend)
        r = await client.call_tool("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"})
        assert json.loads(r.content[0].text)["status"] == "FILLED"


def test_shadow_maps_live_ids_to_twin_ids(engine):
    twin = _twin(engine)
    rx = ShadowReceiver(twin, engine.cfg)
    # live quote request -> twin issues its own quoteId; the live accept must be translated
    rx.handle({"tool_name": "mcp__binance-mcp-server__convert_sendQuoteRequest",
               "tool_input": {"fromAsset": "USDT", "toAsset": "BTC", "fromAmount": 20},
               "tool_response": {"content": [{"type": "text", "text": json.dumps({"quoteId": "LIVE-Q-1", "ratio": "0.0000125", "toAmount": "0.00025", "fromAmount": "20"})}]}})
    assert "LIVE-Q-1" in rx.quote_map
    out = rx.handle({"tool_name": "mcp__binance-mcp-server__convert_acceptQuote", "tool_input": {"quoteId": "LIVE-Q-1"},
                     "tool_response": {"content": [{"type": "text", "text": json.dumps({"orderId": "77", "orderStatus": "PROCESS"})}]}})
    assert out["divergence"]["sim_status"] != "ERROR -4058"
    assert engine.ledger.balance("spot", "BTC")[0] > 0
