"""Engine facade: wires ledger + feed + fill engines; equity, transfers, convert, sessions."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from rehearsal.config import Config
from rehearsal.engine.fees import FeeSchedule
from rehearsal.engine.fills_spot import SpotEngine
from rehearsal.engine.fills_usdm import UsdmEngine
from rehearsal.engine.latency import LatencyModel
from rehearsal.engine.ledger import Ledger, dec, dstr
from rehearsal.market.clock import Clock
from rehearsal.market.feed import MarketFeed
from rehearsal.policy import Policy
from rehearsal.server import errors as E

log = logging.getLogger("rehearsal.engine")
D = Decimal
ZERO = D(0)

TRANSFER_TYPES = {
    "MAIN_UMFUTURE": ("spot", "usdm"), "UMFUTURE_MAIN": ("usdm", "spot"),
    "MAIN_MARGIN": ("spot", "margin"), "MARGIN_MAIN": ("margin", "spot"),
    "MAIN_CMFUTURE": ("spot", "coinm"), "CMFUTURE_MAIN": ("coinm", "spot"),
}


class Engine:
    def __init__(self, cfg: Config, ledger: Ledger, feed: MarketFeed, clock: Clock):
        self.cfg = cfg
        self.ledger = ledger
        self.feed = feed
        self.clock = clock
        self.fees = FeeSchedule(cfg.engine.fees)
        self.latency = LatencyModel(cfg.engine.latency, cfg.engine.seed)
        self.policy = Policy(cfg.policy, ledger)
        self.lock = threading.RLock()
        self.spot = SpotEngine(self)
        self.usdm = UsdmEngine(self)
        self._last_snapshot = 0.0
        self._coid_seq = 0
        feed.on_trade(self._on_trade)
        feed.on_mark(self._on_mark)

    # ------------------------------------------------------------------ basics
    def now(self) -> int:
        return self.clock.now_ms()

    def gen_client_order_id(self) -> str:
        self._coid_seq += 1
        return f"twin_{uuid.uuid4().hex[:16]}"

    def reset(self, keep_history: bool = False) -> None:
        with self.lock:
            self.ledger.reset(self.cfg.engine.initial_balances, keep_history=keep_history)
            self.usdm._last_funding.clear()
        self.snapshot_equity(force=True)

    # ------------------------------------------------------------------ feed callbacks
    def _on_trade(self, market: str, symbol: str, price: Decimal, qty: Decimal, ts: int, bim: bool) -> None:
        with self.lock:
            if market == "spot":
                self.spot.on_trade(symbol, price, qty, ts, bim)
            else:
                self.usdm.on_trade(symbol, price, qty, ts)
        self.snapshot_equity()

    def _on_mark(self, symbol: str, mark: Decimal, ts: int) -> None:
        with self.lock:
            self.usdm.on_mark(symbol, mark, ts)
        self.snapshot_equity()

    def after_write(self, market: str, symbol: str) -> None:
        self.snapshot_equity(force=True)

    # ------------------------------------------------------------------ equity
    def price_in_usdt(self, asset: str) -> Decimal | None:
        asset = asset.upper()
        if asset in ("USDT", "USDC", "FDUSD", "BUSD", "TUSD", "USD1"):
            return D(1)
        if self.feed.symbol_info("spot", f"{asset}USDT"):
            return self.feed.last_price("spot", f"{asset}USDT")
        return None

    def spot_value(self) -> tuple[Decimal, list[dict[str, Any]]]:
        total = ZERO
        detail = []
        for b in self.ledger.balances("spot"):
            amt = dec(b["free"]) + dec(b["locked"])
            if amt == ZERO:
                continue
            px = self.price_in_usdt(b["asset"])
            val = amt * px if px is not None else ZERO
            total += val
            detail.append({"asset": b["asset"], "free": b["free"], "locked": b["locked"], "price": dstr(px) if px else None,
                           "value_usdt": dstr(val)})
        return total, detail

    def gross_exposure(self) -> Decimal:
        spot_val, detail = self.spot_value()
        non_stable = sum((dec(d["value_usdt"]) for d in detail if self.price_in_usdt(d["asset"]) != D(1)), ZERO)
        fut = sum((abs(dec(p["position_amt"])) * (self.feed.mark_price(p["symbol"]) or ZERO)
                   for p in self.ledger.positions("usdm")), ZERO)
        return non_stable + fut

    def equity(self) -> dict[str, Any]:
        spot_val, detail = self.spot_value()
        free, locked = self.ledger.balance("usdm", "USDT")
        positions = self.ledger.positions("usdm")
        iso = sum((dec(p["isolated_margin"]) for p in positions), ZERO)
        upnl = sum((self.usdm.unrealized(p["symbol"]) for p in positions), ZERO)
        usdm_wallet = free + locked + iso
        eq = spot_val + usdm_wallet + upnl
        return {"equity": eq, "spot_value": spot_val, "usdm_wallet": usdm_wallet, "usdm_unrealized": upnl,
                "spot_detail": detail}

    def snapshot_equity(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_snapshot < 1.0:
            return
        self._last_snapshot = now
        try:
            eq = self.equity()
            self.ledger.snapshot_equity(eq["equity"], eq["spot_value"], eq["usdm_wallet"], eq["usdm_unrealized"])
        except Exception as e:  # pragma: no cover
            log.debug("snapshot failed: %s", e)

    # ------------------------------------------------------------------ transfer
    def transfer(self, type_: Any, asset: Any, amount: Any) -> dict[str, Any]:
        if not type_:
            raise E.mandatory("type")
        if not asset:
            raise E.mandatory("asset")
        t = str(type_).upper()
        if t not in TRANSFER_TYPES:
            raise E.err(E.BAD_PARAM, "type")
        src, dst = TRANSFER_TYPES[t]
        if src not in ("spot", "usdm") or dst not in ("spot", "usdm"):
            raise E.err(E.TWIN_UNSUPPORTED, f"transfer type {t} (twin supports spot<->usdm)")
        try:
            amt = D(str(amount))
        except Exception:
            raise E.err(E.BAD_PARAM, "amount")
        if amt <= ZERO:
            raise E.err(E.BAD_PARAM, "amount")
        a = str(asset).upper()
        if dst == "usdm" and a != "USDT":
            raise E.err(E.TRANSFER_BAD_ASSET)
        with self.lock:
            free, _ = self.ledger.balance(src, a)
            if free < amt:
                raise E.err(E.TRANSFER_INSUFFICIENT)
            self.ledger.debit(src, a, amt, "TRANSFER_OUT", t)
            self.ledger.credit(dst, a, amt, "TRANSFER_IN", t)
            tran_id = self.ledger.add_transfer(a, amt, src, dst, t)
        self.ledger.add_event("transfer", None, {"type": t, "asset": a, "amount": str(amt)})
        self.snapshot_equity(force=True)
        return {"tranId": tran_id}

    def transfer_history(self, type_: str | None = None, limit: int = 10) -> dict[str, Any]:
        rows = self.ledger.transfers(limit)
        if type_:
            rows = [r for r in rows if r["type"] == str(type_).upper()]
        return {"total": len(rows), "rows": [{"asset": r["asset"], "amount": dstr(r["amount"]), "type": r["type"],
                                              "status": r["status"], "tranId": r["tran_id"], "timestamp": r["ts"]}
                                             for r in rows]}

    def wallet_balance_view(self) -> list[dict[str, Any]]:
        spot_val, _ = self.spot_value()
        free, locked = self.ledger.balance("usdm", "USDT")
        iso = sum((dec(p["isolated_margin"]) for p in self.ledger.positions("usdm")), ZERO)
        btc = self.feed.last_price("spot", "BTCUSDT") or D(1)
        return [
            {"activate": True, "balance": dstr(spot_val / btc), "walletName": "Spot"},
            {"activate": True, "balance": dstr((free + locked + iso) / btc), "walletName": "USDⓈ-M Futures"},
            {"activate": False, "balance": "0", "walletName": "COIN-M Futures"},
            {"activate": False, "balance": "0", "walletName": "Cross Margin"},
            {"activate": False, "balance": "0", "walletName": "Isolated Margin"},
            {"activate": False, "balance": "0", "walletName": "Earn"},
            {"activate": False, "balance": "0", "walletName": "Funding"},
        ]

    # ------------------------------------------------------------------ convert (spot wallet, quote against the book)
    def convert_quote(self, from_asset: Any, to_asset: Any, from_amount: Any = None, to_amount: Any = None,
                      wallet_type: str = "SPOT", valid_time: str = "10s") -> dict[str, Any]:
        if not from_asset:
            raise E.mandatory("fromAsset")
        if not to_asset:
            raise E.mandatory("toAsset")
        fa, ta = str(from_asset).upper(), str(to_asset).upper()
        if not from_amount and not to_amount:
            raise E.mandatory("fromAmount")
        symbol, direction = self._convert_pair(fa, ta)
        if symbol is None:
            raise E.err(E.CONVERT_BAD_PAIR)
        px = self.feed.mid("spot", symbol)
        if px is None:
            raise E.err(E.TWIN_NO_MARKET_DATA, symbol)
        # ratio = units of toAsset per unit of fromAsset; convert charges an implicit ~0.1% spread.
        spread = D("0.001")
        if direction == "sell_base":  # from = base, to = quote
            ratio = px * (1 - spread)
        else:  # from = quote, to = base
            ratio = (1 / px) * (1 - spread)
        if from_amount:
            fam = D(str(from_amount))
            tam = (fam * ratio).quantize(D("0.00000001"))
        else:
            tam = D(str(to_amount))
            fam = (tam / ratio).quantize(D("0.00000001"))
        secs = {"10s": 10, "30s": 30, "1m": 60, "2m": 120}.get(str(valid_time), 10)
        qid = str(self.now()) + uuid.uuid4().hex[:6]
        self.ledger.add_quote({"quote_id": qid, "from_asset": fa, "to_asset": ta, "from_amount": fam, "to_amount": tam,
                               "ratio": ratio, "valid_until": self.now() + secs * 1000, "wallet_type": wallet_type})
        return {"quoteId": qid, "ratio": dstr(ratio), "inverseRatio": dstr(1 / ratio), "validTimestamp": self.now() + secs * 1000,
                "toAmount": dstr(tam), "fromAmount": dstr(fam)}

    def _convert_pair(self, fa: str, ta: str) -> tuple[str | None, str]:
        if self.feed.symbol_info("spot", fa + ta):
            return fa + ta, "sell_base"
        if self.feed.symbol_info("spot", ta + fa):
            return ta + fa, "buy_base"
        return None, ""

    def convert_accept(self, quote_id: Any) -> dict[str, Any]:
        if not quote_id:
            raise E.mandatory("quoteId")
        q = self.ledger.get_quote(str(quote_id))
        if q is None:
            raise E.err(E.CONVERT_QUOTE_EXPIRED)
        if q["status"] != "QUOTED":
            raise E.err(E.CONVERT_QUOTE_EXPIRED)
        if self.now() > int(q["valid_until"]):
            self.ledger.update_quote(q["quote_id"], status="EXPIRED")
            raise E.err(E.CONVERT_QUOTE_EXPIRED)
        fam, tam = dec(q["from_amount"]), dec(q["to_amount"])
        with self.lock:
            free, _ = self.ledger.balance("spot", q["from_asset"])
            if free < fam:
                raise E.err(E.INSUFFICIENT_BALANCE)
            self.ledger.debit("spot", q["from_asset"], fam, "CONVERT_OUT", q["quote_id"])
            self.ledger.credit("spot", q["to_asset"], tam, "CONVERT_IN", q["quote_id"])
            order_id = str(self.ledger.next_order_id("spot"))
            self.ledger.update_quote(q["quote_id"], status="SUCCESS", order_id=order_id)
            symbol, _ = self._convert_pair(q["from_asset"], q["to_asset"])
            self.ledger.add_fill(market="spot", symbol=symbol or f"{q['from_asset']}{q['to_asset']}", order_id=int(order_id),
                                 side="CONVERT", price=dec(q["ratio"]), qty=fam, quote_qty=tam, commission=ZERO,
                                 commission_asset=q["to_asset"], is_maker=0, source="convert")
        self.ledger.add_event("convert", symbol, {"from": q["from_asset"], "to": q["to_asset"], "fromAmount": str(fam), "toAmount": str(tam)})
        self.snapshot_equity(force=True)
        return {"orderId": order_id, "createTime": self.now(), "orderStatus": "SUCCESS"}

    def convert_status(self, order_id: Any = None, quote_id: Any = None) -> dict[str, Any]:
        q = self.ledger.get_quote(str(quote_id)) if quote_id else (self.ledger.get_quote_by_order(str(order_id)) if order_id else None)
        if q is None:
            raise E.err(E.NO_SUCH_ORDER)
        return {"orderId": q["order_id"], "orderStatus": q["status"], "fromAsset": q["from_asset"], "fromAmount": dstr(q["from_amount"]),
                "toAsset": q["to_asset"], "toAmount": dstr(q["to_amount"]), "ratio": dstr(q["ratio"]),
                "inverseRatio": dstr(1 / dec(q["ratio"])), "createTime": q["ts"]}

    def convert_exchange_info(self, from_asset: str | None = None, to_asset: str | None = None) -> list[dict[str, Any]]:
        info = self.feed.exchange_info("spot")
        out = []
        for s in info.get("symbols", [])[:500]:
            b, q = s.get("baseAsset"), s.get("quoteAsset")
            for fa, ta in ((b, q), (q, b)):
                if from_asset and fa != from_asset.upper():
                    continue
                if to_asset and ta != to_asset.upper():
                    continue
                out.append({"fromAsset": fa, "toAsset": ta, "fromAssetMinAmount": "0.00000001", "fromAssetMaxAmount": "1000000",
                            "toAssetMinAmount": "0.00000001", "toAssetMaxAmount": "1000000"})
        return out[:200]

    # ------------------------------------------------------------------ sessions
    def start_session(self, session_id: str | None = None, run_id: str | None = None, label: str | None = None,
                      reset: bool = True, meta: dict[str, Any] | None = None) -> str:
        sid = session_id or f"s_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        if reset:
            self.reset(keep_history=True)
        eq = self.equity()["equity"]
        self.ledger.start_session(sid, run_id, label, eq, meta)
        self.ledger.add_event("session_start", None, {"session_id": sid, "run_id": run_id})
        self.snapshot_equity(force=True)
        return sid

    def end_session(self, session_id: str, status: str = "DONE") -> dict[str, Any]:
        eq = self.equity()["equity"]
        stables = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD"}
        open_orders = len(self.ledger.open_orders("spot")) + len(self.ledger.open_orders("usdm"))
        positions = len(self.ledger.positions("usdm"))
        holdings = [b for b in self.ledger.balances("spot", omit_zero=True) if b["asset"] not in stables]
        end_state = {"open_orders_at_end": open_orders, "positions_at_end": positions, "spot_holdings_at_end": len(holdings),
                     "flattened": positions == 0 and not holdings}
        self.ledger.end_session(session_id, eq, status, meta_update=end_state)
        self.ledger.add_event("session_end", None, {"session_id": session_id, "final_equity": str(eq)}, session_id=session_id)
        return {"session_id": session_id, "final_equity": dstr(eq)}

    def close(self) -> None:
        self.feed.close()
        self.ledger.close()


def _fixture_start_ms(cfg: Config) -> int:
    """Replay clocks start at the fixture's first timestamp (meta.started) so nothing waits on wall time."""
    import json

    d = cfg.path(cfg.market.replay_fixture) if cfg.market.replay_fixture else None
    if d and (d / "meta.json").exists():
        try:
            return int(json.loads((d / "meta.json").read_text()).get("started") or 0)
        except Exception:
            pass
    return 0


def build_engine(cfg: Config, db_path: str | Path | None = None) -> Engine:
    """Construct ledger + feed + engine from config (does not start the feed)."""
    virtual = cfg.market.mode == "replay"
    clock = Clock(virtual=virtual, start_ms=_fixture_start_ms(cfg) if virtual else None)
    feed = MarketFeed(cfg.market, clock, root=cfg.root)
    db = Path(db_path) if db_path else cfg.path(cfg.engine.db_path)
    ledger = Ledger(db or ":memory:", clock)
    engine = Engine(cfg, ledger, feed, clock)
    if ledger.kv_get("reset_at") is None:
        engine.reset()
    return engine
