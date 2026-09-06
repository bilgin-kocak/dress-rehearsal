import json

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError as McpError

from rehearsal.server.catalog import ToolCatalog, normalize, underscored
from rehearsal.server.mcp_server import Twin


@pytest.fixture
def twin(engine):
    cfg = engine.cfg
    catalog = ToolCatalog(cfg)
    return Twin(cfg, engine, catalog)


def text(r):
    return json.loads(r.content[0].text)


async def test_list_tools_matches_schema_file_verbatim(twin):
    expected = json.loads(twin.catalog.tools_path.read_text())["tools"]
    async with Client(twin.make_server()) as client:
        res = await client.list_tools()
    served = {t.name: t.input_schema for t in res.tools}
    assert set(served) == {t["name"] for t in expected}
    for t in expected:
        assert served[t["name"]] == t["inputSchema"]


async def test_initialize_mirrors_real_server(twin):
    init = twin.catalog.init
    async with Client(twin.make_server()) as client:
        if init.get("server_info"):
            assert client.server_info.name == init["server_info"]["name"]
        if init.get("instructions"):
            assert client.instructions == init["instructions"]


async def test_read_and_write_roundtrip(twin):
    async with Client(twin.make_server()) as client:
        r = await client.call_tool("spot.tickerPrice", {"symbol": "BTCUSDT"})
        assert not r.is_error and text(r) == {"symbol": "BTCUSDT", "price": "80000.50000000"}
        r = await client.call_tool("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.001"})
        body = text(r)
        assert not r.is_error and body["status"] == "FILLED" and body["fills"][0]["price"] == "80001.00000000"
        assert r.structured_content["orderId"] == body["orderId"]
        r = await client.call_tool("spot.getAccount", {"omitZeroBalances": True})
        assert {b["asset"] for b in text(r)["balances"]} == {"BTC", "USDT"}


async def test_error_shape_matches_real_server(twin):
    """Real server (2026-09-06): JSON-RPC error -32603 whose message is the raw Binance JSON."""
    async with Client(twin.make_server()) as client:
        with pytest.raises(McpError) as ei:
            await client.call_tool("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0000001"})
        assert ei.value.code == -32603
        assert ei.value.message == '{"code":-1013,"msg":"Filter failure: LOT_SIZE"}'
        with pytest.raises(McpError) as ei:
            await client.call_tool("margin.marginAccountNewOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "1"})
        assert "TWIN_UNSUPPORTED" in ei.value.message
        with pytest.raises(McpError) as ei:
            await client.call_tool("tool_execute", {"toolName": "spot.doesNotExist", "arguments": {}})
        assert ei.value.message.startswith("Tool not found: 'spot.doesNotExist'")


async def test_error_shape_result_style(engine):
    engine.cfg.server.error_style = "result"
    twin = _twin(engine)
    async with Client(twin.make_server()) as client:
        r = await client.call_tool("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.0000001"})
        assert r.is_error and text(r) == {"code": -1013, "msg": "Filter failure: LOT_SIZE"}


def _twin(engine):
    from rehearsal.server.catalog import ToolCatalog
    return Twin(engine.cfg, engine, ToolCatalog(engine.cfg))


async def test_tool_execute_and_underscore_names(twin):
    async with Client(twin.make_server()) as client:
        r = await client.call_tool("tool_execute", {"toolName": "spot.depth", "arguments": {"symbol": "BTCUSDT", "limit": 1}})
        assert text(r)["bids"] == [["80000.00", "0.50000000"]]
        r = await client.call_tool("tool_search", {"category": "trade"})
        assert any(t["name"] == "spot.newOrder" for t in text(r)["tools"]) and "nextCursor" in text(r)
        if twin.catalog.catalog:  # hidden catalog tools are reachable through tool_execute
            r = await client.call_tool("tool_execute", {"toolName": "futures_usds.markPrice", "arguments": {"symbol": "BTCUSDT"}})
            assert text(r)["symbol"] == "BTCUSDT" and "lastFundingRate" in text(r)
    # Claude Code shows dots as underscores; the twin resolves both.
    assert twin.catalog.resolve("spot_newOrder").name == "spot.newOrder"
    assert normalize("futures_usds_newOrder") == "futures_usds.newOrder"
    assert underscored("futures_usds.newOrder") == "futures_usds_newOrder"


async def test_calls_are_logged_with_session(twin):
    twin.engine.start_session("s1", reset=True)
    async with Client(twin.make_server()) as client:
        await client.call_tool("spot.tickerPrice", {"symbol": "BTCUSDT"})
        with pytest.raises(McpError):
            await client.call_tool("spot.newOrder", {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.000001"})
    calls = twin.engine.ledger.tool_calls(session_id="s1")
    assert [c["tool"] for c in calls] == ["spot.newOrder", "spot.tickerPrice"]
    assert calls[0]["error_code"] == -1013 and calls[0]["result_status"] == "error"
    assert calls[0]["mid_at_arrival"] is not None
