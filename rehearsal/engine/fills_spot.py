"""Spot fill engine: MARKET / LIMIT (GTC, IOC, FOK) / LIMIT_MAKER / STOP_* / TAKE_PROFIT_*.

Fills are computed against the live (or replayed) order book at `now() + latency`. Resting limit
orders fill when the aggTrade stream prints at or through their price, gated by a queue-position
approximation (cumulative traded volume at/through the price since placement >= qty * queue_factor).
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from rehearsal.engine import book as B
from rehearsal.engine.filters import NormalizedOrder, validate_spot_order
from rehearsal.engine.ledger import InsufficientBalance, dec, dstr
from rehearsal.server import errors as E

if TYPE_CHECKING:
    from rehearsal.engine.engine import Engine

D = Decimal
ZERO = D(0)
STOP_TYPES_MARKET = {"STOP_LOSS", "TAKE_PROFIT"}
STOP_TYPES_LIMIT = {"STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}


class SpotEngine:
    market = "spot"

    def __init__(self, engine: "Engine"):
        self.e = engine
        self.ledger = engine.ledger
        self.feed = engine.feed

    # ------------------------------------------------------------------ helpers
    def _sym(self, symbol: str | None) -> dict[str, Any] | None:
        return self.feed.symbol_info("spot", symbol)

    def _book(self, symbol: str) -> tuple[list[B.Level], list[B.Level]]:
        d = self.feed.depth("spot", symbol, self.e.cfg.market.depth_levels)
        return B.levels(d["bids"]), B.levels(d["asks"])

    def _lock_for(self, o: NormalizedOrder, sym: dict[str, Any], ref: Decimal | None) -> tuple[str, Decimal]:
        """Asset and amount Binance would lock for this order."""
        base, quote = sym["baseAsset"], sym["quoteAsset"]
        if o.side == "SELL":
            return base, o.qty or ZERO
        if o.quote_qty is not None:
            return quote, o.quote_qty
        px = o.price if o.price is not None else (o.stop_price if o.stop_price is not None else ref)
        if px is None:
            raise E.err(E.TWIN_NO_MARKET_DATA, o.symbol)
        return quote, (o.qty or ZERO) * px

    # ------------------------------------------------------------------ order entry
    def place(self, params: dict[str, Any], source: str = "agent") -> dict[str, Any]:
        symbol = str(params.get("symbol") or "").upper()
        sym = self._sym(symbol)
        mid = self.feed.mid("spot", symbol) if sym else None
        o = validate_spot_order(sym, params, mid)
        assert sym is not None
        if mid is None:
            raise E.err(E.TWIN_NO_MARKET_DATA, symbol)

        # Policy (counts; blocks only if enforce)
        est_notional = (o.quote_qty if o.quote_qty is not None else (o.qty or ZERO) * (o.price or mid))
        self.e.policy.check_order("spot", symbol, est_notional, None, self.e.gross_exposure() + est_notional, self.e.now())

        # Immediate rejections that Binance evaluates before accepting the order.
        bids, asks = self._book(symbol)
        best_bid = bids[0].price if bids else None
        best_ask = asks[0].price if asks else None
        if o.type == "LIMIT_MAKER" and self._marketable(o, best_bid, best_ask):
            raise E.err(E.WOULD_MATCH_AND_TAKE)
        if o.is_stop and self._would_trigger(o, self.feed.last_price("spot", symbol) or mid):
            raise E.err(E.STOP_WOULD_TRIGGER)

        lock_asset, lock_amt = self._lock_for(o, sym, mid)
        if not self.ledger.can_afford("spot", lock_asset, lock_amt):
            raise E.err(E.INSUFFICIENT_BALANCE)

        # Accept the order: lock funds, persist NEW.
        order_id = self.ledger.next_order_id("spot")
        coid = o.client_order_id or self.e.gen_client_order_id()
        now = self.e.now()
        self.ledger.lock_funds("spot", lock_asset, lock_amt, "ORDER_LOCK", str(order_id))
        row = {
            "market": "spot", "order_id": order_id, "symbol": symbol, "client_order_id": coid, "side": o.side,
            "type": o.type, "orig_type": o.type, "time_in_force": o.time_in_force or "GTC",
            "price": o.price or ZERO, "stop_price": o.stop_price or ZERO, "orig_qty": o.qty or ZERO,
            "quote_order_qty": o.quote_qty or ZERO, "executed_qty": ZERO, "cum_quote": ZERO, "avg_price": ZERO,
            "status": "NEW", "triggered": 0 if o.is_stop else 1, "locked_asset": lock_asset, "locked_amount": lock_amt,
            "created_at": now, "updated_at": now, "working_time": now if not o.is_stop else -1,
            "session_id": self.ledger.current_session_id, "mid_at_arrival": mid, "source": source,
            "volume_since_place": ZERO, "extra": {"resp_type": o.new_order_resp_type},
        }
        self.ledger.insert_order(row)

        # Latency: match against the book as of now + d.
        d = self.e.latency.draw_ms()
        self.feed.wait_until(now + d)
        self.ledger.update_order("spot", order_id, latency_ms=d)
        fills: list[dict[str, Any]] = []
        with self.e.lock:
            if o.is_stop:
                pass  # rests until triggered by the trade stream
            else:
                fills = self._execute_now(order_id, o, sym, mid)
        row = self.ledger.get_order("spot", order_id) or row
        self.e.after_write("spot", symbol)
        return self.response(row, fills, o.new_order_resp_type)

    def _marketable(self, o: NormalizedOrder, best_bid: Decimal | None, best_ask: Decimal | None) -> bool:
        if o.price is None:
            return True
        if o.side == "BUY":
            return best_ask is not None and o.price >= best_ask
        return best_bid is not None and o.price <= best_bid

    def _would_trigger(self, o: NormalizedOrder, last: Decimal | None) -> bool:
        if last is None or o.stop_price is None:
            return False
        if o.type in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
            return last >= o.stop_price if o.side == "BUY" else last <= o.stop_price
        return last <= o.stop_price if o.side == "BUY" else last >= o.stop_price

    def _execute_now(self, order_id: int, o: NormalizedOrder, sym: dict[str, Any], mid: Decimal | None,
                     as_type: str | None = None) -> list[dict[str, Any]]:
        """Take liquidity for a MARKET / marketable LIMIT order. Returns fill dicts (Binance shape)."""
        typ = as_type or o.type
        bids, asks = self._book(o.symbol)
        side_levels = asks if o.side == "BUY" else bids
        step = B.lot_step(sym)
        limit = o.price if typ in ("LIMIT", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT") else None
        if typ in ("MARKET", "STOP_LOSS", "TAKE_PROFIT") and o.quote_qty is not None and o.side == "BUY":
            res = B.walk(side_levels, quote=o.quote_qty, is_buy=True, step=step)
        else:
            res = B.walk(side_levels, qty=o.qty, limit_price=limit, is_buy=(o.side == "BUY"), step=step)

        row = self.ledger.get_order("spot", order_id)
        assert row is not None
        remaining = (o.qty - res.filled_qty) if o.qty is not None else ZERO
        tif = o.time_in_force or "GTC"

        # FOK: all or nothing.
        if tif == "FOK" and o.qty is not None and res.filled_qty < o.qty:
            self._finish(row, "EXPIRED")
            return []
        if typ == "MARKET" and res.filled_qty == ZERO:
            if self.e.cfg.engine.market_insufficient_depth == "reject":
                self._finish(row, "EXPIRED")
                raise E.err(E.INSUFFICIENT_BALANCE if False else E.NEW_ORDER_REJECTED)
            self._finish(row, "EXPIRED")
            return []

        fills = [self._apply_fill(row, sym, price, qty, is_maker=False, mid=mid) for price, qty in res.fills]
        row = self.ledger.get_order("spot", order_id) or row

        if o.qty is not None and remaining > ZERO or (o.quote_qty is not None and res.exhausted):
            if typ == "MARKET" or tif in ("IOC",) or typ in ("STOP_LOSS", "TAKE_PROFIT"):
                # Binance: unfilled remainder of a market/IOC order expires.
                self._finish(row, "EXPIRED" if res.filled_qty == ZERO else ("EXPIRED" if typ != "LIMIT" else "EXPIRED"))
            else:
                # GTC limit: rest the remainder as a maker order.
                self.ledger.update_order("spot", row["order_id"], status="PARTIALLY_FILLED" if res.filled_qty > ZERO else "NEW")
        elif o.quote_qty is None or not res.exhausted:
            self._finish(row, "FILLED")
        return fills

    # ------------------------------------------------------------------ fills & settlement
    def _apply_fill(self, row: dict[str, Any], sym: dict[str, Any], price: Decimal, qty: Decimal,
                    is_maker: bool, mid: Decimal | None) -> dict[str, Any]:
        base, quote = sym["baseAsset"], sym["quoteAsset"]
        quote_qty = price * qty
        oid = row["order_id"]
        fee_rate = self.e.fees.rate("spot", is_maker)
        if row["side"] == "BUY":
            # Binance charges spot commission in the asset received: base for buys, quote for sells.
            commission = (qty * fee_rate).quantize(D("0.00000001"))
            commission_asset = base
            still_locked = dec(row["locked_amount"])
            spend = min(quote_qty, still_locked)
            extra = quote_qty - spend
            self.ledger.consume_locked("spot", quote, spend, "FILL", str(oid))
            if extra > ZERO:
                # Market-by-qty buys may cost more than the estimate that was locked.
                self.ledger.debit("spot", quote, extra, "FILL_EXTRA", str(oid))
            row["locked_amount"] = dstr(still_locked - spend)
            self.ledger.credit("spot", base, qty - commission, "FILL", str(oid))
        else:
            commission = (quote_qty * fee_rate).quantize(D("0.00000001"))
            commission_asset = quote
            self.ledger.consume_locked("spot", base, qty, "FILL", str(oid))
            row["locked_amount"] = dstr(dec(row["locked_amount"]) - qty)
            self.ledger.credit("spot", quote, quote_qty - commission, "FILL", str(oid))
        exec_qty = dec(row["executed_qty"]) + qty
        cum_quote = dec(row["cum_quote"]) + quote_qty
        avg = cum_quote / exec_qty if exec_qty > ZERO else ZERO
        self.ledger.update_order("spot", oid, executed_qty=exec_qty, cum_quote=cum_quote, avg_price=avg,
                                 locked_amount=dec(row["locked_amount"]))
        row["executed_qty"], row["cum_quote"], row["avg_price"] = dstr(exec_qty), dstr(cum_quote), dstr(avg)
        slip = B.slippage_bps(price, mid, row["side"] == "BUY")
        fid = self.ledger.add_fill(market="spot", symbol=row["symbol"], order_id=oid, side=row["side"], price=price,
                                   qty=qty, quote_qty=quote_qty, commission=commission, commission_asset=commission_asset,
                                   is_maker=int(is_maker), slippage_bps=str(slip) if slip is not None else None,
                                   session_id=row.get("session_id"), source=row.get("source") or "agent")
        return {"price": dstr(price), "qty": dstr(qty), "commission": dstr(commission),
                "commissionAsset": commission_asset, "tradeId": fid}

    def _finish(self, row: dict[str, Any], status: str) -> None:
        """Terminal state: release whatever is still locked for this order."""
        row = self.ledger.get_order("spot", row["order_id"]) or row
        leftover = dec(row["locked_amount"])
        if leftover > ZERO:
            try:
                self.ledger.unlock_funds("spot", row["locked_asset"], leftover, f"ORDER_{status}", str(row["order_id"]))
            except InsufficientBalance:
                pass
        self.ledger.update_order("spot", row["order_id"], status=status, locked_amount=ZERO)

    def _quote(self, row: dict[str, Any]) -> str:
        sym = self._sym(row["symbol"]) or {}
        return sym.get("quoteAsset", "USDT")

    # ------------------------------------------------------------------ cancel
    def cancel(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None) -> dict[str, Any]:
        symbol = symbol.upper()
        if order_id is None and orig_client_order_id is None:
            raise E.mandatory("orderId")
        row = self.ledger.get_order("spot", order_id=order_id, client_order_id=orig_client_order_id, symbol=symbol)
        if row is None:
            raise E.err(E.UNKNOWN_ORDER)
        if row["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise E.err(E.UNKNOWN_ORDER)
        with self.e.lock:
            self._finish(row, "CANCELED")
        row = self.ledger.get_order("spot", row["order_id"]) or row
        self.e.after_write("spot", symbol)
        out = self.order_view(row)
        out["origClientOrderId"] = row["client_order_id"]
        out["clientOrderId"] = self.e.gen_client_order_id()
        return out

    def cancel_all(self, symbol: str) -> list[dict[str, Any]]:
        symbol = symbol.upper()
        if not self._sym(symbol):
            raise E.err(E.BAD_SYMBOL)
        rows = self.ledger.open_orders("spot", symbol)
        if not rows:
            raise E.err(E.UNKNOWN_ORDER)
        out = []
        with self.e.lock:
            for row in rows:
                self._finish(row, "CANCELED")
                r = self.ledger.get_order("spot", row["order_id"]) or row
                v = self.order_view(r)
                v["origClientOrderId"] = r["client_order_id"]
                v["clientOrderId"] = self.e.gen_client_order_id()
                out.append(v)
        self.e.after_write("spot", symbol)
        return out

    # ------------------------------------------------------------------ stream-driven fills
    def on_trade(self, symbol: str, price: Decimal, qty: Decimal, ts: int, buyer_is_maker: bool) -> None:
        rows = self.ledger.open_orders("spot", symbol)
        if not rows:
            return
        sym = self._sym(symbol)
        if not sym:
            return
        mid = self.feed.mid("spot", symbol)
        qf = D(str(self.e.cfg.engine.queue_factor))
        for row in rows:
            typ = row["type"]
            # 1) stop triggers on last price
            if not row["triggered"]:
                stop = dec(row["stop_price"])
                if typ in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
                    hit = price >= stop if row["side"] == "BUY" else price <= stop
                else:
                    hit = price <= stop if row["side"] == "BUY" else price >= stop
                if not hit:
                    continue
                self.ledger.update_order("spot", row["order_id"], triggered=1, working_time=ts)
                self.ledger.add_event("stop_triggered", symbol, {"order_id": row["order_id"], "type": typ, "price": str(price)})
                o = self._norm_from_row(row)
                as_type = "MARKET" if typ in STOP_TYPES_MARKET else "LIMIT"
                self._execute_now(row["order_id"], o, sym, mid, as_type=as_type)
                continue
            # 2) resting limit orders (LIMIT / LIMIT_MAKER / triggered *_LIMIT)
            limit = dec(row["price"])
            if limit <= ZERO:
                continue
            is_buy = row["side"] == "BUY"
            through = price <= limit if is_buy else price >= limit
            if not through:
                continue
            remaining = dec(row["orig_qty"]) - dec(row["executed_qty"])
            if remaining <= ZERO:
                continue
            strictly_better = price < limit if is_buy else price > limit
            vol = dec(row["volume_since_place"]) + qty
            self.ledger.update_order("spot", row["order_id"], volume_since_place=vol)
            fill_qty = ZERO
            if vol >= remaining * qf:
                fill_qty = remaining
            elif strictly_better:
                fill_qty = min(remaining, qty)
            if fill_qty <= ZERO:
                continue
            step = B.lot_step(sym)
            fill_qty = B.quantize_down(fill_qty, step) if step > ZERO else fill_qty
            if fill_qty <= ZERO:
                continue
            self._apply_fill(row, sym, limit, fill_qty, is_maker=True, mid=mid)
            row = self.ledger.get_order("spot", row["order_id"]) or row
            if dec(row["executed_qty"]) >= dec(row["orig_qty"]):
                self._finish(row, "FILLED")
            else:
                self.ledger.update_order("spot", row["order_id"], status="PARTIALLY_FILLED")
            self.e.after_write("spot", symbol)

    def _norm_from_row(self, row: dict[str, Any]) -> NormalizedOrder:
        return NormalizedOrder(
            market="spot", symbol=row["symbol"], side=row["side"], type=row["type"], time_in_force=row["time_in_force"],
            qty=dec(row["orig_qty"]) if dec(row["orig_qty"]) > ZERO else None,
            quote_qty=dec(row["quote_order_qty"]) if dec(row["quote_order_qty"]) > ZERO else None,
            price=dec(row["price"]) if dec(row["price"]) > ZERO else None,
            stop_price=dec(row["stop_price"]) if dec(row["stop_price"]) > ZERO else None,
            client_order_id=row["client_order_id"],
        )

    # ------------------------------------------------------------------ views (Binance shapes)
    def order_view(self, row: dict[str, Any]) -> dict[str, Any]:
        extra = row.get("extra")
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        return {
            "symbol": row["symbol"], "orderId": row["order_id"], "orderListId": -1,
            "clientOrderId": row["client_order_id"], "price": dstr(row["price"]), "origQty": dstr(row["orig_qty"]),
            "executedQty": dstr(row["executed_qty"]), "origQuoteOrderQty": dstr(row["quote_order_qty"]),
            "cummulativeQuoteQty": dstr(row["cum_quote"]), "status": row["status"],
            "timeInForce": row["time_in_force"] or "GTC", "type": row["type"], "side": row["side"],
            "stopPrice": dstr(row["stop_price"]), "icebergQty": "0.00000000", "time": row["created_at"],
            "updateTime": row["updated_at"], "isWorking": bool(row["triggered"]),
            "workingTime": row["working_time"] if row["working_time"] is not None else row["created_at"],
            "origQuoteOrderQty_": None, "selfTradePreventionMode": "EXPIRE_MAKER",
        } | {}  # keep key order stable

    def response(self, row: dict[str, Any], fills: list[dict[str, Any]], resp_type: str) -> dict[str, Any]:
        base = {
            "symbol": row["symbol"], "orderId": row["order_id"], "orderListId": -1,
            "clientOrderId": row["client_order_id"], "transactTime": row["updated_at"],
        }
        if resp_type == "ACK":
            return base
        full = base | {
            "price": dstr(row["price"]), "origQty": dstr(row["orig_qty"]), "executedQty": dstr(row["executed_qty"]),
            "origQuoteOrderQty": dstr(row["quote_order_qty"]), "cummulativeQuoteQty": dstr(row["cum_quote"]),
            "status": row["status"], "timeInForce": row["time_in_force"] or "GTC", "type": row["type"],
            "side": row["side"], "workingTime": row["working_time"] if row["working_time"] not in (None, -1) else row["created_at"],
            "selfTradePreventionMode": "EXPIRE_MAKER",
        }
        if resp_type == "RESULT":
            return full
        full["fills"] = fills
        return full

    def trades_view(self, symbol: str | None = None, order_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.ledger.fills("spot", symbol, order_id, limit)
        out = []
        for f in rows:
            out.append({
                "symbol": f["symbol"], "id": f["fill_id"], "orderId": f["order_id"], "orderListId": -1,
                "price": dstr(f["price"]), "qty": dstr(f["qty"]), "quoteQty": dstr(f["quote_qty"]),
                "commission": dstr(f["commission"]), "commissionAsset": f["commission_asset"], "time": f["ts"],
                "isBuyer": f["side"] == "BUY", "isMaker": bool(f["is_maker"]), "isBestMatch": True,
            })
        return out

    def account_view(self, omit_zero: bool = False) -> dict[str, Any]:
        fees = self.e.fees
        return {
            "makerCommission": fees.as_bps("spot", True), "takerCommission": fees.as_bps("spot", False),
            "buyerCommission": 0, "sellerCommission": 0,
            "commissionRates": {"maker": dstr(fees.rate("spot", True)), "taker": dstr(fees.rate("spot", False)),
                                "buyer": "0.00000000", "seller": "0.00000000"},
            "canTrade": True, "canWithdraw": False, "canDeposit": True, "brokered": False,
            "requireSelfTradePrevention": False, "preventSor": False, "updateTime": self.e.now(),
            "accountType": "SPOT", "balances": self.ledger.balances("spot", omit_zero=omit_zero),
            "permissions": ["SPOT"], "uid": 0,
        }
