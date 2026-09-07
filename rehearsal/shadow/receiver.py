"""Shadow mode receiver: mirrors real tool calls (from a Claude Code PostToolUse hook) into the twin."""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

from rehearsal.config import Config
from rehearsal.engine.ledger import dec, dstr
from rehearsal.server.mcp_server import Twin
from rehearsal.shadow.divergence import calibrate, compare

log = logging.getLogger("rehearsal.shadow")
D = Decimal


def _unwrap_response(resp: Any) -> Any:
    """Claude Code hands us tool_response as the MCP result; pull out the JSON the model saw."""
    if resp is None:
        return None
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except Exception:
            return {"_text": resp}
    if isinstance(resp, dict):
        if "structuredContent" in resp and resp["structuredContent"]:
            return resp["structuredContent"]
        content = resp.get("content")
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                try:
                    return json.loads(texts[0])
                except Exception:
                    return {"_text": texts[0]}
        return resp
    if isinstance(resp, list):
        texts = [c.get("text") for c in resp if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            try:
                return json.loads(texts[0])
            except Exception:
                return {"_text": texts[0]}
    return resp


class ShadowReceiver:
    def __init__(self, twin: Twin, cfg: Config):
        self.twin = twin
        self.cfg = cfg
        self.engine = twin.engine
        self.active = False
        self.last_event_ts: float | None = None
        self.count = 0
        self.synced_balances = False
        self.quote_map: dict[str, str] = {}          # real quoteId -> twin quoteId
        self.order_map: dict[tuple[str, str], int] = {}  # (symbol, real orderId) -> twin orderId

    def handle(self, body: dict[str, Any]) -> dict[str, Any]:
        self.active = True
        self.last_event_ts = time.time()
        self.count += 1
        tool_name = str(body.get("tool_name") or body.get("tool") or "")
        # Claude Code prefixes MCP tools: mcp__<server>__<tool>
        if tool_name.startswith("mcp__"):
            parts = tool_name.split("__", 2)
            tool_name = parts[2] if len(parts) == 3 else tool_name
        args = body.get("tool_input") or body.get("arguments") or {}
        real = _unwrap_response(body.get("tool_response"))
        spec = self.twin.catalog.resolve(tool_name)
        is_write = bool(spec and spec.write)
        session_id = self.engine.ledger.current_session_id or "shadow"

        # tool_execute: mirror the inner tool.
        if spec and spec.handler == "tool_execute":
            inner_name = args.get("toolName") or ""
            args = args.get("arguments") or {}
            spec = self.twin.catalog.resolve(str(inner_name))
            tool_name = str(inner_name)
            is_write = bool(spec and spec.write)

        if self.cfg.shadow.sync_balances_on_first_read and not self.synced_balances and not self.engine.ledger.fills(limit=1):
            # Only align paper with live while the paper account is still untouched.
            self._maybe_sync_balances(tool_name, real)

        sim: Any = None
        divergence: dict[str, Any] | None = None
        if spec is not None:
            sim_args = self._translate_ids(dict(args))
            outcome = self.twin.execute(tool_name, sim_args, session_id, source="shadow")
            sim = outcome["result"]
            self._learn_ids(args, real, sim)
            if is_write:
                divergence = compare(tool_name, args, real, sim, outcome.get("latency_ms"))
        self.engine.ledger.add_shadow_event(tool_name, args, real, sim, divergence, is_write)
        self._record_live_equity(tool_name, real)
        return {"ok": True, "tool": tool_name, "mirrored": spec is not None, "divergence": divergence}

    def _translate_ids(self, args: dict[str, Any]) -> dict[str, Any]:
        """The twin issues its own ids; translate the live ids the agent uses back to the twin's."""
        q = args.get("quoteId")
        if q is not None and str(q) in self.quote_map:
            args["quoteId"] = self.quote_map[str(q)]
        oid = args.get("orderId")
        sym = str(args.get("symbol") or "").upper()
        if oid is not None and (sym, str(oid)) in self.order_map:
            args["orderId"] = self.order_map[(sym, str(oid))]
        return args

    def _learn_ids(self, args: dict[str, Any], real: Any, sim: Any) -> None:
        if not isinstance(real, dict) or not isinstance(sim, dict):
            return
        if real.get("quoteId") and sim.get("quoteId"):
            self.quote_map[str(real["quoteId"])] = str(sim["quoteId"])
        if real.get("orderId") is not None and sim.get("orderId") is not None and real.get("symbol"):
            self.order_map[(str(real["symbol"]).upper(), str(real["orderId"]))] = int(sim["orderId"])

    def _maybe_sync_balances(self, tool_name: str, real: Any) -> None:
        if not isinstance(real, dict):
            return
        bal = real.get("balances")
        if tool_name.endswith("getAccount") and isinstance(bal, list):
            init: dict[str, dict[str, float]] = {"spot": {}, "usdm": {}}
            for b in bal:
                try:
                    amt = float(b.get("free", 0)) + float(b.get("locked", 0))
                except Exception:
                    continue
                if amt > 0:
                    init["spot"][b["asset"]] = amt
            if init["spot"]:
                self.engine.ledger.reset(init, keep_calls=True)
                self.engine.snapshot_equity(force=True)
                self.synced_balances = True
                # The dashboard's "vs initial" baseline is now the live account, not rehearsal.yaml.
                self.engine.ledger.kv_set("initial_equity_override", str(self.engine.equity()["equity"]))
                self.engine.ledger.add_event("shadow_balance_sync", None, {"balances": init["spot"]}, source="shadow")
                log.info("shadow: synced paper balances from live account: %s", init["spot"])

    def _record_live_equity(self, tool_name: str, real: Any) -> None:
        """Estimate live equity from mirrored reads (spot account / futures account)."""
        if not isinstance(real, dict):
            return
        eq = None
        if tool_name.endswith("getAccount") and isinstance(real.get("balances"), list):
            tot = D(0)
            for b in real["balances"]:
                try:
                    amt = dec(b.get("free", 0)) + dec(b.get("locked", 0))
                except Exception:
                    continue
                if amt == 0:
                    continue
                px = self.engine.price_in_usdt(b["asset"])
                if px is not None:
                    tot += amt * px
            eq = tot
        elif tool_name.endswith("futures_usds.account") or tool_name.endswith("account") and real.get("totalMarginBalance"):
            try:
                eq = dec(real.get("totalMarginBalance"))
            except Exception:
                eq = None
        if eq is not None:
            self.engine.ledger.snapshot_equity(eq, eq, D(0), D(0), source="live")

    def status(self, full: bool = False) -> dict[str, Any]:
        events = self.engine.ledger.shadow_events(limit=20)
        writes = [e for e in events if e["is_write"]]
        cal = calibrate([e["divergence"] for e in writes if e.get("divergence")], self.cfg)
        out = {"active": self.active, "events": self.count, "last_event_ts": self.last_event_ts,
               "divergences": [{"ts": e["ts"], "tool": e["tool"], **(e["divergence"] or {})} for e in writes],
               "calibration": cal, "synced_balances": self.synced_balances}
        if full:
            out["recent"] = events
        return out
