"""Handlers for the long tail of the real catalog: kline variants, COIN-M / margin read shapes,
wallet extras, futures account configuration. Registered into the same HANDLERS table."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rehearsal.engine.ledger import dec, dstr
from rehearsal.server import errors as E
from rehearsal.server.handlers import ToolContext, _int, _limit, _require_symbol, _sym, handler

D = Decimal
ZERO = D(0)


def _passthrough_or_synth(ctx: ToolContext, name: str, market: str, **kw: Any) -> Any:
    feed = ctx.engine.feed
    if feed.mode == "live":
        return feed.passthrough(name, **kw)
    if market == "coinm":
        raise E.err(E.TWIN_NO_MARKET_DATA, f"{name} (COIN-M is not recorded in replay fixtures)")
    return feed.passthrough(name, **kw)


# ------------------------------------------------------------------ generic
@handler("ping")
def ping(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {}


@handler("server_time")
def server_time(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"serverTime": ctx.engine.now()}


@handler("empty_list")
def empty_list(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return []


@handler("empty_rows")
def empty_rows(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"rows": [], "total": 0}


@handler("empty_dustlog")
def empty_dustlog(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"total": 0, "userAssetDribblets": []}


@handler("no_such_order")
def no_such_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    raise E.err(E.NO_SUCH_ORDER)


# ------------------------------------------------------------------ spot extras
@handler("spot_ticker_trading_day")
def spot_ticker_trading_day(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    return ctx.engine.feed.passthrough("spot_ticker_24hr", symbol=s, type_=args.get("type"))


@handler("spot_recent_trades")
def spot_recent_trades(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    rows = ctx.engine.feed.passthrough("spot_agg_trades", symbol=s, limit=_limit(args, 500, 1000))
    return [{"id": r.get("a"), "price": r.get("p"), "qty": r.get("q"), "quoteQty": dstr(D(str(r.get("p", "0"))) * D(str(r.get("q", "0")))),
             "time": r.get("T"), "isBuyerMaker": r.get("m", False), "isBestMatch": True} for r in rows]


@handler("spot_account_commission")
def spot_account_commission(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    f = ctx.engine.fees
    return {"symbol": s, "standardCommission": {"maker": dstr(f.rate("spot", True)), "taker": dstr(f.rate("spot", False)),
                                                "buyer": "0.00000000", "seller": "0.00000000"},
            "taxCommission": {"maker": "0.00000000", "taker": "0.00000000", "buyer": "0.00000000", "seller": "0.00000000"},
            "discount": {"enabledForAccount": False, "enabledForSymbol": False, "discountAsset": "BNB", "discount": "0.75000000"}}


@handler("spot_my_filters")
def spot_my_filters(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "spot", args)
    sym = ctx.engine.feed.symbol_info("spot", s) or {}
    return {"symbol": s, "filters": sym.get("filters", [])}


# ------------------------------------------------------------------ USDⓈ-M extras
@handler("usdm_mark_price_klines")
def usdm_mark_price_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    if not args.get("interval"):
        raise E.mandatory("interval")
    return _passthrough_or_synth(ctx, "usdm_mark_price_klines", "usdm", symbol=s, interval=args["interval"],
                                 start=_int(args.get("startTime")), end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("usdm_index_price_klines")
def usdm_index_price_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    pair = str(args.get("pair") or args.get("symbol") or "").upper()
    if not pair:
        raise E.mandatory("pair")
    if not args.get("interval"):
        raise E.mandatory("interval")
    if not ctx.engine.feed.symbol_info("usdm", pair):
        raise E.err(E.BAD_SYMBOL)
    return _passthrough_or_synth(ctx, "usdm_index_price_klines", "usdm", pair=pair, interval=args["interval"],
                                 start=_int(args.get("startTime")), end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("usdm_premium_index_klines")
def usdm_premium_index_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    if not args.get("interval"):
        raise E.mandatory("interval")
    return _passthrough_or_synth(ctx, "usdm_premium_index_klines", "usdm", symbol=s, interval=args["interval"],
                                 start=_int(args.get("startTime")), end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("usdm_continuous_klines")
def usdm_continuous_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    pair = str(args.get("pair") or "").upper()
    if not pair:
        raise E.mandatory("pair")
    if not args.get("contractType"):
        raise E.mandatory("contractType")
    if not args.get("interval"):
        raise E.mandatory("interval")
    if not ctx.engine.feed.symbol_info("usdm", pair):
        raise E.err(E.BAD_SYMBOL)
    return _passthrough_or_synth(ctx, "usdm_continuous_klines", "usdm", pair=pair, contract_type=str(args["contractType"]),
                                 interval=args["interval"], start=_int(args.get("startTime")), end=_int(args.get("endTime")),
                                 limit=_limit(args, 500, 1500))


@handler("usdm_funding_info")
def usdm_funding_info(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if ctx.engine.feed.mode == "live":
        return ctx.engine.feed.passthrough("usdm_funding_info")
    return [{"symbol": x["symbol"], "adjustedFundingRateCap": "0.02000000", "adjustedFundingRateFloor": "-0.02000000",
             "fundingIntervalHours": 8, "disclaimer": False} for x in ctx.engine.feed.exchange_info("usdm").get("symbols", [])]


@handler("usdm_agg_trades")
def usdm_agg_trades(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    return ctx.engine.feed.passthrough("usdm_agg_trades", symbol=s, limit=_limit(args, 500, 1000))


@handler("usdm_leverage_brackets")
def usdm_leverage_brackets(ctx: ToolContext, args: dict[str, Any]) -> Any:
    mmr = dstr(D(str(ctx.engine.cfg.engine.usdm.maintenance_rate)))
    s = _sym(args)
    syms = [s] if s else [x["symbol"] for x in ctx.engine.feed.exchange_info("usdm").get("symbols", [])[:50]]
    out = []
    for sym in syms:
        if not ctx.engine.feed.symbol_info("usdm", sym):
            raise E.err(E.BAD_SYMBOL)
        out.append({"symbol": sym, "notionalCoef": "4.0", "brackets": [
            {"bracket": 1, "initialLeverage": ctx.engine.cfg.engine.usdm.max_leverage, "notionalCap": 50000, "notionalFloor": 0,
             "maintMarginRatio": float(mmr), "cum": 0.0}]})
    return out[0] if s else out


@handler("usdm_position_risk_v3")
def usdm_position_risk_v3(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    if s and not ctx.engine.feed.symbol_info("usdm", s):
        raise E.err(E.BAD_SYMBOL)
    rows = ctx.engine.usdm.position_view(s)
    return [r for r in rows if dec(r["positionAmt"]) != ZERO]


@handler("usdm_get_open_order")
def usdm_get_open_order(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    oid, coid = _int(args.get("orderId")), args.get("origClientOrderId")
    if oid is None and not coid:
        raise E.mandatory("orderId")
    row = ctx.engine.ledger.get_order("usdm", oid, coid, s)
    if not row or row["status"] not in ("NEW", "PARTIALLY_FILLED"):
        raise E.err(E.NO_SUCH_ORDER)
    return ctx.engine.usdm.order_view(row)


@handler("usdm_position_mode")
def usdm_position_mode(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"dualSidePosition": False}


@handler("usdm_multi_assets_mode")
def usdm_multi_assets_mode(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"multiAssetsMargin": False}


@handler("usdm_account_config")
def usdm_account_config(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"feeTier": 0, "canTrade": True, "canDeposit": True, "canWithdraw": False, "dualSidePosition": False,
            "updateTime": 0, "multiAssetsMargin": False, "tradeGroupId": -1}


@handler("usdm_symbol_config")
def usdm_symbol_config(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _sym(args)
    syms = [s] if s else [x["symbol"] for x in ctx.engine.feed.exchange_info("usdm").get("symbols", [])[:50]]
    out = []
    for sym in syms:
        if not ctx.engine.feed.symbol_info("usdm", sym):
            raise E.err(E.BAD_SYMBOL)
        st = ctx.engine.usdm.settings(sym)
        out.append({"symbol": sym, "marginType": "CROSSED" if st["margin_type"] == "CROSSED" else "ISOLATED", "isAutoAddMargin": "false",
                    "leverage": int(st["leverage"]), "maxNotionalValue": "100000000"})
    return out


@handler("usdm_commission_rate")
def usdm_commission_rate(ctx: ToolContext, args: dict[str, Any]) -> Any:
    s = _require_symbol(ctx, "usdm", args)
    f = ctx.engine.fees
    return {"symbol": s, "makerCommissionRate": dstr(f.rate("usdm", True)), "takerCommissionRate": dstr(f.rate("usdm", False))}


@handler("usdm_new_order_test")
def usdm_new_order_test(ctx: ToolContext, args: dict[str, Any]) -> Any:
    from rehearsal.engine.filters import validate_usdm_order
    s = _sym(args)
    sym = ctx.engine.feed.symbol_info("usdm", s)
    mark = ctx.engine.feed.mark_price(s) if sym else None
    validate_usdm_order(sym, args, mark)
    return {}


# ------------------------------------------------------------------ COIN-M (public market data only)
def _coinm_symbol(args: dict[str, Any]) -> str:
    s = str(args.get("symbol") or "").upper()
    if not s:
        raise E.mandatory("symbol")
    return s


@handler("coinm_exchange_info")
def coinm_exchange_info(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _passthrough_or_synth(ctx, "coinm_exchange_info", "coinm")


@handler("coinm_klines")
def coinm_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if not args.get("interval"):
        raise E.mandatory("interval")
    return _passthrough_or_synth(ctx, "coinm_klines", "coinm", symbol=_coinm_symbol(args), interval=args["interval"],
                                 start=_int(args.get("startTime")), end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("coinm_mark_price_klines")
def coinm_mark_price_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if not args.get("interval"):
        raise E.mandatory("interval")
    return _passthrough_or_synth(ctx, "coinm_mark_price_klines", "coinm", symbol=_coinm_symbol(args), interval=args["interval"],
                                 start=_int(args.get("startTime")), end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("coinm_index_price_klines")
def coinm_index_price_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    pair = str(args.get("pair") or "").upper()
    if not pair:
        raise E.mandatory("pair")
    if not args.get("interval"):
        raise E.mandatory("interval")
    return _passthrough_or_synth(ctx, "coinm_index_price_klines", "coinm", pair=pair, interval=args["interval"],
                                 start=_int(args.get("startTime")), end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("coinm_premium_index_klines")
def coinm_premium_index_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if not args.get("interval"):
        raise E.mandatory("interval")
    return _passthrough_or_synth(ctx, "coinm_premium_index_klines", "coinm", symbol=_coinm_symbol(args), interval=args["interval"],
                                 start=_int(args.get("startTime")), end=_int(args.get("endTime")), limit=_limit(args, 500, 1500))


@handler("coinm_continuous_klines")
def coinm_continuous_klines(ctx: ToolContext, args: dict[str, Any]) -> Any:
    pair = str(args.get("pair") or "").upper()
    if not pair:
        raise E.mandatory("pair")
    if not args.get("contractType"):
        raise E.mandatory("contractType")
    if not args.get("interval"):
        raise E.mandatory("interval")
    return _passthrough_or_synth(ctx, "coinm_continuous_klines", "coinm", pair=pair, contract_type=str(args["contractType"]),
                                 interval=args["interval"], start=_int(args.get("startTime")), end=_int(args.get("endTime")),
                                 limit=_limit(args, 500, 1500))


@handler("coinm_ticker_price")
def coinm_ticker_price(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return _passthrough_or_synth(ctx, "coinm_ticker_price", "coinm", symbol=(str(args.get("symbol")).upper() if args.get("symbol") else None),
                                 pair=(str(args.get("pair")).upper() if args.get("pair") else None))


@handler("coinm_account")
def coinm_account(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"assets": [], "positions": [], "canDeposit": True, "canTrade": True, "canWithdraw": False, "feeTier": 0, "updateTime": 0}


# ------------------------------------------------------------------ margin read shapes
@handler("margin_all_assets")
def margin_all_assets(ctx: ToolContext, args: dict[str, Any]) -> Any:
    out = []
    for a in ("BTC", "ETH", "USDT", "BNB"):
        out.append({"assetFullName": a, "assetName": a, "isBorrowable": True, "isMortgageable": True, "userMinBorrow": "0.00000000",
                    "userMinRepay": "0.00000000", "delistTime": 0})
    return out


@handler("margin_max_borrow")
def margin_max_borrow(ctx: ToolContext, args: dict[str, Any]) -> Any:
    if not args.get("asset"):
        raise E.mandatory("asset")
    return {"amount": "0.00000000", "borrowLimit": "0.00000000"}


# ------------------------------------------------------------------ wallet extras
@handler("wallet_system_status")
def wallet_system_status(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"status": 0, "msg": "normal"}


@handler("wallet_trade_fee")
def wallet_trade_fee(ctx: ToolContext, args: dict[str, Any]) -> Any:
    f = ctx.engine.fees
    s = _sym(args)
    syms = [s] if s else [x["symbol"] for x in ctx.engine.feed.exchange_info("spot").get("symbols", [])[:100]]
    return [{"symbol": x, "standardCommission": {"maker": dstr(f.rate("spot", True)), "taker": dstr(f.rate("spot", False))},
             "taxCommission": {"maker": "0.00000000", "taker": "0.00000000"}, "discount": {"enabledForAccount": False, "enabledForSymbol": False,
             "discountAsset": "BNB", "discount": "0.75000000"}} for x in syms]


@handler("wallet_account_info")
def wallet_account_info(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"vipLevel": 0, "isMarginEnabled": False, "isFutureEnabled": True, "isOptionsEnabled": False, "isPortfolioMarginRetailEnabled": False}


# ------------------------------------------------------------------ convert extras
@handler("convert_asset_precision")
def convert_asset_precision(ctx: ToolContext, args: dict[str, Any]) -> Any:
    seen: dict[str, int] = {}
    for x in ctx.engine.feed.exchange_info("spot").get("symbols", []):
        seen.setdefault(x.get("baseAsset", ""), int(x.get("baseAssetPrecision", 8)))
        seen.setdefault(x.get("quoteAsset", ""), int(x.get("quoteAssetPrecision", 8)))
    return [{"asset": a, "fraction": p} for a, p in sorted(seen.items()) if a][:300]


@handler("convert_limit_open_orders")
def convert_limit_open_orders(ctx: ToolContext, args: dict[str, Any]) -> Any:
    return {"list": []}
