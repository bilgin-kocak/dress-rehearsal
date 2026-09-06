#!/usr/bin/env python3
"""Generate schemas/fallback_tools.json.

Used only when schemas/tools.json (dumped from the real server with `rehearsal schema dump`) is
missing. Tool names follow the real server's `<product>.<method>` convention observed on
2026-09-03..05 by other Agent OS clients (spot.newOrder, spot.depth, futures_usds.symbolPriceTicker,
tool_search, tool_execute). Parameter names are the Binance REST parameter names.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}
B = {"type": "boolean"}


def s(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "string", "description": desc, **kw}


def i(desc: str, **kw: Any) -> dict[str, Any]:
    return {"type": "integer", "description": desc, **kw}


def b(desc: str) -> dict[str, Any]:
    return {"type": "boolean", "description": desc}


def num(desc: str) -> dict[str, Any]:
    return {"type": ["number", "string"], "description": desc}


def arr(desc: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def tool(name: str, description: str, props: dict[str, Any], required: list[str] | None = None,
         read_only: bool = True, title: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    schema["additionalProperties"] = False
    t: dict[str, Any] = {"name": name, "description": description, "inputSchema": schema}
    if title:
        t["title"] = title
    t["annotations"] = {"readOnlyHint": read_only, "destructiveHint": not read_only, "openWorldHint": True}
    return t


SYMBOL = s("Trading pair symbol, e.g. BTCUSDT")
RECV = i("recvWindow in milliseconds (max 60000)")
LIMIT = i("Number of rows to return")
START = i("Start time in milliseconds since epoch")
END = i("End time in milliseconds since epoch")

TOOLS: list[dict[str, Any]] = [
    # ---------------------------------------------------------------- meta
    tool("tool_search",
         "Search the Binance tool catalog by category. Returns tool names, descriptions and input schemas that can be "
         "run with tool_execute. Categories: market, account, trade, transfer, convert, futures, margin, wallet, analysis.",
         {"category": s("Tool category to list", enum=["market", "account", "trade", "transfer", "convert", "futures",
                                                       "margin", "wallet", "analysis"]),
          "query": s("Optional free-text filter on tool name / description"),
          "cursor": s("Pagination cursor from a previous call")},
         ["category"]),
    tool("tool_execute",
         "Execute a tool from the Binance catalog by name with the given arguments (e.g. toolName=spot.ticker24hr, "
         "arguments={symbol: 'BTCUSDT'}). Write tools (orders, transfers) require the user's explicit confirmation first.",
         {"toolName": s("Catalog tool name, e.g. spot.newOrder"),
          "arguments": {"type": "object", "description": "Arguments for the tool", "additionalProperties": True}},
         ["toolName"], read_only=False),

    # ---------------------------------------------------------------- spot market data
    tool("spot.ticker24hr", "24hr rolling window price change statistics for a Spot symbol (or all symbols).",
         {"symbol": SYMBOL, "symbols": arr("List of symbols"), "type": s("FULL or MINI", enum=["FULL", "MINI"])}),
    tool("spot.tickerPrice", "Latest price for a Spot symbol or symbols.", {"symbol": SYMBOL, "symbols": arr("List of symbols")}),
    tool("spot.ticker", "Rolling window price change statistics (windowSize e.g. 1m, 1h, 1d).",
         {"symbol": SYMBOL, "windowSize": s("Window size, e.g. 1h"), "type": s("FULL or MINI", enum=["FULL", "MINI"])}, ["symbol"]),
    tool("spot.bookTicker", "Best bid/ask price and quantity for a Spot symbol.", {"symbol": SYMBOL, "symbols": arr("List of symbols")}),
    tool("spot.depth", "Spot order book (bids and asks).", {"symbol": SYMBOL, "limit": i("Depth levels: 5,10,20,50,100,500,1000,5000")}, ["symbol"]),
    tool("spot.klines", "Spot kline/candlestick bars for a symbol and interval (1m,5m,15m,1h,4h,1d,...).",
         {"symbol": SYMBOL, "interval": s("Kline interval, e.g. 1h"), "startTime": START, "endTime": END,
          "timeZone": s("Time zone offset, default 0 (UTC)"), "limit": LIMIT}, ["symbol", "interval"]),
    tool("spot.uiKlines", "Spot klines optimised for presentation (same parameters as spot.klines).",
         {"symbol": SYMBOL, "interval": s("Kline interval"), "startTime": START, "endTime": END, "timeZone": S, "limit": LIMIT},
         ["symbol", "interval"]),
    tool("spot.aggTrades", "Compressed/aggregate trades for a Spot symbol.",
         {"symbol": SYMBOL, "fromId": i("Aggregate trade id to fetch from"), "startTime": START, "endTime": END, "limit": LIMIT}, ["symbol"]),
    tool("spot.avgPrice", "Current average price for a Spot symbol.", {"symbol": SYMBOL}, ["symbol"]),
    tool("spot.exchangeInfo", "Spot exchange information: symbols, status, order types and trading filters (PRICE_FILTER, LOT_SIZE, NOTIONAL).",
         {"symbol": SYMBOL, "symbols": arr("List of symbols"), "permissions": arr("Permission filter, e.g. SPOT"),
          "showPermissionSets": b("Include permissionSets"), "symbolStatus": s("Filter by status, e.g. TRADING")}),

    # ---------------------------------------------------------------- spot account / trade
    tool("spot.getAccount", "Spot account information for the Agentic sub-account: balances, commission rates, permissions.",
         {"omitZeroBalances": b("Omit zero balances"), "recvWindow": RECV}),
    tool("spot.newOrder",
         "Place a new Spot order. Requires the user's explicit confirmation of symbol, side, type, quantity and price before "
         "calling. Types: LIMIT (timeInForce, quantity, price), MARKET (quantity or quoteOrderQty), STOP_LOSS, STOP_LOSS_LIMIT, "
         "TAKE_PROFIT, TAKE_PROFIT_LIMIT, LIMIT_MAKER. Quantity must respect LOT_SIZE stepSize and NOTIONAL minNotional.",
         {"symbol": SYMBOL, "side": s("BUY or SELL", enum=["BUY", "SELL"]),
          "type": s("Order type", enum=["LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "LIMIT_MAKER"]),
          "timeInForce": s("GTC, IOC or FOK", enum=["GTC", "IOC", "FOK"]), "quantity": num("Order quantity in base asset"),
          "quoteOrderQty": num("MARKET orders: amount of quote asset to spend/receive"), "price": num("Limit price"),
          "newClientOrderId": s("Unique id for the order (max 36 chars); auto-generated if not sent"),
          "strategyId": I, "strategyType": I, "stopPrice": num("Trigger price for STOP_LOSS/TAKE_PROFIT orders"),
          "trailingDelta": I, "icebergQty": num("Iceberg quantity"),
          "newOrderRespType": s("ACK, RESULT or FULL", enum=["ACK", "RESULT", "FULL"]),
          "selfTradePreventionMode": s("EXPIRE_TAKER, EXPIRE_MAKER, EXPIRE_BOTH, NONE"), "recvWindow": RECV},
         ["symbol", "side", "type"], read_only=False),
    tool("spot.newOrderTest", "Test a new Spot order without sending it to the matching engine (validates parameters and filters).",
         {"symbol": SYMBOL, "side": s("BUY or SELL", enum=["BUY", "SELL"]), "type": s("Order type"), "timeInForce": S,
          "quantity": num("Quantity"), "quoteOrderQty": num("Quote quantity"), "price": num("Price"), "newClientOrderId": S,
          "stopPrice": num("Stop price"), "computeCommissionRates": b("Return commission rates"), "recvWindow": RECV},
         ["symbol", "side", "type"]),
    tool("spot.deleteOrder", "Cancel an active Spot order by orderId or origClientOrderId. Requires user confirmation.",
         {"symbol": SYMBOL, "orderId": i("Order id"), "origClientOrderId": s("Client order id"), "newClientOrderId": S,
          "cancelRestrictions": s("ONLY_NEW or ONLY_PARTIALLY_FILLED"), "recvWindow": RECV}, ["symbol"], read_only=False),
    tool("spot.deleteOpenOrders", "Cancel all open Spot orders on a symbol. Requires user confirmation.",
         {"symbol": SYMBOL, "recvWindow": RECV}, ["symbol"], read_only=False),
    tool("spot.getOrder", "Query a Spot order's status by orderId or origClientOrderId.",
         {"symbol": SYMBOL, "orderId": i("Order id"), "origClientOrderId": s("Client order id"), "recvWindow": RECV}, ["symbol"]),
    tool("spot.getOpenOrders", "All open Spot orders (optionally for one symbol).", {"symbol": SYMBOL, "recvWindow": RECV}),
    tool("spot.allOrders", "All Spot orders for a symbol: active, canceled or filled.",
         {"symbol": SYMBOL, "orderId": i("Return orders >= this id"), "startTime": START, "endTime": END, "limit": LIMIT, "recvWindow": RECV}, ["symbol"]),
    tool("spot.myTrades", "Spot trades (fills) for the account and symbol.",
         {"symbol": SYMBOL, "orderId": i("Filter by order id"), "startTime": START, "endTime": END, "fromId": i("Trade id to fetch from"),
          "limit": LIMIT, "recvWindow": RECV}, ["symbol"]),
    tool("spot.rateLimitOrder", "Current unfilled order count and order rate limits.", {"recvWindow": RECV}),

    # ---------------------------------------------------------------- USDⓈ-M futures
    tool("futures_usds.exchangeInfo", "USDⓈ-M Futures exchange information: symbols, precision, filters, leverage brackets.", {}),
    tool("futures_usds.depth", "USDⓈ-M Futures order book.", {"symbol": SYMBOL, "limit": i("5, 10, 20, 50, 100, 500, 1000")}, ["symbol"]),
    tool("futures_usds.klines", "USDⓈ-M Futures klines/candlesticks.",
         {"symbol": SYMBOL, "interval": s("Interval, e.g. 1h"), "startTime": START, "endTime": END, "limit": LIMIT}, ["symbol", "interval"]),
    tool("futures_usds.symbolPriceTicker", "Latest USDⓈ-M Futures price for a symbol (or all).", {"symbol": SYMBOL}),
    tool("futures_usds.ticker24hr", "USDⓈ-M Futures 24hr price change statistics.", {"symbol": SYMBOL}),
    tool("futures_usds.bookTicker", "USDⓈ-M Futures best bid/ask.", {"symbol": SYMBOL}),
    tool("futures_usds.markPrice", "USDⓈ-M Futures mark price, index price, funding rate and next funding time (premium index).", {"symbol": SYMBOL}),
    tool("futures_usds.fundingRate", "USDⓈ-M Futures funding rate history.",
         {"symbol": SYMBOL, "startTime": START, "endTime": END, "limit": LIMIT}),
    tool("futures_usds.openInterest", "Present open interest of a USDⓈ-M Futures symbol.", {"symbol": SYMBOL}, ["symbol"]),
    tool("futures_usds.account", "USDⓈ-M Futures account: balances, margin, unrealized PnL and positions.", {"recvWindow": RECV}),
    tool("futures_usds.balance", "USDⓈ-M Futures wallet balances.", {"recvWindow": RECV}),
    tool("futures_usds.positionRisk", "USDⓈ-M Futures position information: size, entry, mark, liquidation price, leverage, margin.",
         {"symbol": SYMBOL, "recvWindow": RECV}),
    tool("futures_usds.changeLeverage", "Change initial leverage for a USDⓈ-M Futures symbol (1-125). Requires user confirmation.",
         {"symbol": SYMBOL, "leverage": i("Target leverage"), "recvWindow": RECV}, ["symbol", "leverage"], read_only=False),
    tool("futures_usds.changeMarginType", "Change margin type (ISOLATED / CROSSED) for a USDⓈ-M Futures symbol.",
         {"symbol": SYMBOL, "marginType": s("ISOLATED or CROSSED", enum=["ISOLATED", "CROSSED"]), "recvWindow": RECV},
         ["symbol", "marginType"], read_only=False),
    tool("futures_usds.newOrder",
         "Place a USDⓈ-M Futures order. Requires the user's explicit confirmation of symbol, side, type, quantity, price and "
         "leverage before calling. Types: LIMIT, MARKET, STOP, STOP_MARKET, TAKE_PROFIT, TAKE_PROFIT_MARKET, TRAILING_STOP_MARKET.",
         {"symbol": SYMBOL, "side": s("BUY or SELL", enum=["BUY", "SELL"]), "positionSide": s("BOTH (one-way), LONG or SHORT (hedge)"),
          "type": s("Order type", enum=["LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"]),
          "timeInForce": s("GTC, IOC, FOK, GTX or GTD"), "quantity": num("Contract quantity in base asset"),
          "reduceOnly": s("true or false", enum=["true", "false"]), "price": num("Limit price"),
          "newClientOrderId": s("Unique id (max 36 chars)"), "stopPrice": num("Trigger price for STOP/TAKE_PROFIT orders"),
          "closePosition": s("true to close the whole position (STOP_MARKET/TAKE_PROFIT_MARKET)", enum=["true", "false"]),
          "activationPrice": num("TRAILING_STOP_MARKET activation price"), "callbackRate": num("TRAILING_STOP_MARKET callback rate"),
          "workingType": s("CONTRACT_PRICE or MARK_PRICE", enum=["CONTRACT_PRICE", "MARK_PRICE"]),
          "priceProtect": s("true or false"), "newOrderRespType": s("ACK or RESULT", enum=["ACK", "RESULT"]),
          "priceMatch": S, "selfTradePreventionMode": S, "goodTillDate": I, "recvWindow": RECV},
         ["symbol", "side", "type"], read_only=False),
    tool("futures_usds.cancelOrder", "Cancel an active USDⓈ-M Futures order. Requires user confirmation.",
         {"symbol": SYMBOL, "orderId": i("Order id"), "origClientOrderId": s("Client order id"), "recvWindow": RECV}, ["symbol"], read_only=False),
    tool("futures_usds.cancelAllOpenOrders", "Cancel all open USDⓈ-M Futures orders on a symbol. Requires user confirmation.",
         {"symbol": SYMBOL, "recvWindow": RECV}, ["symbol"], read_only=False),
    tool("futures_usds.getOrder", "Query a USDⓈ-M Futures order.",
         {"symbol": SYMBOL, "orderId": i("Order id"), "origClientOrderId": s("Client order id"), "recvWindow": RECV}, ["symbol"]),
    tool("futures_usds.openOrders", "Current open USDⓈ-M Futures orders.", {"symbol": SYMBOL, "recvWindow": RECV}),
    tool("futures_usds.allOrders", "All USDⓈ-M Futures orders for a symbol.",
         {"symbol": SYMBOL, "orderId": I, "startTime": START, "endTime": END, "limit": LIMIT, "recvWindow": RECV}, ["symbol"]),
    tool("futures_usds.userTrades", "USDⓈ-M Futures account trade list (fills) for a symbol.",
         {"symbol": SYMBOL, "orderId": I, "startTime": START, "endTime": END, "fromId": I, "limit": LIMIT, "recvWindow": RECV}, ["symbol"]),
    tool("futures_usds.income", "USDⓈ-M Futures income history: REALIZED_PNL, FUNDING_FEE, COMMISSION, TRANSFER, ...",
         {"symbol": SYMBOL, "incomeType": s("Income type filter"), "startTime": START, "endTime": END, "page": I, "limit": LIMIT, "recvWindow": RECV}),

    # ---------------------------------------------------------------- wallet / transfer
    tool("wallet.accountStatus", "Account status of the Agentic sub-account.", {"recvWindow": RECV}),
    tool("wallet.allCoinsInformation", "Information of all coins available: networks, deposit/withdraw status, fees.", {"recvWindow": RECV}),
    tool("wallet.queryUserWalletBalance", "Balance of every wallet (Spot, Futures, Margin, Earn, Funding) in BTC or the quoteAsset.",
         {"quoteAsset": s("Quote asset for valuation, default BTC"), "recvWindow": RECV}),
    tool("wallet.userUniversalTransfer",
         "Transfer funds between wallets inside the Agentic sub-account, e.g. type=MAIN_UMFUTURE moves from Spot to USDⓈ-M "
         "Futures, UMFUTURE_MAIN moves back. Withdrawals to external addresses are not possible. Requires user confirmation.",
         {"type": s("Transfer type", enum=["MAIN_UMFUTURE", "UMFUTURE_MAIN", "MAIN_CMFUTURE", "CMFUTURE_MAIN", "MAIN_MARGIN", "MARGIN_MAIN"]),
          "asset": s("Asset, e.g. USDT"), "amount": num("Amount to transfer"), "fromSymbol": S, "toSymbol": S, "recvWindow": RECV},
         ["type", "asset", "amount"], read_only=False),
    tool("wallet.queryUserUniversalTransferHistory", "History of universal transfers.",
         {"type": s("Transfer type"), "startTime": START, "endTime": END, "current": i("Page"), "size": i("Page size"), "recvWindow": RECV}, ["type"]),
    tool("wallet.dailyAccountSnapshot", "Daily account snapshot (SPOT, MARGIN or FUTURES).",
         {"type": s("SPOT, MARGIN or FUTURES", enum=["SPOT", "MARGIN", "FUTURES"]), "startTime": START, "endTime": END, "limit": LIMIT, "recvWindow": RECV}, ["type"]),
    tool("wallet.getApiKeyPermission", "Permissions granted to the current authorization.", {"recvWindow": RECV}),

    # ---------------------------------------------------------------- convert
    tool("convert.exchangeInfo", "Convert pairs and limits.", {"fromAsset": s("From asset"), "toAsset": s("To asset")}),
    tool("convert.getQuote", "Request a Convert quote (valid for validTime, default 10s). Accept it with convert.acceptQuote.",
         {"fromAsset": s("From asset"), "toAsset": s("To asset"), "fromAmount": num("Amount of fromAsset"),
          "toAmount": num("Amount of toAsset"), "walletType": s("SPOT or FUNDING"), "validTime": s("10s, 30s, 1m or 2m"), "recvWindow": RECV},
         ["fromAsset", "toAsset"]),
    tool("convert.acceptQuote", "Accept a Convert quote and execute the conversion. Requires user confirmation.",
         {"quoteId": s("Quote id from convert.getQuote"), "recvWindow": RECV}, ["quoteId"], read_only=False),
    tool("convert.orderStatus", "Status of a Convert order.", {"orderId": s("Order id"), "quoteId": s("Quote id"), "recvWindow": RECV}),
    tool("convert.tradeFlow", "Convert trade history.", {"startTime": START, "endTime": END, "limit": LIMIT, "recvWindow": RECV}, ["startTime", "endTime"]),

    # ---------------------------------------------------------------- margin / sub-account / analysis
    tool("margin.account", "Cross Margin account details.", {"recvWindow": RECV}),
    tool("margin.newOrder", "Place a Margin order. Requires user confirmation.",
         {"symbol": SYMBOL, "side": s("BUY or SELL"), "type": s("Order type"), "quantity": num("Quantity"), "price": num("Price"),
          "timeInForce": S, "isIsolated": s("TRUE or FALSE"), "sideEffectType": S, "recvWindow": RECV}, ["symbol", "side", "type"], read_only=False),
    tool("sub_account.getMainAccountAsset", "Read-only view of the main account assets (only if the user granted it).", {"recvWindow": RECV}),
    tool("analysis.getTokenAiReport", "AI-generated research report for a token.", {"symbol": s("Token symbol, e.g. BTC")}, ["symbol"]),
]


def main() -> None:
    out = {
        "_meta": {
            "source": "fallback",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "Hand-written fallback. Replace with schemas/tools.json dumped from the real server (rehearsal schema dump).",
            "server": "binance-mcp-server",
            "endpoint": "https://agent.binance.com/mcp/agentic",
        },
        "tools": TOOLS,
    }
    path = Path(__file__).resolve().parent.parent / "schemas" / "fallback_tools.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path} with {len(TOOLS)} tools")


if __name__ == "__main__":
    main()
