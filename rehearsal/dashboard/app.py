"""FastAPI app: dashboard (/), JSON API (/api/*), shadow receiver (/shadow/event), MCP HTTP (/mcp)."""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount

from rehearsal.config import Config
from rehearsal.engine.ledger import dec, dstr
from rehearsal.server.mcp_server import Twin
from rehearsal.shadow.receiver import ShadowReceiver

log = logging.getLogger("rehearsal.dashboard")
STATIC = Path(__file__).parent / "static"


def _latest_report(cfg: Config) -> dict[str, Any] | None:
    d = cfg.path(cfg.runner.reports_dir)
    if not d or not d.exists():
        return None
    latest = d / "latest.json"
    if latest.exists():
        try:
            return json.loads(latest.read_text())
        except Exception:
            return None
    return None


def build_state(twin: Twin, cfg: Config, shadow: ShadowReceiver) -> dict[str, Any]:
    e = twin.engine
    led = e.ledger
    feed = e.feed
    eq = e.equity()
    mode = "REPLAY" if feed.mode == "replay" else ("SHADOW" if shadow.active else "PAPER")
    fills = []
    for f in led.fills(limit=20):
        fills.append({"ts": f["ts"], "market": f["market"], "symbol": f["symbol"], "side": f["side"], "price": dstr(f["price"]),
                      "qty": dstr(f["qty"]), "quote_qty": dstr(f["quote_qty"]), "commission": dstr(f["commission"]),
                      "commission_asset": f["commission_asset"], "is_maker": bool(f["is_maker"]), "slippage_bps": f["slippage_bps"],
                      "realized_pnl": dstr(f["realized_pnl"]), "source": f["source"], "order_id": f["order_id"]})
    open_orders = []
    for m in ("spot", "usdm"):
        for r in led.open_orders(m):
            open_orders.append({"market": m, "order_id": r["order_id"], "symbol": r["symbol"], "side": r["side"], "type": r["type"],
                                "price": dstr(r["price"]), "stop_price": dstr(r["stop_price"]), "orig_qty": dstr(r["orig_qty"]),
                                "executed_qty": dstr(r["executed_qty"]), "status": r["status"], "created_at": r["created_at"],
                                "triggered": bool(r["triggered"])})
    calls = []
    for c in led.tool_calls(limit=50):
        calls.append({"id": c["id"], "ts": c["ts"], "tool": c["tool"], "category": c["category"], "status": c["result_status"],
                      "error_code": c["error_code"], "latency_ms": c["latency_ms"], "session_id": c["session_id"], "source": c["source"],
                      "args": c["args"], "msg": (c["result"] or {}).get("msg") if isinstance(c["result"], dict) else None})
    curve = [[r["ts"], float(r["equity"])] for r in _latest_segment(led.equity_curve(limit=3000))]
    live_curve = [[r["ts"], float(r["equity"])] for r in _latest_segment(led.equity_curve(source="live", limit=1500))]
    report = _latest_report(cfg)
    sessions = led.sessions()
    cur = led.current_session_id
    return {
        "ts": int(time.time() * 1000),
        "mode": mode,
        "clock_ms": e.now(),
        "clock_iso": e.clock.iso(),
        "server": twin.status(),
        "schema": twin.catalog.status(),
        "feed": feed.status(),
        "equity": {"equity": dstr(eq["equity"], 2), "spot_value": dstr(eq["spot_value"], 2), "usdm_wallet": dstr(eq["usdm_wallet"], 2),
                   "usdm_unrealized": dstr(eq["usdm_unrealized"], 2), "initial": dstr(_initial_equity(cfg, led), 2)},
        "balances": {"spot": eq["spot_detail"], "usdm": e.usdm.balance_view()[0]},
        "positions": e.usdm.position_view(),
        "open_orders": open_orders,
        "fills": fills,
        "equity_curve": curve,
        "live_equity_curve": live_curve,
        "tool_calls": calls,
        "events": led.events(limit=30),
        "session": {"current": cur, "count": len(sessions), "sessions": sessions[-10:]},
        "report": report,
        "shadow": shadow.status(),
        "config": {"initial_balances": cfg.engine.initial_balances, "policy": cfg.policy.model_dump(), "gate": cfg.gate.model_dump(),
                   "latency": cfg.engine.latency.model_dump(), "queue_factor": cfg.engine.queue_factor, "live_url": cfg.live_url,
                   "http_port": cfg.server.http_port},
    }


