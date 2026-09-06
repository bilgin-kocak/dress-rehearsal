"""Category handlers. Each handler: (ctx, args) -> JSON-serialisable result, or raises BinanceError."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from rehearsal.engine.engine import Engine
from rehearsal.engine.ledger import dec, dstr
from rehearsal.server import errors as E
from rehearsal.server.catalog import ToolCatalog, ToolSpec

D = Decimal
ZERO = D(0)


@dataclass
class ToolContext:
    engine: Engine
    catalog: ToolCatalog
    session_id: str | None
    source: str = "agent"
    # Set by the server so tool_execute can recurse through the same gate.
    dispatch: Callable[["ToolContext", str, dict[str, Any]], Any] | None = None


HANDLERS: dict[str, Callable[[ToolContext, dict[str, Any]], Any]] = {}


def handler(name: str):
    def deco(fn):
        HANDLERS[name] = fn
        return fn
    return deco


def _sym(args: dict[str, Any]) -> str | None:
    s = args.get("symbol")
    return str(s).upper() if s else None


def _int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(str(v))
    except ValueError:
        raise E.err(E.BAD_PARAM, "orderId")


def _limit(args: dict[str, Any], default: int = 500, cap: int = 1000) -> int:
    try:
        return min(int(args.get("limit") or default), cap)
    except ValueError:
        return default


def _require_symbol(ctx: ToolContext, market: str, args: dict[str, Any]) -> str:
    s = _sym(args)
    if not s:
        raise E.mandatory("symbol")
    if not ctx.engine.feed.symbol_info(market, s):
        raise E.err(E.BAD_SYMBOL)
    return s


# ====================================================================== meta
@handler("tool_search")
def tool_search(ctx: ToolContext, args: dict[str, Any]) -> Any:
    category = args.get("category")
    if not category:
        raise E.mandatory("category")
    tools = ctx.catalog.search(str(category), args.get("query"))
    return {"category": category, "count": len(tools), "tools": tools, "nextCursor": None,
            "hint": "Run any of these with tool_execute(toolName, arguments)."}


@handler("tool_execute")
def tool_execute(ctx: ToolContext, args: dict[str, Any]) -> Any:
    name = args.get("toolName") or args.get("name") or args.get("tool")
    if not name:
        raise E.mandatory("toolName")
    inner = args.get("arguments") or args.get("args") or {}
    if not isinstance(inner, dict):
        raise E.err(E.BAD_PARAM, "arguments")
    if ctx.dispatch is None:
        raise E.err(E.TWIN_UNSUPPORTED, "tool_execute")
    return ctx.dispatch(ctx, str(name), inner)


@handler("unsupported")
def unsupported(ctx: ToolContext, args: dict[str, Any]) -> Any:
    raise E.err(E.TWIN_UNSUPPORTED, args.get("_tool", "tool"))


@handler("passthrough_public")
def passthrough_public(ctx: ToolContext, args: dict[str, Any]) -> Any:
    """Unmapped market-data-looking tool: best-effort routing to the public API."""
    name = str(args.get("_tool", "")).lower()
    market = "usdm" if "futures" in name else "spot"
    s = _sym(args)
    if "depth" in name or "orderbook" in name:
        return _depth(ctx, market, args)
    if "kline" in name or "candle" in name:
        return ctx.engine.feed.passthrough(f"{market}_klines", symbol=s, interval=args.get("interval") or "1h", limit=_limit(args, 100, 1000))
    if "24hr" in name:
        return ctx.engine.feed.passthrough(f"{market}_ticker_24hr", symbol=s)
    if "price" in name or "ticker" in name:
        return _ticker_price(ctx, market, args)
    if "exchangeinfo" in name:
        return ctx.engine.feed.exchange_info(market, s)
    raise E.err(E.TWIN_UNSUPPORTED, args.get("_tool", "tool"))


# ====================================================================== market data (shared)
def _depth(ctx: ToolContext, market: str, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, market, args)
    limit = _limit(args, 100, 1000)
    d = ctx.engine.feed.depth(market, s, limit)
    out: dict[str, Any] = {"lastUpdateId": d["lastUpdateId"], "bids": d["bids"], "asks": d["asks"]}
    if market == "usdm":
        out = {"lastUpdateId": d["lastUpdateId"], "E": d["ts"], "T": d["ts"], "bids": d["bids"], "asks": d["asks"]}
    return out


def _ticker_price(ctx: ToolContext, market: str, args: dict[str, Any]) -> Any:
    feed = ctx.engine.feed
    s = _sym(args)
    symbols = args.get("symbols")
    if isinstance(symbols, str):
        symbols = [x.strip().strip('"') for x in symbols.strip("[]").split(",") if x.strip()]
    if s:
        if not feed.symbol_info(market, s):
            raise E.err(E.BAD_SYMBOL)
        p = feed.last_price(market, s)
        if p is None:
            raise E.err(E.TWIN_NO_MARKET_DATA, s)
        out = {"symbol": s, "price": dstr(p)}
        if market == "usdm":
            out["time"] = feed.clock.now_ms()
        return out
    if symbols:
        return [_ticker_price(ctx, market, {"symbol": x}) for x in symbols]
    if feed.mode == "live":
        return feed.passthrough(f"{market}_ticker_price")
    return [_ticker_price(ctx, market, {"symbol": x["symbol"]}) for x in feed.exchange_info(market).get("symbols", [])]


def _book_ticker(ctx: ToolContext, market: str, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if not s:
        if ctx.engine.feed.mode == "live":
            return ctx.engine.feed.passthrough(f"{market}_book_ticker")
        raise E.mandatory("symbol")
    s = _require_symbol(ctx, market, args)
    bt = ctx.engine.feed.book_ticker(market, s)
    if not bt:
        raise E.err(E.TWIN_NO_MARKET_DATA, s)
    if market == "usdm":
        bt = bt | {"time": ctx.engine.feed.clock.now_ms()}
    return bt


# ---- spot
@handler("spot_ticker24hr")
def spot_ticker24hr(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if s and not ctx.engine.feed.symbol_info("spot", s):
        raise E.err(E.BAD_SYMBOL)
    symbols = args.get("symbols")
    if isinstance(symbols, list):
        return [ctx.engine.feed.passthrough("spot_ticker_24hr", symbol=str(x).upper(), type_=args.get("type")) for x in symbols]
    if not s and ctx.engine.feed.mode != "live":
        raise E.mandatory("symbol")
    return ctx.engine.feed.passthrough("spot_ticker_24hr", symbol=s, type_=args.get("type"))


@handler("spot_ticker_price")
def spot_ticker_price(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _ticker_price(ctx, "spot", args)


@handler("spot_rolling_ticker")
def spot_rolling_ticker(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    return ctx.engine.feed.passthrough("spot_rolling_ticker", symbol=s, window=args.get("windowSize"), type_=args.get("type"))


@handler("spot_book_ticker")
def spot_book_ticker(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _book_ticker(ctx, "spot", args)


@handler("spot_depth")
def spot_depth(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _depth(ctx, "spot", args)


@handler("spot_klines")
def spot_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    if not args.get("interval"):
        raise E.mandatory("interval")
    return ctx.engine.feed.passthrough("spot_klines", symbol=s, interval=args["interval"], start=_int(args.get("startTime")),
                                       end=_int(args.get("endTime")), limit=_limit(args, 500, 1000))


@handler("spot_ui_klines")
def spot_ui_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    if not args.get("interval"):
        raise E.mandatory("interval")
    return ctx.engine.feed.passthrough("spot_klines", symbol=s, interval=args["interval"], start=_int(args.get("startTime")),
                                       end=_int(args.get("endTime")), limit=_limit(args, 500, 1000), ui=True)


@handler("spot_agg_trades")
def spot_agg_trades(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    return ctx.engine.feed.passthrough("spot_agg_trades", symbol=s, limit=_limit(args, 500, 1000), from_id=_int(args.get("fromId")),
                                       start=_int(args.get("startTime")), end=_int(args.get("endTime")))


@handler("spot_avg_price")
def spot_avg_price(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    if ctx.engine.feed.mode == "live":
        return ctx.engine.feed.passthrough("spot_avg_price", symbol=s)
    return {"mins": 5, "price": dstr(ctx.engine.feed.mid("spot", s) or ZERO), "closeTime": ctx.engine.now()}


@handler("spot_exchange_info")
def spot_exchange_info(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _exchange_info(ctx, "spot", args)


def _exchange_info(ctx: ToolContext, market: str, args: dict[str, Any]) -> Any:
    s = _sym(args)
    symbols = args.get("symbols")
    if isinstance(symbols, str):
        symbols = [x.strip().strip('"') for x in symbols.strip("[]").split(",") if x.strip()]
    if s and not ctx.engine.feed.symbol_info(market, s):
        raise E.err(E.BAD_SYMBOL)
    info = ctx.engine.feed.exchange_info(market, s, symbols)
    if not s and not symbols and len(info.get("symbols", [])) > 300:
        # Keep the payload LLM-sized: the real server also trims very large responses.
        trimmed = dict(info)
        trimmed["symbols"] = [x for x in info["symbols"] if x.get("status") == "TRADING" and x.get("quoteAsset") == "USDT"][:300]
        trimmed["_twin_note"] = "trimmed to 300 USDT TRADING symbols; pass symbol= for a specific one"
        return trimmed
    return info


# ---- spot account / trade
@handler("spot_get_account")
def spot_get_account(ctx: ToolContext, args: dict[str, Any]) -> Any:
    omit = str(args.get("omitZeroBalances", "false")).lower() == "true"
    return ctx.engine.spot.account_view(omit_zero=omit)


@handler("spot_new_order")
def spot_new_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.spot.place(args, source=ctx.source)


@handler("spot_new_order_test")
def spot_new_order_test(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from rehearsal.engine.filters import validate_spot_order
    s = _sym(args)
    sym = ctx.engine.feed.symbol_info("spot", s)
    mid = ctx.engine.feed.mid("spot", s) if sym else None
    validate_spot_order(sym, args, mid)
    if str(args.get("computeCommissionRates", "false")).lower() == "true":
        f = ctx.engine.fees
        return {"standardCommissionForOrder": {"maker": dstr(f.rate("spot", True)), "taker": dstr(f.rate("spot", False))},
                "taxCommissionForOrder": {"maker": "0.00000000", "taker": "0.00000000"},
                "discount": {"enabledForAccount": False, "enabledForSymbol": False, "discountAsset": "BNB", "discount": "0.75000000"}}
    return {}


@handler("spot_cancel_order")
def spot_cancel_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    return ctx.engine.spot.cancel(s, _int(args.get("orderId")), args.get("origClientOrderId"))


@handler("spot_cancel_all")
def spot_cancel_all(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if not s:
        raise E.mandatory("symbol")
    return ctx.engine.spot.cancel_all(s)


@handler("spot_get_order")
def spot_get_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    oid, coid = _int(args.get("orderId")), args.get("origClientOrderId")
    if oid is None and not coid:
        raise E.mandatory("orderId")
    row = ctx.engine.ledger.get_order("spot", oid, coid, s)
    if not row:
        raise E.err(E.NO_SUCH_ORDER)
    return ctx.engine.spot.order_view(row)


@handler("spot_open_orders")
def spot_open_orders(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if s and not ctx.engine.feed.symbol_info("spot", s):
        raise E.err(E.BAD_SYMBOL)
    return [ctx.engine.spot.order_view(r) for r in ctx.engine.ledger.open_orders("spot", s)]


@handler("spot_all_orders")
def spot_all_orders(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    return [ctx.engine.spot.order_view(r) for r in ctx.engine.ledger.all_orders("spot", s, _limit(args))]


@handler("spot_my_trades")
def spot_my_trades(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    return ctx.engine.spot.trades_view(s, _int(args.get("orderId")), _limit(args))


@handler("spot_rate_limit_order")
def spot_rate_limit_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    n = len(ctx.engine.ledger.all_orders("spot", limit=1000))
    return [{"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 100, "count": 0},
            {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 200000, "count": n}]


# ---- usdm market data
@handler("usdm_exchange_info")
def usdm_exchange_info(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _exchange_info(ctx, "usdm", args)


@handler("usdm_depth")
def usdm_depth(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _depth(ctx, "usdm", args)


@handler("usdm_klines")
def usdm_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    if not args.get("interval"):
        raise E.mandatory("interval")
    return ctx.engine.feed.passthrough("usdm_klines", symbol=s, interval=args["interval"], start=_int(args.get("startTime")),
                                       end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("usdm_ticker_price")
def usdm_ticker_price(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _ticker_price(ctx, "usdm", args)


@handler("usdm_ticker24hr")
def usdm_ticker24hr(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if s and not ctx.engine.feed.symbol_info("usdm", s):
        raise E.err(E.BAD_SYMBOL)
    if not s and ctx.engine.feed.mode != "live":
        raise E.mandatory("symbol")
    return ctx.engine.feed.passthrough("usdm_ticker_24hr", symbol=s)


@handler("usdm_book_ticker")
def usdm_book_ticker(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _book_ticker(ctx, "usdm", args)


@handler("usdm_mark_price")
def usdm_mark_price(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if not s:
        if ctx.engine.feed.mode == "live":
            return ctx.engine.feed.passthrough("usdm_premium_index")
        return [ctx.engine.feed.premium_index(x["symbol"]) for x in ctx.engine.feed.exchange_info("usdm").get("symbols", [])]
    s = _require_symbol(ctx, "usdm", args)
    return ctx.engine.feed.premium_index(s)


@handler("usdm_funding_rate")
def usdm_funding_rate(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if s and not ctx.engine.feed.symbol_info("usdm", s):
        raise E.err(E.BAD_SYMBOL)
    return ctx.engine.feed.passthrough("usdm_funding_rate", symbol=s, start=_int(args.get("startTime")), end=_int(args.get("endTime")),
                                       limit=_limit(args, 100, 1000))


@handler("usdm_open_interest")
def usdm_open_interest(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    if ctx.engine.feed.mode == "live":
        return ctx.engine.feed.passthrough("usdm_open_interest", symbol=s)
    return {"symbol": s, "openInterest": "0", "time": ctx.engine.now(), "_twin_note": "replay fixture has no open interest"}


# ---- usdm account / trade
@handler("usdm_account")
def usdm_account(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.usdm.account_view()


@handler("usdm_balance")
def usdm_balance(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.usdm.balance_view()


@handler("usdm_position_risk")
def usdm_position_risk(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if s and not ctx.engine.feed.symbol_info("usdm", s):
        raise E.err(E.BAD_SYMBOL)
    return ctx.engine.usdm.position_view(s)


@handler("usdm_change_leverage")
def usdm_change_leverage(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.usdm.change_leverage(args.get("symbol"), args.get("leverage"))


@handler("usdm_change_margin_type")
def usdm_change_margin_type(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.usdm.change_margin_type(args.get("symbol"), args.get("marginType"))


@handler("usdm_new_order")
def usdm_new_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.usdm.place(args, source=ctx.source)


@handler("usdm_cancel_order")
def usdm_cancel_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if not s:
        raise E.mandatory("symbol")
    return ctx.engine.usdm.cancel(s, _int(args.get("orderId")), args.get("origClientOrderId"))


@handler("usdm_cancel_all")
def usdm_cancel_all(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if not s:
        raise E.mandatory("symbol")
    return ctx.engine.usdm.cancel_all(s)


@handler("usdm_get_order")
def usdm_get_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    oid, coid = _int(args.get("orderId")), args.get("origClientOrderId")
    if oid is None and not coid:
        raise E.mandatory("orderId")
    row = ctx.engine.ledger.get_order("usdm", oid, coid, s)
    if not row:
        raise E.err(E.NO_SUCH_ORDER)
    return ctx.engine.usdm.order_view(row)


@handler("usdm_open_orders")
def usdm_open_orders(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if s and not ctx.engine.feed.symbol_info("usdm", s):
        raise E.err(E.BAD_SYMBOL)
    return [ctx.engine.usdm.order_view(r) for r in ctx.engine.ledger.open_orders("usdm", s)]


@handler("usdm_all_orders")
def usdm_all_orders(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    return [ctx.engine.usdm.order_view(r) for r in ctx.engine.ledger.all_orders("usdm", s, _limit(args))]


@handler("usdm_user_trades")
def usdm_user_trades(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    return ctx.engine.usdm.trades_view(s, _int(args.get("orderId")), _limit(args))


@handler("usdm_income")
def usdm_income(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.usdm.income_view(_sym(args), args.get("incomeType"), _limit(args, 100, 1000))


# ---- wallet / transfer
@handler("wallet_account_status")
def wallet_account_status(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"data": "Normal"}


@handler("wallet_all_coins")
def wallet_all_coins(ctx: ToolContext, args: dict[str, Any]) -> Any:
    out = []
    seen = set()
    for b in ctx.engine.ledger.balances("spot"):
        seen.add(b["asset"])
        out.append({"coin": b["asset"], "depositAllEnable": True, "withdrawAllEnable": False, "name": b["asset"],
                    "free": b["free"], "locked": b["locked"], "freeze": "0", "withdrawing": "0", "ipoing": "0", "ipoable": "0",
                    "storage": "0", "isLegalMoney": False, "trading": True, "networkList": []})
    for a in ("BTC", "ETH", "USDT", "BNB"):
        if a not in seen:
            out.append({"coin": a, "depositAllEnable": True, "withdrawAllEnable": False, "name": a, "free": "0", "locked": "0",
                        "freeze": "0", "withdrawing": "0", "ipoing": "0", "ipoable": "0", "storage": "0", "isLegalMoney": False,
                        "trading": True, "networkList": []})
    return out


@handler("wallet_balance")
def wallet_balance(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.wallet_balance_view()


@handler("wallet_transfer")
def wallet_transfer(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.transfer(args.get("type"), args.get("asset"), args.get("amount"))


@handler("wallet_transfer_history")
def wallet_transfer_history(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if not args.get("type"):
        raise E.mandatory("type")
    return ctx.engine.transfer_history(args.get("type"), int(args.get("size") or 10))


@handler("wallet_daily_snapshot")
def wallet_daily_snapshot(ctx: ToolContext, args: dict[str, Any]) -> Any:
    t = str(args.get("type") or "").upper()
    if t not in ("SPOT", "MARGIN", "FUTURES"):
        raise E.err(E.BAD_PARAM, "type")
    eq = ctx.engine.equity()
    btc = ctx.engine.feed.last_price("spot", "BTCUSDT") or D(1)
    if t == "SPOT":
        data = {"balances": ctx.engine.ledger.balances("spot", omit_zero=True), "totalAssetOfBtc": dstr(eq["spot_value"] / btc)}
    elif t == "FUTURES":
        data = {"assets": [{"asset": "USDT", "marginBalance": dstr(eq["usdm_wallet"] + eq["usdm_unrealized"]), "walletBalance": dstr(eq["usdm_wallet"])}],
                "position": ctx.engine.usdm.position_view()}
    else:
        data = {"marginLevel": "999", "totalAssetOfBtc": "0", "totalLiabilityOfBtc": "0", "totalNetAssetOfBtc": "0", "userAssets": []}
    return {"code": 200, "msg": "", "snapshotVos": [{"type": t.lower(), "updateTime": ctx.engine.now(), "data": data}]}


@handler("wallet_api_permission")
def wallet_api_permission(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"ipRestrict": False, "createTime": ctx.engine.ledger.kv_get("reset_at", 0), "enableReading": True,
            "enableSpotAndMarginTrading": True, "enableWithdrawals": False, "enableInternalTransfer": True, "permitsUniversalTransfer": True,
            "enableVanillaOptions": False, "enableFutures": True, "enableMargin": False, "enablePortfolioMarginTrading": False,
            "enableFixApiTrade": False, "enableFixReadOnly": False}


# ---- convert
@handler("convert_exchange_info")
def convert_exchange_info(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.convert_exchange_info(args.get("fromAsset"), args.get("toAsset"))


@handler("convert_get_quote")
def convert_get_quote(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.convert_quote(args.get("fromAsset"), args.get("toAsset"), args.get("fromAmount"), args.get("toAmount"),
                                    str(args.get("walletType") or "SPOT"), str(args.get("validTime") or "10s"))


@handler("convert_accept_quote")
def convert_accept_quote(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.convert_accept(args.get("quoteId"))


@handler("convert_order_status")
def convert_order_status(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return ctx.engine.convert_status(args.get("orderId"), args.get("quoteId"))


@handler("convert_trade_flow")
def convert_trade_flow(ctx: ToolContext, args: dict[str, Any]) -> Any:
    rows = [f for f in ctx.engine.ledger.fills("spot", limit=_limit(args, 100)) if f["source"] == "convert"]
    return {"list": [{"quoteId": "", "orderId": f["order_id"], "orderStatus": "SUCCESS", "fromAsset": "", "fromAmount": dstr(f["qty"]),
                      "toAsset": f["commission_asset"], "toAmount": dstr(f["quote_qty"]), "ratio": dstr(f["price"]),
                      "inverseRatio": dstr(1 / dec(f["price"])) if dec(f["price"]) > ZERO else "0", "createTime": f["ts"]} for f in rows],
            "startTime": _int(args.get("startTime")), "endTime": _int(args.get("endTime")), "limit": _limit(args, 100), "moreData": False}


# ---- margin / sub-account
@handler("margin_account")
def margin_account(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"borrowEnabled": False, "marginLevel": "999.00000000", "totalAssetOfBtc": "0.00000000", "totalLiabilityOfBtc": "0.00000000",
            "totalNetAssetOfBtc": "0.00000000", "tradeEnabled": False, "transferEnabled": False, "accountType": "MARGIN_1", "userAssets": [],
            "_twin_note": "margin is not simulated by the twin"}


@handler("sub_account_main_assets")
def sub_account_main_assets(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"balances": [], "_twin_note": "no read-only main-account view granted to the twin"}
