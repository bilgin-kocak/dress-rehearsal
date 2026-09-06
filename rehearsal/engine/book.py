"""Order-book walking helpers shared by the spot and futures fill engines."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

D = Decimal
ZERO = D(0)


@dataclass
class Level:
    price: Decimal
    qty: Decimal


@dataclass
class WalkResult:
    fills: list[tuple[Decimal, Decimal]]  # (price, qty)
    filled_qty: Decimal
    quote_spent: Decimal
    exhausted: bool  # book ran out before the order was satisfied

    @property
    def avg_price(self) -> Decimal:
        return (self.quote_spent / self.filled_qty) if self.filled_qty > ZERO else ZERO


def levels(side_levels: list[list[str]]) -> list[Level]:
    out = []
    for p, q in side_levels:
        lv = Level(D(str(p)), D(str(q)))
        if lv.qty > ZERO:
            out.append(lv)
    return out


def walk(book_side: list[Level], qty: Decimal | None = None, quote: Decimal | None = None,
         limit_price: Decimal | None = None, is_buy: bool = True, step: Decimal | None = None) -> WalkResult:
    """Consume liquidity from `book_side` (asks for a buy, bids for a sell).

    Either `qty` (base) or `quote` (quote budget, buys only) must be given. `limit_price` caps the
    levels touched. `step` rounds the per-level fill quantity down to the lot step (Binance never
    fills fractional steps).
    """
    fills: list[tuple[Decimal, Decimal]] = []
    filled = ZERO
    spent = ZERO
    remaining_qty = qty
    remaining_quote = quote
    for lv in book_side:
        if limit_price is not None:
            if is_buy and lv.price > limit_price:
                break
            if not is_buy and lv.price < limit_price:
                break
        take = lv.qty
        if remaining_qty is not None:
            take = min(take, remaining_qty)
        if remaining_quote is not None:
            affordable = remaining_quote / lv.price
            if step is not None and step > ZERO:
                affordable = (affordable / step).to_integral_value(rounding=ROUND_DOWN) * step
            take = min(take, affordable)
        if step is not None and step > ZERO:
            take = (take / step).to_integral_value(rounding=ROUND_DOWN) * step
        if take <= ZERO:
            if remaining_quote is not None:
                break
            continue
        fills.append((lv.price, take))
        filled += take
        spent += take * lv.price
        if remaining_qty is not None:
            remaining_qty -= take
            if remaining_qty <= ZERO:
                return WalkResult(fills, filled, spent, False)
        if remaining_quote is not None:
            remaining_quote -= take * lv.price
            if step is not None and step > ZERO and remaining_quote < (book_side[0].price * step if book_side else ZERO):
                return WalkResult(fills, filled, spent, False)
            if remaining_quote <= ZERO:
                return WalkResult(fills, filled, spent, False)
    return WalkResult(fills, filled, spent, True)


def lot_step(sym: dict[str, Any]) -> Decimal:
    for f in sym.get("filters", []):
        if f.get("filterType") == "LOT_SIZE":
            return D(str(f.get("stepSize", "0")))
    return ZERO


def quantize_down(x: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return x
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def slippage_bps(avg_price: Decimal, mid: Decimal | None, is_buy: bool) -> Decimal | None:
    if mid is None or mid <= ZERO or avg_price <= ZERO:
        return None
    diff = (avg_price - mid) if is_buy else (mid - avg_price)
    return (diff / mid * 10000).quantize(D("0.01"))
