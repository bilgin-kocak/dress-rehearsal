"""Validate and normalise orders exactly like Binance does (no auto-rounding).

A rejection here is a feature: it is the "hallucinated parameter" catch. Codes and messages
come from `rehearsal.server.errors`, which mirrors the public Binance error docs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from rehearsal.server import errors as E
from rehearsal.server.errors import BinanceError

D = Decimal
ZERO = D(0)

SPOT_TYPES = {"LIMIT", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "LIMIT_MAKER"}
USDM_TYPES = {"LIMIT", "MARKET", "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}
SIDES = {"BUY", "SELL"}
TIFS = {"GTC", "IOC", "FOK"}
USDM_TIFS = {"GTC", "IOC", "FOK", "GTX", "GTD"}
WORKING_TYPES = {"CONTRACT_PRICE", "MARK_PRICE"}


@dataclass
class NormalizedOrder:
    market: str
    symbol: str
    side: str
    type: str
    time_in_force: str | None = None
    qty: Decimal | None = None
    quote_qty: Decimal | None = None
    price: Decimal | None = None
    stop_price: Decimal | None = None
    client_order_id: str | None = None
    reduce_only: bool = False
    close_position: bool = False
    position_side: str = "BOTH"
    working_type: str = "CONTRACT_PRICE"
    new_order_resp_type: str = "FULL"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_market_like(self) -> bool:
        return self.type in {"MARKET", "STOP_LOSS", "TAKE_PROFIT", "STOP_MARKET", "TAKE_PROFIT_MARKET"}

    @property
    def is_stop(self) -> bool:
        return self.type in {"STOP_LOSS", "STOP_LOSS_LIMIT", "TAKE_PROFIT", "TAKE_PROFIT_LIMIT",
                             "STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}


# ---------------------------------------------------------------------------- parsing helpers
def parse_decimal(value: Any, name: str) -> Decimal:
    if value is None or value == "":
        raise E.mandatory(name)
    if isinstance(value, bool):
        raise E.err(E.BAD_PARAM, name)
    try:
        d = D(str(value))
    except (InvalidOperation, ValueError):
        raise E.err(E.BAD_PARAM, name)
    if not d.is_finite():
        raise E.err(E.BAD_PARAM, name)
    return d


def decimals_of(value: Any) -> int:
    s = str(value)
    if "e" in s.lower():
        s = format(D(s), "f")
    if "." not in s:
        return 0
    return len(s.split(".")[1].rstrip("0"))


def filter_of(sym: dict[str, Any], name: str) -> dict[str, Any] | None:
    for f in sym.get("filters", []):
        if f.get("filterType") == name:
            return f
    return None


def _multiple_of(value: Decimal, step: Decimal, base: Decimal = ZERO) -> bool:
    if step == ZERO:
        return True
    rem = (value - base) % step
    # Guard against tiny binary artefacts: Decimal is exact so equality is fine.
    return rem == ZERO


# ---------------------------------------------------------------------------- filter checks
def check_price_filter(sym: dict[str, Any], price: Decimal) -> None:
    f = filter_of(sym, "PRICE_FILTER")
    if not f:
        return
    min_p, max_p, tick = D(f.get("minPrice", "0")), D(f.get("maxPrice", "0")), D(f.get("tickSize", "0"))
    if price <= ZERO:
        raise E.filter_failure("PRICE_FILTER")
    if min_p > ZERO and price < min_p:
        raise E.filter_failure("PRICE_FILTER")
    if max_p > ZERO and price > max_p:
        raise E.filter_failure("PRICE_FILTER")
    if not _multiple_of(price, tick, min_p if min_p > ZERO else ZERO):
        raise E.filter_failure("PRICE_FILTER")


def check_lot_size(sym: dict[str, Any], qty: Decimal, market_order: bool) -> None:
    name = "MARKET_LOT_SIZE" if market_order and filter_of(sym, "MARKET_LOT_SIZE") else "LOT_SIZE"
    f = filter_of(sym, name)
    if market_order:
        # Binance applies LOT_SIZE to all orders and MARKET_LOT_SIZE additionally to market orders.
        lot = filter_of(sym, "LOT_SIZE")
        if lot:
            _check_lot(lot, qty, "LOT_SIZE")
    if f:
        _check_lot(f, qty, name)


def _check_lot(f: dict[str, Any], qty: Decimal, name: str) -> None:
    min_q, max_q, step = D(f.get("minQty", "0")), D(f.get("maxQty", "0")), D(f.get("stepSize", "0"))
    if qty <= ZERO:
        raise E.filter_failure(name)
    if min_q > ZERO and qty < min_q:
        raise E.filter_failure(name)
    if max_q > ZERO and qty > max_q:
        raise E.filter_failure(name)
    if not _multiple_of(qty, step, min_q if step > ZERO and min_q > ZERO and min_q % step == ZERO else ZERO):
        raise E.filter_failure(name)


def check_notional(sym: dict[str, Any], notional: Decimal, market_order: bool, market: str) -> None:
    f = filter_of(sym, "NOTIONAL") or filter_of(sym, "MIN_NOTIONAL")
    if not f:
        return
    min_n = D(f.get("minNotional") or f.get("notional") or "0")
    max_n = D(f.get("maxNotional", "0") or "0")
    apply_market = f.get("applyMinToMarket", f.get("applyToMarket", True))
    if market_order and not apply_market:
        return
    if min_n > ZERO and notional < min_n:
        if market == "usdm":
            raise E.err(E.FUT_NOTIONAL_TOO_SMALL, format(min_n.normalize(), "f"))
        raise E.filter_failure("NOTIONAL")
    if max_n > ZERO and notional > max_n:
        raise E.filter_failure("NOTIONAL")


def check_percent_price_by_side(sym: dict[str, Any], side: str, price: Decimal, ref: Decimal | None) -> None:
    f = filter_of(sym, "PERCENT_PRICE_BY_SIDE")
    if not f or ref is None or ref <= ZERO:
        return
    if side == "BUY":
        up, down = D(f.get("bidMultiplierUp", "5")), D(f.get("bidMultiplierDown", "0.2"))
    else:
        up, down = D(f.get("askMultiplierUp", "5")), D(f.get("askMultiplierDown", "0.2"))
    if price > ref * up or price < ref * down:
        raise E.filter_failure("PERCENT_PRICE_BY_SIDE")


def check_percent_price(sym: dict[str, Any], price: Decimal, ref: Decimal | None) -> None:
    f = filter_of(sym, "PERCENT_PRICE")
    if not f or ref is None or ref <= ZERO:
        return
    up, down = D(f.get("multiplierUp", "1.05")), D(f.get("multiplierDown", "0.95"))
    if price > ref * up or price < ref * down:
        raise E.filter_failure("PERCENT_PRICE")


# ---------------------------------------------------------------------------- public API
def validate_symbol(exchange_info_symbol: dict[str, Any] | None, symbol: Any) -> dict[str, Any]:
    if not symbol or not isinstance(symbol, str):
        raise E.mandatory("symbol")
    if exchange_info_symbol is None:
        raise E.err(E.BAD_SYMBOL)
    if exchange_info_symbol.get("status") not in (None, "TRADING"):
        raise E.err(E.FUT_INVALID_SYMBOL_STATUS)
    return exchange_info_symbol


def validate_spot_order(sym: dict[str, Any] | None, p: dict[str, Any], ref_price: Decimal | None) -> NormalizedOrder:
    """Validate a spot order request. `ref_price` is the current mid (used for MARKET notional
    and PERCENT_PRICE_BY_SIDE). Raises BinanceError with the real code/message."""
    symbol = p.get("symbol")
    sym = validate_symbol(sym, symbol)
    side = str(p.get("side") or "").upper()
    if not p.get("side"):
        raise E.mandatory("side")
    if side not in SIDES:
        raise E.err(E.BAD_SIDE)
    typ = str(p.get("type") or "").upper()
    if not p.get("type"):
        raise E.mandatory("type")
    if typ not in SPOT_TYPES:
        raise E.err(E.BAD_ORDER_TYPE)
    if typ not in set(sym.get("orderTypes", [])) and sym.get("orderTypes"):
        raise E.err(E.BAD_ORDER_TYPE)

    tif = p.get("timeInForce")
    if tif is not None:
        tif = str(tif).upper()
        if tif not in TIFS:
            raise E.err(E.BAD_TIF)

    coid = p.get("newClientOrderId")
    if coid is not None:
        coid = str(coid)
        if coid == "":
            raise E.err(E.EMPTY_CLIENT_ORDER_ID)
        if len(coid) > 36:
            raise E.err(E.BAD_PARAM, "newClientOrderId")

    base_prec = int(sym.get("baseAssetPrecision", 8))
    quote_prec = int(sym.get("quoteAssetPrecision", sym.get("quotePrecision", 8)))

    qty = quote_qty = price = stop = None
    if typ == "LIMIT":
        if tif is None:
            raise E.mandatory("timeInForce")
        qty = parse_decimal(p.get("quantity"), "quantity")
        price = parse_decimal(p.get("price"), "price")
    elif typ == "MARKET":
        has_q = p.get("quantity") not in (None, "")
        has_qq = p.get("quoteOrderQty") not in (None, "")
        if has_q and has_qq:
            raise E.err(E.UNSUPPORTED_ORDER_COMBINATION, "quoteOrderQty")
        if not has_q and not has_qq:
            raise E.mandatory("quantity")
        if has_q:
            qty = parse_decimal(p.get("quantity"), "quantity")
        else:
            if not sym.get("quoteOrderQtyMarketAllowed", True):
                raise E.err(E.UNSUPPORTED_ORDER_COMBINATION, "quoteOrderQty")
            quote_qty = parse_decimal(p.get("quoteOrderQty"), "quoteOrderQty")
    elif typ in ("STOP_LOSS", "TAKE_PROFIT"):
        qty = parse_decimal(p.get("quantity"), "quantity")
        stop = parse_decimal(p.get("stopPrice"), "stopPrice")
    elif typ in ("STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"):
        if tif is None:
            raise E.mandatory("timeInForce")
        qty = parse_decimal(p.get("quantity"), "quantity")
        price = parse_decimal(p.get("price"), "price")
        stop = parse_decimal(p.get("stopPrice"), "stopPrice")
    elif typ == "LIMIT_MAKER":
        qty = parse_decimal(p.get("quantity"), "quantity")
        price = parse_decimal(p.get("price"), "price")

    # Precision checks (-1111) come before filter checks.
    if qty is not None and decimals_of(p.get("quantity")) > base_prec:
        raise E.err(E.BAD_PRECISION)
    if quote_qty is not None and decimals_of(p.get("quoteOrderQty")) > quote_prec:
        raise E.err(E.BAD_PRECISION)
    if price is not None and decimals_of(p.get("price")) > quote_prec:
        raise E.err(E.BAD_PRECISION)

    is_market = typ in ("MARKET", "STOP_LOSS", "TAKE_PROFIT")
    if price is not None:
        check_price_filter(sym, price)
        check_percent_price_by_side(sym, side, price, ref_price)
    if stop is not None:
        check_price_filter(sym, stop)
    if qty is not None:
        check_lot_size(sym, qty, market_order=is_market)
        ref = price if price is not None else ref_price
        if ref is not None:
            check_notional(sym, qty * ref, market_order=is_market, market="spot")
    if quote_qty is not None:
        if quote_qty <= ZERO:
            raise E.err(E.INVALID_QUANTITY)
        check_notional(sym, quote_qty, market_order=True, market="spot")

    return NormalizedOrder(
        market="spot", symbol=sym["symbol"], side=side, type=typ, time_in_force=tif or ("GTC" if typ == "LIMIT" else None),
        qty=qty, quote_qty=quote_qty, price=price, stop_price=stop, client_order_id=coid,
        new_order_resp_type=str(p.get("newOrderRespType") or "FULL").upper(), raw=dict(p),
    )


def validate_usdm_order(sym: dict[str, Any] | None, p: dict[str, Any], ref_price: Decimal | None) -> NormalizedOrder:
    """Validate a USDⓈ-M futures order request. `ref_price` is the mark price."""
    symbol = p.get("symbol")
    sym = validate_symbol(sym, symbol)
    if not p.get("side"):
        raise E.mandatory("side")
    side = str(p.get("side")).upper()
    if side not in SIDES:
        raise E.err(E.BAD_SIDE)
    if not p.get("type"):
        raise E.mandatory("type")
    typ = str(p.get("type")).upper()
    if typ not in USDM_TYPES:
        raise E.err(E.BAD_ORDER_TYPE)
    if typ == "TRAILING_STOP_MARKET":
        raise E.err(E.TWIN_UNSUPPORTED, "TRAILING_STOP_MARKET orders")

    tif = p.get("timeInForce")
    if tif is not None:
        tif = str(tif).upper()
        if tif not in USDM_TIFS:
            raise E.err(E.BAD_TIF)

    coid = p.get("newClientOrderId")
    if coid is not None:
        coid = str(coid)
        if coid == "" or len(coid) > 36:
            raise E.err(E.BAD_PARAM, "newClientOrderId")

    position_side = str(p.get("positionSide") or "BOTH").upper()
    if position_side not in ("BOTH", "LONG", "SHORT"):
        raise E.err(E.BAD_PARAM, "positionSide")
    working_type = str(p.get("workingType") or "CONTRACT_PRICE").upper()
    if working_type not in WORKING_TYPES:
        raise E.err(E.FUT_INVALID_WORKING_TYPE, working_type)
    reduce_only = str(p.get("reduceOnly", "false")).lower() == "true"
    close_position = str(p.get("closePosition", "false")).lower() == "true"

    qty_prec = int(sym.get("quantityPrecision", 8))
    price_prec = int(sym.get("pricePrecision", 8))

    qty = price = stop = None
    if typ == "LIMIT":
        if tif is None:
            raise E.mandatory("timeInForce")
        qty = parse_decimal(p.get("quantity"), "quantity")
        price = parse_decimal(p.get("price"), "price")
    elif typ == "MARKET":
        qty = parse_decimal(p.get("quantity"), "quantity")
    elif typ in ("STOP", "TAKE_PROFIT"):
        qty = parse_decimal(p.get("quantity"), "quantity")
        price = parse_decimal(p.get("price"), "price")
        stop = parse_decimal(p.get("stopPrice"), "stopPrice")
    elif typ in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
        stop = parse_decimal(p.get("stopPrice"), "stopPrice")
        if not close_position:
            qty = parse_decimal(p.get("quantity"), "quantity")

    if qty is not None and decimals_of(p.get("quantity")) > qty_prec:
        raise E.err(E.BAD_PRECISION)
    if price is not None and decimals_of(p.get("price")) > price_prec:
        raise E.err(E.BAD_PRECISION)
    if stop is not None and decimals_of(p.get("stopPrice")) > price_prec:
        raise E.err(E.BAD_PRECISION)

    is_market = typ in ("MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET")
    if price is not None:
        check_price_filter(sym, price)
        check_percent_price(sym, price, ref_price)
    if stop is not None:
        check_price_filter(sym, stop)
    if qty is not None:
        if qty <= ZERO:
            raise E.err(E.FUT_QTY_LESS_THAN_ZERO)
        check_lot_size(sym, qty, market_order=is_market)
        ref = price if price is not None else ref_price
        if ref is not None and not reduce_only and not close_position:
            check_notional(sym, qty * ref, market_order=is_market, market="usdm")

    return NormalizedOrder(
        market="usdm", symbol=sym["symbol"], side=side, type=typ,
        time_in_force=tif or ("GTC" if typ in ("LIMIT", "STOP", "TAKE_PROFIT") else None),
        qty=qty, price=price, stop_price=stop, client_order_id=coid, reduce_only=reduce_only,
        close_position=close_position, position_side=position_side, working_type=working_type,
        new_order_resp_type=str(p.get("newOrderRespType") or "ACK").upper(), raw=dict(p),
    )


def validate_leverage(sym: dict[str, Any] | None, symbol: Any, leverage: Any, max_leverage: int) -> int:
    validate_symbol(sym, symbol)
    if leverage is None or leverage == "":
        raise E.mandatory("leverage")
    try:
        lev = int(str(leverage))
    except ValueError:
        raise E.err(E.BAD_PARAM, "leverage")
    if lev < 1 or lev > max_leverage:
        raise E.err(E.FUT_INVALID_LEVERAGE, lev)
    return lev