def _latest_segment(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the most recent monotonic-time run of snapshots.

    Replay restarts (demo loops, new rehearsal sessions) rewind the virtual clock; drawing the older
    loops on the same time axis produces lines that run backwards across the chart."""
    seg: list[dict[str, Any]] = []
    last: int | None = None
    for r in rows:
        if last is not None and r["ts"] < last:
            seg = []
        seg.append(r)
        last = r["ts"]
    return seg


def _initial_equity(cfg: Config, ledger: Any = None) -> Any:
    if ledger is not None:
        ov = ledger.kv_get("initial_equity_override")
        if ov is not None:
            return dec(ov)
    tot = 0.0
    for m, assets in cfg.engine.initial_balances.items():
        for a, v in assets.items():
            if a.upper() in ("USDT", "USDC", "FDUSD", "BUSD"):
                tot += float(v)
    return dec(tot)


def create_app(twin: Twin, cfg: Config, shadow: ShadowReceiver, mount_mcp: bool = True) -> FastAPI:
    mcp_server = twin.make_server()
    mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                             host=cfg.server.http_host) if mount_mcp else None

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if mcp_app is not None:
            async with mcp_server.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="Dress Rehearsal twin", lifespan=lifespan)

    @app.middleware("http")
    async def bearer_guard(request: Request, call_next):
        tok = cfg.server.bearer_token
        if tok and request.url.path.startswith("/mcp"):
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {tok}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (STATIC / "index.html").read_text()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "mode": twin.engine.feed.mode, "schema": twin.catalog.source}

    @app.get("/api/state")
    async def state() -> Any:
        import anyio
        return await anyio.to_thread.run_sync(build_state, twin, cfg, shadow)

    @app.get("/api/tools")
    async def tools() -> Any:
        return {"source": twin.catalog.source, "tools": [t.raw for t in twin.catalog.list_for_mcp()]}

    @app.get("/api/report/latest")
    async def report_latest() -> Any:
        r = _latest_report(cfg)
        if r is None:
            raise HTTPException(404, "no report yet")
        return r

    @app.post("/api/reset")
    async def reset() -> Any:
        twin.engine.reset()
        return {"ok": True}

    @app.post("/api/session/start")
    async def session_start(body: dict[str, Any] | None = None) -> Any:
        body = body or {}
        sid = twin.engine.start_session(body.get("session_id"), body.get("run_id"), body.get("label"),
                                        reset=bool(body.get("reset", True)), meta=body.get("meta"))
        return {"session_id": sid}

    @app.post("/api/session/end")
    async def session_end(body: dict[str, Any]) -> Any:
        return twin.engine.end_session(body["session_id"], body.get("status", "DONE"))

    @app.post("/api/call")
    async def api_call(body: dict[str, Any]) -> Any:
        """Direct tool call (used by `rehearsal demo` and tests). Same path as MCP."""
        import anyio
        return await anyio.to_thread.run_sync(twin.execute, body["tool"], body.get("arguments") or {},
                                              body.get("session_id") or twin.engine.ledger.current_session_id, body.get("source") or "agent")

    @app.post("/shadow/event")
    async def shadow_event(body: dict[str, Any]) -> Any:
        import anyio
        return await anyio.to_thread.run_sync(shadow.handle, body)

    @app.get("/api/shadow")
    async def shadow_state() -> Any:
        return shadow.status(full=True)

    if mcp_app is not None:
        app.router.routes.append(Mount("/", app=mcp_app))
    return app


class DashboardThread:
    """Runs uvicorn in a daemon thread (used when the MCP transport is stdio)."""

    def __init__(self, app: FastAPI, host: str, port: int):
        import uvicorn

        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name="dashboard", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
