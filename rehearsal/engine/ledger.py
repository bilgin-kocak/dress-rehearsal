"""Virtual ledger: wallets, orders, fills, positions, transfers, tool calls, sessions, events.

SQLite-backed. All money values are Decimals stored as TEXT so balance invariants hold exactly.
Every balance change is written to `balance_changes` with a reason.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from rehearsal.market.clock import Clock

D = Decimal
ZERO = D(0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
  market TEXT NOT NULL, asset TEXT NOT NULL,
  free TEXT NOT NULL DEFAULT '0', locked TEXT NOT NULL DEFAULT '0',
  PRIMARY KEY (market, asset));
CREATE TABLE IF NOT EXISTS balance_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, market TEXT, asset TEXT,
  free_delta TEXT, locked_delta TEXT, reason TEXT, ref TEXT, session_id TEXT);
CREATE TABLE IF NOT EXISTS orders (
  market TEXT NOT NULL, order_id INTEGER NOT NULL, symbol TEXT NOT NULL,
  client_order_id TEXT, side TEXT, type TEXT, orig_type TEXT, time_in_force TEXT,
  price TEXT, stop_price TEXT, orig_qty TEXT, quote_order_qty TEXT,
  executed_qty TEXT DEFAULT '0', cum_quote TEXT DEFAULT '0', avg_price TEXT DEFAULT '0',
  status TEXT, reduce_only INTEGER DEFAULT 0, close_position INTEGER DEFAULT 0,
  position_side TEXT DEFAULT 'BOTH', working_type TEXT DEFAULT 'CONTRACT_PRICE',
  triggered INTEGER DEFAULT 0, locked_asset TEXT, locked_amount TEXT DEFAULT '0',
  created_at INTEGER, updated_at INTEGER, working_time INTEGER,
  session_id TEXT, mid_at_arrival TEXT, latency_ms INTEGER, source TEXT DEFAULT 'agent',
  volume_since_place TEXT DEFAULT '0', extra TEXT,
  PRIMARY KEY (market, order_id));
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(market, status);
CREATE TABLE IF NOT EXISTS fills (
  fill_id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT, symbol TEXT, order_id INTEGER,
  side TEXT, price TEXT, qty TEXT, quote_qty TEXT, commission TEXT, commission_asset TEXT,
  is_maker INTEGER, ts INTEGER, realized_pnl TEXT DEFAULT '0', slippage_bps TEXT,
  session_id TEXT, source TEXT DEFAULT 'agent');
CREATE TABLE IF NOT EXISTS positions (
  market TEXT NOT NULL, symbol TEXT NOT NULL, position_amt TEXT DEFAULT '0',
  entry_price TEXT DEFAULT '0', isolated_margin TEXT DEFAULT '0',
  updated_at INTEGER, PRIMARY KEY (market, symbol));
CREATE TABLE IF NOT EXISTS symbol_settings (
  market TEXT NOT NULL, symbol TEXT NOT NULL, leverage INTEGER, margin_type TEXT,
  PRIMARY KEY (market, symbol));
CREATE TABLE IF NOT EXISTS income (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, market TEXT, symbol TEXT,
  income_type TEXT, income TEXT, asset TEXT, info TEXT, tran_id INTEGER, session_id TEXT);
CREATE TABLE IF NOT EXISTS transfers (
  tran_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, asset TEXT, amount TEXT,
  from_wallet TEXT, to_wallet TEXT, type TEXT, status TEXT, session_id TEXT);
CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, session_id TEXT, tool TEXT,
  category TEXT, args TEXT, result_status TEXT, result TEXT, latency_ms INTEGER,
  error_code INTEGER, mid_at_arrival TEXT, source TEXT DEFAULT 'agent');
CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY, run_id TEXT, label TEXT, started_at INTEGER, ended_at INTEGER,
  initial_equity TEXT, final_equity TEXT, status TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, session_id TEXT, kind TEXT,
  symbol TEXT, detail TEXT, source TEXT DEFAULT 'agent');
CREATE TABLE IF NOT EXISTS equity_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, session_id TEXT, equity TEXT,
  spot_value TEXT, usdm_wallet TEXT, usdm_unrealized TEXT, source TEXT DEFAULT 'paper');
CREATE TABLE IF NOT EXISTS shadow_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, tool TEXT, args TEXT,
  real_result TEXT, sim_result TEXT, divergence TEXT, is_write INTEGER);
CREATE TABLE IF NOT EXISTS convert_quotes (
  quote_id TEXT PRIMARY KEY, ts INTEGER, from_asset TEXT, to_asset TEXT, from_amount TEXT,
  to_amount TEXT, ratio TEXT, valid_until INTEGER, status TEXT, order_id TEXT, wallet_type TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def dstr(x: Decimal | str | float | int | None, places: int = 8) -> str:
    """Format a number like Binance does (fixed decimals, no exponent)."""
    if x is None:
        return "0"
    d = D(str(x)) if not isinstance(x, Decimal) else x
    q = D(1).scaleb(-places)
    return format(d.quantize(q), "f")


def dec(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None or x == "":
        return ZERO
    return D(str(x))


class InsufficientBalance(Exception):
    pass


class Ledger:
    def __init__(self, db_path: str | Path, clock: Clock):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.current_session_id: str | None = None

    # ------------------------------------------------------------------ helpers
    def now(self) -> int:
        return self.clock.now_ms()

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def _row(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self._row("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def kv_set(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # ------------------------------------------------------------------ reset
    def reset(self, initial_balances: dict[str, dict[str, float]], keep_calls: bool = False, keep_history: bool = False) -> None:
        """Recreate the trading state from initial_balances.

        keep_history=True (new rehearsal session): wallets/positions/leverage are reset and open orders
        expire, but orders, fills, tool calls, sessions, events and equity snapshots stay (they are keyed
        by session_id and feed the report). keep_history=False wipes everything (`rehearsal reset`).
        """
        with self.lock:
            if keep_history:
                self.conn.execute("UPDATE orders SET status='EXPIRED', locked_amount='0', updated_at=? "
                                  "WHERE status IN ('NEW','PARTIALLY_FILLED')", (self.now(),))
                for t in ("wallets", "positions", "symbol_settings", "convert_quotes"):
                    self.conn.execute(f"DELETE FROM {t}")
            else:
                tables = [
                    "wallets", "balance_changes", "orders", "fills", "positions", "symbol_settings",
                    "income", "transfers", "events", "equity_snapshots", "convert_quotes",
                ]
                if not keep_calls:
                    tables += ["tool_calls", "sessions", "shadow_events"]
                for t in tables:
                    self.conn.execute(f"DELETE FROM {t}")
                self.conn.execute("DELETE FROM kv WHERE key LIKE 'order_seq:%'")
            for market, assets in initial_balances.items():
                for asset, amt in assets.items():
                    self.conn.execute(
                        "INSERT OR REPLACE INTO wallets(market, asset, free, locked) VALUES (?,?,?,'0')",
                        (market, asset.upper(), dstr(amt)),
                    )
                    self.conn.execute(
                        "INSERT INTO balance_changes(ts, market, asset, free_delta, locked_delta, reason, ref, session_id)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (self.now(), market, asset.upper(), dstr(amt), "0", "INITIAL", None, self.current_session_id),
                    )
            self.kv_set("reset_at", self.now())

    # ------------------------------------------------------------------ wallets
    def balance(self, market: str, asset: str) -> tuple[Decimal, Decimal]:
        r = self._row("SELECT free, locked FROM wallets WHERE market=? AND asset=?", (market, asset.upper()))
        if not r:
            return ZERO, ZERO
        return dec(r["free"]), dec(r["locked"])

    def balances(self, market: str, omit_zero: bool = False) -> list[dict[str, Any]]:
        rows = self._rows("SELECT asset, free, locked FROM wallets WHERE market=? ORDER BY asset", (market,))
        out = []
        for r in rows:
            if omit_zero and dec(r["free"]) == ZERO and dec(r["locked"]) == ZERO:
                continue
            out.append({"asset": r["asset"], "free": dstr(r["free"]), "locked": dstr(r["locked"])})
        return out

    def _apply(self, market: str, asset: str, free_delta: Decimal, locked_delta: Decimal,
               reason: str, ref: str | None = None) -> None:
        asset = asset.upper()
        with self.lock:
            free, locked = self.balance(market, asset)
            nf, nl = free + free_delta, locked + locked_delta
            if nf < ZERO or nl < ZERO:
                raise InsufficientBalance(f"{market}:{asset} free={free} locked={locked} "
                                          f"delta=({free_delta},{locked_delta}) reason={reason}")
            self.conn.execute(
                "INSERT OR REPLACE INTO wallets(market, asset, free, locked) VALUES (?,?,?,?)",
                (market, asset, dstr(nf), dstr(nl)),
            )
            self.conn.execute(
                "INSERT INTO balance_changes(ts, market, asset, free_delta, locked_delta, reason, ref, session_id)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (self.now(), market, asset, dstr(free_delta), dstr(locked_delta), reason, ref,
                 self.current_session_id),
            )

    def credit(self, market: str, asset: str, amount: Decimal, reason: str, ref: str | None = None) -> None:
        if amount != ZERO:
            self._apply(market, asset, amount, ZERO, reason, ref)

    def debit(self, market: str, asset: str, amount: Decimal, reason: str, ref: str | None = None) -> None:
        if amount != ZERO:
            self._apply(market, asset, -amount, ZERO, reason, ref)

    def lock_funds(self, market: str, asset: str, amount: Decimal, reason: str, ref: str | None = None) -> None:
        if amount != ZERO:
            self._apply(market, asset, -amount, amount, reason, ref)

    def unlock_funds(self, market: str, asset: str, amount: Decimal, reason: str, ref: str | None = None) -> None:
        if amount != ZERO:
            self._apply(market, asset, amount, -amount, reason, ref)

    def consume_locked(self, market: str, asset: str, amount: Decimal, reason: str, ref: str | None = None) -> None:
        """Spend funds that were previously locked for an order."""
        if amount != ZERO:
            self._apply(market, asset, ZERO, -amount, reason, ref)

    def can_afford(self, market: str, asset: str, amount: Decimal) -> bool:
        free, _ = self.balance(market, asset)
        return free >= amount

    # ------------------------------------------------------------------ orders
    def next_order_id(self, market: str) -> int:
        with self.lock:
            key = f"order_seq:{market}"
            base = 10_000_000 if market == "spot" else 40_000_000_000
            cur = self.kv_get(key, base)
            self.kv_set(key, cur + 1)
            return cur + 1

    def insert_order(self, o: dict[str, Any]) -> None:
        cols = [
            "market", "order_id", "symbol", "client_order_id", "side", "type", "orig_type", "time_in_force",
            "price", "stop_price", "orig_qty", "quote_order_qty", "executed_qty", "cum_quote", "avg_price",
            "status", "reduce_only", "close_position", "position_side", "working_type", "triggered",
            "locked_asset", "locked_amount", "created_at", "updated_at", "working_time", "session_id",
            "mid_at_arrival", "latency_ms", "source", "volume_since_place", "extra",
        ]
        vals = []
        for c in cols:
            v = o.get(c)
            if isinstance(v, Decimal):
                v = dstr(v)
            elif isinstance(v, (dict, list)):
                v = json.dumps(v)
            elif isinstance(v, bool):
                v = int(v)
            vals.append(v)
        self._exec(f"INSERT INTO orders({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)

    def update_order(self, market: str, order_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets, vals = [], []
        for k, v in fields.items():
            if isinstance(v, Decimal):
                v = dstr(v)
            elif isinstance(v, (dict, list)):
                v = json.dumps(v)
            elif isinstance(v, bool):
                v = int(v)
            sets.append(f"{k}=?")
            vals.append(v)
        sets.append("updated_at=?")
        vals.append(self.now())
        vals += [market, order_id]
        self._exec(f"UPDATE orders SET {', '.join(sets)} WHERE market=? AND order_id=?", vals)

    def get_order(self, market: str, order_id: int | None = None, client_order_id: str | None = None,
                  symbol: str | None = None) -> dict[str, Any] | None:
        if order_id is not None:
            q, p = "SELECT * FROM orders WHERE market=? AND order_id=?", [market, int(order_id)]
        elif client_order_id is not None:
            q, p = "SELECT * FROM orders WHERE market=? AND client_order_id=?", [market, client_order_id]
        else:
            return None
        if symbol:
            q += " AND symbol=?"
            p.append(symbol)
        q += " ORDER BY created_at DESC LIMIT 1"
        return self._row(q, p)

    def open_orders(self, market: str, symbol: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM orders WHERE market=? AND status IN ('NEW','PARTIALLY_FILLED')"
        p: list[Any] = [market]
        if symbol:
            q += " AND symbol=?"
            p.append(symbol)
        return self._rows(q + " ORDER BY order_id", p)

    def all_orders(self, market: str, symbol: str | None = None, limit: int = 500,
                   session_id: str | None = None) -> list[dict[str, Any]]:
        q, p = "SELECT * FROM orders WHERE market=?", [market]
        if symbol:
            q += " AND symbol=?"
            p.append(symbol)
        if session_id:
            q += " AND session_id=?"
            p.append(session_id)
        q += " ORDER BY order_id DESC LIMIT ?"
        p.append(int(limit))
        return self._rows(q, p)

    # ------------------------------------------------------------------ fills
    def add_fill(self, **f: Any) -> int:
        cols = ["market", "symbol", "order_id", "side", "price", "qty", "quote_qty", "commission",
                "commission_asset", "is_maker", "ts", "realized_pnl", "slippage_bps", "session_id", "source"]
        f.setdefault("ts", self.now())
        f.setdefault("session_id", self.current_session_id)
        f.setdefault("source", "agent")
        f.setdefault("realized_pnl", ZERO)
        vals = [dstr(f.get(c)) if isinstance(f.get(c), Decimal) else f.get(c) for c in cols]
        cur = self._exec(f"INSERT INTO fills({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return int(cur.lastrowid or 0)

    def fills(self, market: str | None = None, symbol: str | None = None, order_id: int | None = None,
              limit: int = 500, session_id: str | None = None) -> list[dict[str, Any]]:
        q, p = "SELECT * FROM fills WHERE 1=1", []
        if market:
            q += " AND market=?"; p.append(market)
        if symbol:
            q += " AND symbol=?"; p.append(symbol)
        if order_id is not None:
            q += " AND order_id=?"; p.append(int(order_id))
        if session_id:
            q += " AND session_id=?"; p.append(session_id)
        q += " ORDER BY fill_id DESC LIMIT ?"; p.append(int(limit))
        return self._rows(q, p)

    # ------------------------------------------------------------------ positions (usdm)
    def position(self, symbol: str, market: str = "usdm") -> dict[str, Any]:
        r = self._row("SELECT * FROM positions WHERE market=? AND symbol=?", (market, symbol))
        if not r:
            return {"market": market, "symbol": symbol, "position_amt": "0", "entry_price": "0",
                    "isolated_margin": "0", "updated_at": None}
        return r

    def positions(self, market: str = "usdm", nonzero: bool = True) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM positions WHERE market=? ORDER BY symbol", (market,))
        if nonzero:
            rows = [r for r in rows if dec(r["position_amt"]) != ZERO]
        return rows

    def upsert_position(self, symbol: str, position_amt: Decimal, entry_price: Decimal,
                        isolated_margin: Decimal, market: str = "usdm") -> None:
        self._exec(
            "INSERT OR REPLACE INTO positions(market, symbol, position_amt, entry_price, isolated_margin, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (market, symbol, dstr(position_amt), dstr(entry_price), dstr(isolated_margin), self.now()),
        )

    def symbol_settings(self, symbol: str, default_leverage: int, market: str = "usdm") -> dict[str, Any]:
        r = self._row("SELECT * FROM symbol_settings WHERE market=? AND symbol=?", (market, symbol))
        if not r:
            return {"leverage": default_leverage, "margin_type": "ISOLATED"}
        return {"leverage": int(r["leverage"]), "margin_type": r["margin_type"]}

    def set_symbol_settings(self, symbol: str, leverage: int | None = None, margin_type: str | None = None,
                            default_leverage: int = 5, market: str = "usdm") -> dict[str, Any]:
        cur = self.symbol_settings(symbol, default_leverage, market)
        if leverage is not None:
            cur["leverage"] = int(leverage)
        if margin_type is not None:
            cur["margin_type"] = margin_type
        self._exec("INSERT OR REPLACE INTO symbol_settings(market, symbol, leverage, margin_type) VALUES (?,?,?,?)",
                   (market, symbol, cur["leverage"], cur["margin_type"]))
        return cur

    def add_income(self, symbol: str | None, income_type: str, income: Decimal, asset: str = "USDT",
                   info: str = "", market: str = "usdm") -> None:
        self._exec(
            "INSERT INTO income(ts, market, symbol, income_type, income, asset, info, session_id) VALUES (?,?,?,?,?,?,?,?)",
            (self.now(), market, symbol, income_type, dstr(income), asset, info, self.current_session_id),
        )

    def incomes(self, symbol: str | None = None, income_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q, p = "SELECT * FROM income WHERE 1=1", []
        if symbol:
            q += " AND symbol=?"; p.append(symbol)
        if income_type:
            q += " AND income_type=?"; p.append(income_type)
        q += " ORDER BY id DESC LIMIT ?"; p.append(int(limit))
        return self._rows(q, p)

    # ------------------------------------------------------------------ transfers
    def add_transfer(self, asset: str, amount: Decimal, from_wallet: str, to_wallet: str, type_: str) -> int:
        cur = self._exec(
            "INSERT INTO transfers(ts, asset, amount, from_wallet, to_wallet, type, status, session_id)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (self.now(), asset.upper(), dstr(amount), from_wallet, to_wallet, type_, "CONFIRMED",
             self.current_session_id),
        )
        return int(cur.lastrowid or 0)

    def transfers(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM transfers ORDER BY tran_id DESC LIMIT ?", (int(limit),))

    # ------------------------------------------------------------------ sessions / calls / events
    def start_session(self, session_id: str, run_id: str | None = None, label: str | None = None,
                      initial_equity: Decimal | None = None, meta: dict[str, Any] | None = None) -> None:
        self._exec(
            "INSERT OR REPLACE INTO sessions(session_id, run_id, label, started_at, ended_at, initial_equity,"
            " final_equity, status, meta) VALUES (?,?,?,?,NULL,?,NULL,'RUNNING',?)",
            (session_id, run_id, label, self.now(), dstr(initial_equity) if initial_equity is not None else None,
             json.dumps(meta or {})),
        )
        self.current_session_id = session_id

    def end_session(self, session_id: str, final_equity: Decimal | None = None, status: str = "DONE",
                    meta_update: dict[str, Any] | None = None) -> None:
        if meta_update:
            row = self._row("SELECT meta FROM sessions WHERE session_id=?", (session_id,))
            try:
                meta = json.loads((row or {}).get("meta") or "{}")
            except Exception:
                meta = {}
            meta.update(meta_update)
            self._exec("UPDATE sessions SET meta=? WHERE session_id=?", (json.dumps(meta, default=str), session_id))
        self._exec("UPDATE sessions SET ended_at=?, final_equity=?, status=? WHERE session_id=?",
                   (self.now(), dstr(final_equity) if final_equity is not None else None, status, session_id))
        if self.current_session_id == session_id:
            self.current_session_id = None

    def sessions(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id:
            return self._rows("SELECT * FROM sessions WHERE run_id=? ORDER BY started_at", (run_id,))
        return self._rows("SELECT * FROM sessions ORDER BY started_at")

    def log_tool_call(self, tool: str, category: str, args: Any, result_status: str, result: Any,
                      latency_ms: int, error_code: int | None = None, mid: Decimal | None = None,
                      source: str = "agent", session_id: str | None = None) -> int:
        cur = self._exec(
            "INSERT INTO tool_calls(ts, session_id, tool, category, args, result_status, result, latency_ms,"
            " error_code, mid_at_arrival, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.now(), session_id or self.current_session_id, tool, category, json.dumps(args, default=str),
             result_status, json.dumps(result, default=str)[:20000], latency_ms, error_code,
             dstr(mid) if mid is not None else None, source),
        )
        return int(cur.lastrowid or 0)

    def tool_calls(self, session_id: str | None = None, limit: int = 50, source: str | None = None) -> list[dict[str, Any]]:
        q, p = "SELECT * FROM tool_calls WHERE 1=1", []
        if session_id:
            q += " AND session_id=?"; p.append(session_id)
        if source:
            q += " AND source=?"; p.append(source)
        q += " ORDER BY id DESC LIMIT ?"; p.append(int(limit))
        rows = self._rows(q, p)
        for r in rows:
            for k in ("args", "result"):
                try:
                    r[k] = json.loads(r[k]) if r[k] else None
                except Exception:
                    pass
        return rows

    def add_event(self, kind: str, symbol: str | None = None, detail: dict[str, Any] | None = None,
                  source: str = "agent", session_id: str | None = None) -> None:
        self._exec("INSERT INTO events(ts, session_id, kind, symbol, detail, source) VALUES (?,?,?,?,?,?)",
                   (self.now(), session_id or self.current_session_id, kind, symbol,
                    json.dumps(detail or {}, default=str), source))

    def events(self, session_id: str | None = None, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q, p = "SELECT * FROM events WHERE 1=1", []
        if session_id:
            q += " AND session_id=?"; p.append(session_id)
        if kind:
            q += " AND kind=?"; p.append(kind)
        q += " ORDER BY id DESC LIMIT ?"; p.append(int(limit))
        rows = self._rows(q, p)
        for r in rows:
            try:
                r["detail"] = json.loads(r["detail"]) if r["detail"] else {}
            except Exception:
                pass
        return rows

    def snapshot_equity(self, equity: Decimal, spot_value: Decimal, usdm_wallet: Decimal,
                        usdm_unrealized: Decimal, source: str = "paper") -> None:
        self._exec(
            "INSERT INTO equity_snapshots(ts, session_id, equity, spot_value, usdm_wallet, usdm_unrealized, source)"
            " VALUES (?,?,?,?,?,?,?)",
            (self.now(), self.current_session_id, dstr(equity), dstr(spot_value), dstr(usdm_wallet),
             dstr(usdm_unrealized), source),
        )

    def equity_curve(self, session_id: str | None = None, source: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        q, p = "SELECT ts, session_id, equity, spot_value, usdm_wallet, usdm_unrealized, source FROM equity_snapshots WHERE 1=1", []
        if session_id:
            q += " AND session_id=?"; p.append(session_id)
        if source:
            q += " AND source=?"; p.append(source)
        q += " ORDER BY id DESC LIMIT ?"; p.append(int(limit))
        rows = self._rows(q, p)
        rows.reverse()
        return rows

    def add_shadow_event(self, tool: str, args: Any, real_result: Any, sim_result: Any,
                         divergence: dict[str, Any] | None, is_write: bool) -> None:
        self._exec(
            "INSERT INTO shadow_events(ts, tool, args, real_result, sim_result, divergence, is_write) VALUES (?,?,?,?,?,?,?)",
            (self.now(), tool, json.dumps(args, default=str), json.dumps(real_result, default=str)[:20000],
             json.dumps(sim_result, default=str)[:20000], json.dumps(divergence or {}, default=str), int(is_write)),
        )

    def shadow_events(self, limit: int = 20, writes_only: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM shadow_events" + (" WHERE is_write=1" if writes_only else "") + " ORDER BY id DESC LIMIT ?"
        rows = self._rows(q, (int(limit),))
        for r in rows:
            for k in ("args", "real_result", "sim_result", "divergence"):
                try:
                    r[k] = json.loads(r[k]) if r[k] else None
                except Exception:
                    pass
        return rows

    # ------------------------------------------------------------------ convert quotes
    def add_quote(self, q: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO convert_quotes(quote_id, ts, from_asset, to_asset, from_amount, to_amount, ratio,"
            " valid_until, status, order_id, wallet_type) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (q["quote_id"], self.now(), q["from_asset"], q["to_asset"], dstr(q["from_amount"]), dstr(q["to_amount"]),
             dstr(q["ratio"]), q["valid_until"], q.get("status", "QUOTED"), q.get("order_id"), q.get("wallet_type", "SPOT")),
        )

    def get_quote(self, quote_id: str) -> dict[str, Any] | None:
        return self._row("SELECT * FROM convert_quotes WHERE quote_id=?", (quote_id,))

    def get_quote_by_order(self, order_id: str) -> dict[str, Any] | None:
        return self._row("SELECT * FROM convert_quotes WHERE order_id=?", (order_id,))

    def update_quote(self, quote_id: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE convert_quotes SET {sets} WHERE quote_id=?", [*fields.values(), quote_id])

    # ------------------------------------------------------------------ invariants (tests)
    def total(self, market: str, asset: str) -> Decimal:
        f, l = self.balance(market, asset)
        return f + l

    def close(self) -> None:
        with self.lock:
            self.conn.close()


def wall_ms() -> int:
    return int(time.time() * 1000)
