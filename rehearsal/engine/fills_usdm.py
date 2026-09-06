"""USDⓈ-M futures engine (isolated margin, one-way mode).

Mark price drives unrealized PnL and liquidation; last price drives CONTRACT_PRICE stop triggers.
Funding is applied at 00:00 / 08:00 / 16:00 UTC on the virtual clock.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from rehearsal.engine import book as B
from rehearsal.engine.filters import NormalizedOrder, validate_leverage, validate_usdm_order
from rehearsal.engine.ledger import dec, dstr
from rehearsal.server import errors as E

if TYPE_CHECKING:
    from rehearsal.engine.engine import Engine

D = Decimal
ZERO = D(0)
FUNDING_PERIOD_MS = 8 * 3600 * 1000
MARKET_TYPES = {"MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"}
STOP_TYPES = {"STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET"}


class UsdmEngine:
    market = "usdm"

    def __init__(self, engine: "Engine"):
        self.e = engine
        self.ledger = engine.ledger
        self.feed = engine.feed
        self.cfg = engine.cfg.engine.usdm
        self._last_funding: dict[str, int] = {}

    # ------------------------------------------------------------------ helpers
    def _sym(self, symbol: str | None) -> dict[str, Any] | None:
        return self.feed.symbol_info("usdm", symbol)

    def _prec(self, sym: dict[str, Any]) -> tuple[int, int]:
        return int(sym.get("pricePrecision", 2)), int(sym.get("quantityPrecision", 3))

    def _book(self, symbol: str) -> tuple[list[B.Level], list[B.Level]]:
        d = self.feed.depth("usdm", symbol, self.e.cfg.market.depth_levels)
        return B.levels(d["bids"]), B.levels(d["asks"])

    def settings(self, symbol: str) -> dict[str, Any]:
        return self.ledger.symbol_settings(symbol, self.cfg.default_leverage)

    def mark(self, symbol: str) -> Decimal:
        m = self.feed.mark_price(symbol)
        if m is None:
            raise E.err(E.TWIN_NO_MARKET_DATA, symbol)
        return m

    def wallet(self) -> tuple[Decimal, Decimal]:
        return self.ledger.balance("usdm", "USDT")

    def unrealized(self, symbol: str, mark: Decimal | None = None) -> Decimal:
        pos = self.ledger.position(symbol)
        amt, entry = dec(pos["position_amt"]), dec(pos["entry_price"])
        if amt == ZERO:
            return ZERO
        m = mark if mark is not None else (self.feed.mark_price(symbol) or entry)
        return (m - entry) * amt

    def liquidation_price(self, symbol: str) -> Decimal:
        pos = self.ledger.position(symbol)
        amt, entry, margin = dec(pos["position_amt"]), dec(pos["entry_price"]), dec(pos["isolated_margin"])
        if amt == ZERO:
            return ZERO
        mmr = D(str(self.cfg.maintenance_rate))
        q = abs(amt)
        if amt > ZERO:
            lp = (entry * q - margin) / (q * (1 - mmr))
        else:
            lp = (entry * q + margin) / (q * (1 + mmr))
        return max(lp, ZERO)

    # ------------------------------------------------------------------ leverage / margin type
    def change_leverage(self, symbol: str, leverage: Any) -> dict[str, Any]:
        symbol = str(symbol or "").upper()
        sym = self._sym(symbol)
        lev = validate_leverage(sym, symbol, leverage, self.cfg.max_leverage)
        self.e.policy.check_leverage(symbol, lev)
        pos = self.ledger.position(symbol)
        if dec(pos["position_amt"]) != ZERO:
            # Binance allows changing leverage with a position only if margin permits; keep simple: allow.
            pass
        self.ledger.set_symbol_settings(symbol, leverage=lev, default_leverage=self.cfg.default_leverage)
        self.ledger.add_event("leverage_changed", symbol, {"leverage": lev})
        max_notional = "1000000" if lev >= 50 else "5000000" if lev >= 20 else "20000000"
        return {"symbol": symbol, "leverage": lev, "maxNotionalValue": max_notional}

    def change_margin_type(self, symbol: str, margin_type: Any) -> dict[str, Any]:
        symbol = str(symbol or "").upper()
        if not self._sym(symbol):
            raise E.err(E.BAD_SYMBOL)
        mt = str(margin_type or "").upper()
        if mt not in ("ISOLATED", "CROSSED"):
            raise E.err(E.BAD_PARAM, "marginType")
        cur = self.settings(symbol)
        if cur["margin_type"] == mt:
            raise E.err(E.FUT_NO_NEED_TO_CHANGE_MARGIN_TYPE)
        if dec(self.ledger.position(symbol)["position_amt"]) != ZERO:
            raise E.err(E.FUT_MARGIN_TYPE_POSITION)
        if self.ledger.open_orders("usdm", symbol):
            raise E.err(E.FUT_MARGIN_TYPE_CANNOT_CHANGE)
        if mt == "CROSSED":
            raise E.err(E.TWIN_UNSUPPORTED, "CROSSED margin (twin supports ISOLATED only)")
        self.ledger.set_symbol_settings(symbol, margin_type=mt, default_leverage=self.cfg.default_leverage)
        return {"code": 200, "msg": "success"}

    # ------------------------------------------------------------------ order entry
    def place(self, params: dict[str, Any], source: str = "agent") -> dict[str, Any]:
        symbol = str(params.get("symbol") or "").upper()
        sym = self._sym(symbol)
        mark = self.feed.mark_price(symbol) if sym else None
        o = validate_usdm_order(sym, params, mark)
        assert sym is not None
        if mark is None:
            raise E.err(E.TWIN_NO_MARKET_DATA, symbol)
        st = self.settings(symbol)
        lev = int(st["leverage"])
        pos = self.ledger.position(symbol)
        pos_amt = dec(pos["position_amt"])

        # reduceOnly / closePosition semantics
        if o.close_position:
            if pos_amt == ZERO:
                raise E.err(E.FUT_REDUCE_ONLY_REJECTED)
            o.qty = abs(pos_amt)
            o.reduce_only = True
        if o.reduce_only:
            if pos_amt == ZERO or (pos_amt > ZERO and o.side == "BUY") or (pos_amt < ZERO and o.side == "SELL"):
                raise E.err(E.FUT_REDUCE_ONLY_REJECTED)
            if o.qty is not None and o.qty > abs(pos_amt):
                o.qty = abs(pos_amt)  # Binance caps reduce-only at the position size
        assert o.qty is not None

        ref = o.price if o.price is not None else mark
        notional = o.qty * ref
        opening_qty = self._opening_qty(pos_amt, o.side, o.qty)
        init_margin = (opening_qty * ref / lev) if opening_qty > ZERO else ZERO
        est_fee = notional * self.e.fees.rate("usdm", False)

        self.e.policy.check_order("usdm", symbol, notional, lev, self.e.gross_exposure() + opening_qty * ref, self.e.now())

        # Immediate rejections
        last = self.feed.last_price("usdm", symbol) or mark
        if o.is_stop and self._would_trigger(o, last if o.working_type == "CONTRACT_PRICE" else mark):
            raise E.err(E.FUT_ORDER_WOULD_TRIGGER)
        free, _ = self.wallet()
        if init_margin + est_fee > free and not o.reduce_only:
            raise E.err(E.FUT_MARGIN_INSUFFICIENT)
        if o.time_in_force == "GTX":
            bids, asks = self._book(symbol)
            if self._marketable(o, bids[0].price if bids else None, asks[0].price if asks else None):
                raise E.err(E.WOULD_MATCH_AND_TAKE)

        order_id = self.ledger.next_order_id("usdm")
        coid = o.client_order_id or self.e.gen_client_order_id()
        now = self.e.now()
        # Lock initial margin for the opening portion while the order is open.
        if init_margin > ZERO:
            self.ledger.lock_funds("usdm", "USDT", init_margin, "ORDER_MARGIN_LOCK", str(order_id))
        row = {
            "market": "usdm", "order_id": order_id, "symbol": symbol, "client_order_id": coid, "side": o.side,
            "type": o.type, "orig_type": o.type, "time_in_force": o.time_in_force or "GTC",
            "price": o.price or ZERO, "stop_price": o.stop_price or ZERO, "orig_qty": o.qty, "quote_order_qty": ZERO,
            "executed_qty": ZERO, "cum_quote": ZERO, "avg_price": ZERO, "status": "NEW",
            "reduce_only": o.reduce_only, "close_position": o.close_position, "position_side": o.position_side,
            "working_type": o.working_type, "triggered": 0 if o.is_stop else 1, "locked_asset": "USDT",
            "locked_amount": init_margin, "created_at": now, "updated_at": now, "working_time": now,
            "session_id": self.ledger.current_session_id, "mid_at_arrival": mark, "source": source,
            "volume_since_place": ZERO, "extra": {"leverage": lev, "resp_type": o.new_order_resp_type},
        }
        self.ledger.insert_order(row)

        d = self.e.latency.draw_ms()
        self.feed.wait_until(now + d)
        self.ledger.update_order("usdm", order_id, latency_ms=d)
        with self.e.lock:
            if not o.is_stop:
                self._execute_now(order_id, o, sym)
        row = self.ledger.get_order("usdm", order_id) or row
        self.e.after_write("usdm", symbol)
        return self.order_view(row, sym)

    def _opening_qty(self, pos_amt: Decimal, side: str, qty: Decimal) -> Decimal:
        """How much of `qty` increases exposure (vs. reduces the existing position)."""
        if pos_amt == ZERO:
            return qty
        if (pos_amt > ZERO and side == "BUY") or (pos_amt < ZERO and side == "SELL"):
            return qty
        return max(ZERO, qty - abs(pos_amt))

    def _marketable(self, o: NormalizedOrder, best_bid: Decimal | None, best_ask: Decimal | None) -> bool:
        if o.price is None:
            return True
        if o.side == "BUY":
            return best_ask is not None and o.price >= best_ask
        return best_bid is not None and o.price <= best_bid

    def _would_trigger(self, o: NormalizedOrder, ref: Decimal | None) -> bool:
        if ref is None or o.stop_price is None:
            return False
        if o.type in ("STOP", "STOP_MARKET"):
            return ref >= o.stop_price if o.side == "BUY" else ref <= o.stop_price
        return ref <= o.stop_price if o.side == "BUY" else ref >= o.stop_price

    def _execute_now(self, order_id: int, o: NormalizedOrder, sym: dict[str, Any], as_type: str | None = None) -> None:
        typ = as_type or o.type
        bids, asks = self._book(o.symbol)
        side_levels = asks if o.side == "BUY" else bids
        step = B.lot_step(sym)
        limit = o.price if typ in ("LIMIT", "STOP", "TAKE_PROFIT") else None
        res = B.walk(side_levels, qty=o.qty, limit_price=limit, is_buy=(o.side == "BUY"), step=step)
        row = self.ledger.get_order("usdm", order_id)
        assert row is not None
        tif = o.time_in_force or "GTC"
        if tif == "FOK" and res.filled_qty < (o.qty or ZERO):
            self._finish(row, "EXPIRED")
            return
        if typ in MARKET_TYPES and res.filled_qty == ZERO:
            self._finish(row, "EXPIRED")
            return
        mark = self.feed.mark_price(o.symbol)
        for price, qty in res.fills:
            self._apply_fill(row, sym, price, qty, is_maker=False, mark=mark)
            row = self.ledger.get_order("usdm", order_id) or row
        remaining = (o.qty or ZERO) - res.filled_qty
        if remaining > ZERO:
            if typ in MARKET_TYPES or tif == "IOC":
                self._finish(row, "EXPIRED")
            else:
                self.ledger.update_order("usdm", order_id, status="PARTIALLY_FILLED" if res.filled_qty > ZERO else "NEW")
        else:
            self._finish(row, "FILLED")

    # ------------------------------------------------------------------ fills & settlement
    def _apply_fill(self, row: dict[str, Any], sym: dict[str, Any], price: Decimal, qty: Decimal,
                    is_maker: bool, mark: Decimal | None) -> None:
        symbol, oid = row["symbol"], row["order_id"]
        side = row["side"]
        pos = self.ledger.position(symbol)
        amt, entry, margin = dec(pos["position_amt"]), dec(pos["entry_price"]), dec(pos["isolated_margin"])
        lev = int(self.settings(symbol)["leverage"])
        signed = qty if side == "BUY" else -qty
        fee = (price * qty * self.e.fees.rate("usdm", is_maker)).quantize(D("0.00000001"))
        realized = ZERO

        # Split into reducing and opening parts.
        reduce_qty = ZERO
        if amt != ZERO and (amt > ZERO) != (signed > ZERO):
            reduce_qty = min(abs(amt), qty)
        open_qty = qty - reduce_qty

        if reduce_qty > ZERO:
            pnl = (price - entry) * reduce_qty * (1 if amt > ZERO else -1)
            realized += pnl
            # Return proportional isolated margin + pnl to the wallet.
            frac = reduce_qty / abs(amt)
            released = (margin * frac).quantize(D("0.00000001"))
            margin -= released
            self.ledger.credit("usdm", "USDT", released + pnl if released + pnl > ZERO else ZERO, "POSITION_REDUCE", str(oid))
            if released + pnl < ZERO:
                # Loss exceeded margin (should have liquidated first); absorb from wallet.
                self._debit_safely(-(released + pnl), "POSITION_LOSS", str(oid))
            self.ledger.add_income(symbol, "REALIZED_PNL", pnl, info=str(oid))
            new_amt = amt + (reduce_qty if signed > ZERO else -reduce_qty)
            if new_amt == ZERO:
                entry = ZERO
            amt = new_amt

        if open_qty > ZERO:
            need = (open_qty * price / lev).quantize(D("0.00000001"))
            still_locked = dec(row["locked_amount"])
            use_locked = min(need, max(ZERO, still_locked))
            if use_locked > ZERO:
                self.ledger.consume_locked("usdm", "USDT", use_locked, "OPEN_MARGIN", str(oid))
            if need - use_locked > ZERO:
                self._debit_safely(need - use_locked, "OPEN_MARGIN_EXTRA", str(oid))
            margin += need
            row["locked_amount"] = dstr(still_locked - use_locked)
            new_abs = abs(amt) + open_qty
            if amt == ZERO:
                entry = price
            else:
                entry = (entry * abs(amt) + price * open_qty) / new_abs
            amt = amt + (open_qty if signed > ZERO else -open_qty)

        self._debit_safely(fee, "COMMISSION", str(oid))
        self.ledger.add_income(symbol, "COMMISSION", -fee, info=str(oid))
        self.ledger.upsert_position(symbol, amt, entry, margin)

        exec_qty = dec(row["executed_qty"]) + qty
        cum_quote = dec(row["cum_quote"]) + price * qty
        avg = cum_quote / exec_qty if exec_qty > ZERO else ZERO
        self.ledger.update_order("usdm", oid, executed_qty=exec_qty, cum_quote=cum_quote, avg_price=avg,
                                 locked_amount=dec(row["locked_amount"]))
        row["executed_qty"], row["cum_quote"], row["avg_price"] = dstr(exec_qty), dstr(cum_quote), dstr(avg)
        slip = B.slippage_bps(price, mark, side == "BUY")
        self.ledger.add_fill(market="usdm", symbol=symbol, order_id=oid, side=side, price=price, qty=qty,
                             quote_qty=price * qty, commission=fee, commission_asset="USDT", is_maker=int(is_maker),
                             realized_pnl=realized, slippage_bps=str(slip) if slip is not None else None,
                             session_id=row.get("session_id"), source=row.get("source") or "agent")

    def _debit_safely(self, amount: Decimal, reason: str, ref: str) -> None:
        if amount <= ZERO:
            return
        free, _ = self.wallet()
        take = min(free, amount)
        if take > ZERO:
            self.ledger.debit("usdm", "USDT", take, reason, ref)
        if amount - take > ZERO:
            self.ledger.add_event("negative_balance", None, {"shortfall": str(amount - take), "reason": reason})

    def _finish(self, row: dict[str, Any], status: str) -> None:
        row = self.ledger.get_order("usdm", row["order_id"]) or row
        leftover = dec(row["locked_amount"])
        if leftover > ZERO:
            try:
                self.ledger.unlock_funds("usdm", "USDT", leftover, f"ORDER_{status}", str(row["order_id"]))
            except Exception:
                pass
        self.ledger.update_order("usdm", row["order_id"], status=status, locked_amount=ZERO)

    # ------------------------------------------------------------------ cancel
    def cancel(self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None) -> dict[str, Any]:
        symbol = symbol.upper()
        sym = self._sym(symbol)
        if not sym:
            raise E.err(E.BAD_SYMBOL)
        if order_id is None and orig_client_order_id is None:
            raise E.mandatory("orderId")
        row = self.ledger.get_order("usdm", order_id=order_id, client_order_id=orig_client_order_id, symbol=symbol)
        if row is None or row["status"] not in ("NEW", "PARTIALLY_FILLED"):
            raise E.err(E.UNKNOWN_ORDER)
        with self.e.lock:
            self._finish(row, "CANCELED")
        row = self.ledger.get_order("usdm", row["order_id"]) or row
        self.e.after_write("usdm", symbol)
        return self.order_view(row, sym)

    def cancel_all(self, symbol: str) -> dict[str, Any]:
        symbol = symbol.upper()
        if not self._sym(symbol):
            raise E.err(E.BAD_SYMBOL)
        with self.e.lock:
            for row in self.ledger.open_orders("usdm", symbol):
                self._finish(row, "CANCELED")
        self.e.after_write("usdm", symbol)
        return {"code": 200, "msg": "The operation of cancel all open order is done."}

    # ------------------------------------------------------------------ stream-driven
    def on_trade(self, symbol: str, price: Decimal, qty: Decimal, ts: int) -> None:
        rows = self.ledger.open_orders("usdm", symbol)
        if not rows:
            return
        sym = self._sym(symbol)
        if not sym:
            return
        mark = self.feed.mark_price(symbol)
        qf = D(str(self.e.cfg.engine.queue_factor))
        for row in rows:
            typ = row["type"]
            if not row["triggered"]:
                if row["working_type"] != "CONTRACT_PRICE":
                    continue
                if self._stop_hit(row, price):
                    self._trigger(row, sym, ts)
                continue
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
            self.ledger.update_order("usdm", row["order_id"], volume_since_place=vol)
            fill_qty = remaining if vol >= remaining * qf else (min(remaining, qty) if strictly_better else ZERO)
            step = B.lot_step(sym)
            fill_qty = B.quantize_down(fill_qty, step) if step > ZERO else fill_qty
            if fill_qty <= ZERO:
                continue
            self._apply_fill(row, sym, limit, fill_qty, is_maker=True, mark=mark)
            row = self.ledger.get_order("usdm", row["order_id"]) or row
            if dec(row["executed_qty"]) >= dec(row["orig_qty"]):
                self._finish(row, "FILLED")
            else:
                self.ledger.update_order("usdm", row["order_id"], status="PARTIALLY_FILLED")
            self.e.after_write("usdm", symbol)

    def _stop_hit(self, row: dict[str, Any], ref: Decimal) -> bool:
        stop = dec(row["stop_price"])
        if row["type"] in ("STOP", "STOP_MARKET"):
            return ref >= stop if row["side"] == "BUY" else ref <= stop
        return ref <= stop if row["side"] == "BUY" else ref >= stop

    def _trigger(self, row: dict[str, Any], sym: dict[str, Any], ts: int) -> None:
        self.ledger.update_order("usdm", row["order_id"], triggered=1, working_time=ts)
        self.ledger.add_event("stop_triggered", row["symbol"], {"order_id": row["order_id"], "type": row["type"]})
        o = NormalizedOrder(market="usdm", symbol=row["symbol"], side=row["side"], type=row["type"],
                            time_in_force=row["time_in_force"], qty=dec(row["orig_qty"]),
                            price=dec(row["price"]) if dec(row["price"]) > ZERO else None,
                            stop_price=dec(row["stop_price"]), reduce_only=bool(row["reduce_only"]),
                            close_position=bool(row["close_position"]))
        as_type = "MARKET" if row["type"] in ("STOP_MARKET", "TAKE_PROFIT_MARKET") else "LIMIT"
        self._execute_now(row["order_id"], o, sym, as_type=as_type)
        self.e.after_write("usdm", row["symbol"])

    def on_mark(self, symbol: str, mark: Decimal, ts: int) -> None:
        sym = self._sym(symbol)
        if not sym:
            return
        # MARK_PRICE-triggered stops
        for row in self.ledger.open_orders("usdm", symbol):
            if not row["triggered"] and row["working_type"] == "MARK_PRICE" and self._stop_hit(row, mark):
                self._trigger(row, sym, ts)
        self._apply_funding(symbol, mark, ts)
        self._check_liquidation(symbol, mark, ts)

    def _apply_funding(self, symbol: str, mark: Decimal, ts: int) -> None:
        pos = self.ledger.position(symbol)
        amt = dec(pos["position_amt"])
        boundary = (ts // FUNDING_PERIOD_MS) * FUNDING_PERIOD_MS
        last = self._last_funding.get(symbol)
        if last is None:
            self._last_funding[symbol] = boundary
            return
        if boundary <= last:
            return
        self._last_funding[symbol] = boundary
        if amt == ZERO:
            return
        rate = self.feed.funding_rate(symbol)
        payment = (amt * mark * rate).quantize(D("0.00000001"))  # long pays when rate > 0
        margin = dec(pos["isolated_margin"]) - payment
        self.ledger.upsert_position(symbol, amt, dec(pos["entry_price"]), margin)
        self.ledger.add_income(symbol, "FUNDING_FEE", -payment, info=f"rate={rate}")
        self.ledger.add_event("funding", symbol, {"rate": str(rate), "payment": str(-payment), "mark": str(mark)})

    def _check_liquidation(self, symbol: str, mark: Decimal, ts: int) -> None:
        pos = self.ledger.position(symbol)
        amt = dec(pos["position_amt"])
        if amt == ZERO:
            return
        margin = dec(pos["isolated_margin"])
        upnl = (mark - dec(pos["entry_price"])) * amt
        maint = abs(amt) * mark * D(str(self.cfg.maintenance_rate))
        if margin + upnl > maint:
            return
        # Liquidate: close at mark, isolated margin is lost (clearance fee on top if configured).
        liq_fee = (abs(amt) * mark * D(str(self.cfg.liquidation_fee))).quantize(D("0.00000001"))
        with self.e.lock:
            for row in self.ledger.open_orders("usdm", symbol):
                self._finish(row, "EXPIRED")
            # Binance closes the position at the bankruptcy price: the whole isolated margin is lost and any
            # remainder (plus the clearance fee) goes to the insurance fund. Nothing returns to the wallet.
            remaining = margin + upnl - liq_fee
            self.ledger.upsert_position(symbol, ZERO, ZERO, ZERO)
            self.ledger.add_income(symbol, "REALIZED_PNL", -margin, info="LIQUIDATION")
            self.ledger.add_income(symbol, "INSURANCE_CLEAR", -(max(remaining, ZERO) + liq_fee), info="LIQUIDATION")
            self.ledger.add_fill(market="usdm", symbol=symbol, order_id=0, side="SELL" if amt > ZERO else "BUY",
                                 price=mark, qty=abs(amt), quote_qty=abs(amt) * mark, commission=liq_fee,
                                 commission_asset="USDT", is_maker=0, realized_pnl=upnl, slippage_bps=None,
                                 source="liquidation")
            self.ledger.add_event("LIQUIDATION", symbol, {"mark": str(mark), "position_amt": str(amt),
                                                          "entry_price": pos["entry_price"], "isolated_margin": str(margin),
                                                          "unrealized": str(upnl), "lost": str(margin), "leverage": self.settings(symbol)["leverage"]})
        self.e.after_write("usdm", symbol)

    # ------------------------------------------------------------------ views (Binance shapes)
    def order_view(self, row: dict[str, Any], sym: dict[str, Any] | None = None) -> dict[str, Any]:
        sym = sym or self._sym(row["symbol"]) or {}
        pp, qp = self._prec(sym)
        return {
            "orderId": row["order_id"], "symbol": row["symbol"], "status": row["status"],
            "clientOrderId": row["client_order_id"], "price": dstr(row["price"], pp), "avgPrice": dstr(row["avg_price"], pp),
            "origQty": dstr(row["orig_qty"], qp), "executedQty": dstr(row["executed_qty"], qp),
            "cumQty": dstr(row["executed_qty"], qp), "cumQuote": dstr(row["cum_quote"], pp),
            "timeInForce": row["time_in_force"] or "GTC", "type": row["type"], "reduceOnly": bool(row["reduce_only"]),
            "closePosition": bool(row["close_position"]), "side": row["side"], "positionSide": row["position_side"],
            "stopPrice": dstr(row["stop_price"], pp), "workingType": row["working_type"], "priceProtect": False,
            "origType": row["orig_type"] or row["type"], "priceMatch": "NONE", "selfTradePreventionMode": "EXPIRE_MAKER",
            "goodTillDate": 0, "time": row["created_at"], "updateTime": row["updated_at"],
        }

    def position_view(self, symbol: str | None = None) -> list[dict[str, Any]]:
        rows = self.ledger.positions("usdm", nonzero=False)
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol.upper()] or [self.ledger.position(symbol.upper())]
        out = []
        for r in rows:
            amt = dec(r["position_amt"])
            if amt == ZERO and not symbol:
                continue
            sym = self._sym(r["symbol"]) or {}
            pp, qp = self._prec(sym)
            mark = self.feed.mark_price(r["symbol"]) or ZERO
            st = self.settings(r["symbol"])
            upnl = (mark - dec(r["entry_price"])) * amt
            out.append({
                "symbol": r["symbol"], "positionAmt": dstr(amt, qp), "entryPrice": dstr(r["entry_price"], pp),
                "breakEvenPrice": dstr(r["entry_price"], pp), "markPrice": dstr(mark, 8),
                "unRealizedProfit": dstr(upnl, 8), "liquidationPrice": dstr(self.liquidation_price(r["symbol"]), pp),
                "leverage": str(st["leverage"]), "maxNotionalValue": "20000000", "marginType": st["margin_type"].lower(),
                "isolatedMargin": dstr(dec(r["isolated_margin"]), 8), "isAutoAddMargin": "false", "positionSide": "BOTH",
                "notional": dstr(amt * mark, 8), "isolatedWallet": dstr(dec(r["isolated_margin"]), 8),
                "updateTime": r.get("updated_at") or 0, "bidNotional": "0", "askNotional": "0",
            })
        return out

    def balance_view(self) -> list[dict[str, Any]]:
        free, locked = self.wallet()
        upnl = sum((self.unrealized(p["symbol"]) for p in self.ledger.positions("usdm")), ZERO)
        return [{
            "accountAlias": "twinTwin", "asset": "USDT", "balance": dstr(free + locked), "crossWalletBalance": dstr(free + locked),
            "crossUnPnl": "0.00000000", "availableBalance": dstr(free), "maxWithdrawAmount": dstr(free),
            "marginAvailable": True, "updateTime": self.e.now(), "_isolatedUnPnl": dstr(upnl),
        }]

    def account_view(self) -> dict[str, Any]:
        free, locked = self.wallet()
        positions = self.ledger.positions("usdm")
        upnl = sum((self.unrealized(p["symbol"]) for p in positions), ZERO)
        iso_margin = sum((dec(p["isolated_margin"]) for p in positions), ZERO)
        wallet = free + locked
        fees = self.e.fees
        return {
            "feeTier": 0, "feeBurn": False, "canTrade": True, "canDeposit": True, "canWithdraw": False,
            "updateTime": 0, "multiAssetsMargin": False, "tradeGroupId": -1,
            "totalInitialMargin": dstr(iso_margin), "totalMaintMargin": dstr(sum((abs(dec(p["position_amt"])) * (self.feed.mark_price(p["symbol"]) or ZERO) * D(str(self.cfg.maintenance_rate)) for p in positions), ZERO)),
            "totalWalletBalance": dstr(wallet + iso_margin), "totalUnrealizedProfit": dstr(upnl),
            "totalMarginBalance": dstr(wallet + iso_margin + upnl), "totalPositionInitialMargin": dstr(iso_margin),
            "totalOpenOrderInitialMargin": dstr(locked), "totalCrossWalletBalance": dstr(wallet),
            "totalCrossUnPnl": "0.00000000", "availableBalance": dstr(free), "maxWithdrawAmount": dstr(free),
            "assets": [{"asset": "USDT", "walletBalance": dstr(wallet + iso_margin), "unrealizedProfit": dstr(upnl),
                        "marginBalance": dstr(wallet + iso_margin + upnl), "maintMargin": "0", "initialMargin": dstr(iso_margin),
                        "positionInitialMargin": dstr(iso_margin), "openOrderInitialMargin": dstr(locked),
                        "crossWalletBalance": dstr(wallet), "crossUnPnl": "0", "availableBalance": dstr(free),
                        "maxWithdrawAmount": dstr(free), "marginAvailable": True, "updateTime": self.e.now()}],
            "positions": self.position_view(),
            "_twin": {"makerFee": dstr(fees.rate("usdm", True)), "takerFee": dstr(fees.rate("usdm", False))},
        }

    def trades_view(self, symbol: str | None = None, order_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        out = []
        for f in self.ledger.fills("usdm", symbol, order_id, limit):
            sym = self._sym(f["symbol"]) or {}
            pp, qp = self._prec(sym)
            out.append({
                "symbol": f["symbol"], "id": f["fill_id"], "orderId": f["order_id"], "side": f["side"],
                "price": dstr(f["price"], pp), "qty": dstr(f["qty"], qp), "realizedPnl": dstr(f["realized_pnl"]),
                "marginAsset": "USDT", "quoteQty": dstr(f["quote_qty"], pp), "commission": dstr(f["commission"]),
                "commissionAsset": "USDT", "time": f["ts"], "positionSide": "BOTH", "buyer": f["side"] == "BUY",
                "maker": bool(f["is_maker"]),
            })
        return out

    def income_view(self, symbol: str | None = None, income_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [{"symbol": r["symbol"] or "", "incomeType": r["income_type"], "income": dstr(r["income"]), "asset": r["asset"],
                 "info": r["info"] or "", "time": r["ts"], "tranId": r["id"], "tradeId": ""}
                for r in self.ledger.incomes(symbol, income_type, limit)]
