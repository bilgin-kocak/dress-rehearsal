"""The twin MCP server: tools generated from schemas/tools.json, dispatched to category handlers.

Every call is logged to the ledger's tool_calls table with latency, status, error code and the
mid price at arrival (for trade tools), which feeds metrics, the report and the dashboard.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import anyio
from mcp.server import Server, ServerRequestContext
from mcp.shared.exceptions import MCPError as McpError
from mcp.types import (
    INTERNAL_ERROR,
    CallToolRequestParams,
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    TextContent,
    TextResourceContents,
    Tool,
    ToolAnnotations,
)

from rehearsal import __version__
from rehearsal.config import Config
from rehearsal.engine.engine import Engine
from rehearsal.server.catalog import ToolCatalog, ToolSpec
from rehearsal.server.confirm import Confirmer, restatement
from rehearsal.server.errors import BinanceError, ToolNotFound
from rehearsal.server.handlers import HANDLERS, ToolContext

log = logging.getLogger("rehearsal.server")

INSTRUCTIONS = (
    "Binance MCP server (Agentic sub-account). Read market data, check balances and positions, trade Spot / "
    "USDⓈ-M Futures / Convert, and move funds between wallets inside the Agentic sub-account. Use tool_search to "
    "discover more tools and tool_execute to run them. Every order, cancel or transfer must be restated to the user "
    "(symbol, side, type, quantity, price) and explicitly confirmed before the tool is called. Withdrawals are not possible."
)


class Twin:
    def __init__(self, cfg: Config, engine: Engine, catalog: ToolCatalog):
        self.cfg = cfg
        self.engine = engine
        self.catalog = catalog
        self.confirmer = Confirmer(cfg)
        self.name = cfg.schema_.server_name
        self.started_at = time.time()
        self.stats = {"calls": 0, "errors": 0, "unsupported": 0}

    # ------------------------------------------------------------------ server factory
    def make_server(self) -> Server:
        # Mirror the real server's initialize payload when we have it (name, version, instructions).
        init = self.catalog.init or {}
        si = init.get("server_info") or {}
        name = si.get("name") or self.name
        version = str(si.get("version") or __version__)
        instructions = init.get("instructions") or INSTRUCTIONS
        kw: dict[str, Any] = {}
        if self.catalog.resources:
            kw.update(on_list_resources=self._list_resources, on_read_resource=self._read_resource)
        return Server(name, version=version, title=si.get("title"), instructions=instructions,
                      on_list_tools=self._list_tools, on_call_tool=self._call_tool, **kw)

    async def _list_resources(self, ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListResourcesResult:
        res = []
        for r in self.catalog.resources:
            res.append(Resource(uri=r["uri"], name=r.get("name") or r["uri"], description=r.get("description"),
                                mime_type=r.get("mimeType"), size=r.get("size")))
        return ListResourcesResult(resources=res)

    async def _read_resource(self, ctx: ServerRequestContext, params: ReadResourceRequestParams) -> ReadResourceResult:
        for r in self.catalog.resources:
            if str(r["uri"]) == str(params.uri):
                contents = []
                for c in r.get("contents") or []:
                    contents.append(TextResourceContents(uri=c.get("uri", r["uri"]), mime_type=c.get("mimeType", r.get("mimeType")),
                                                         text=c.get("text", "")))
                return ReadResourceResult(contents=contents)
        raise McpError(INTERNAL_ERROR, f"Resource not found: {params.uri}")

    # ------------------------------------------------------------------ MCP handlers
    async def _list_tools(self, ctx: ServerRequestContext, params: PaginatedRequestParams | None) -> ListToolsResult:
        tools = []
        for spec in self.catalog.list_for_mcp():
            desc = spec.description
            if self.cfg.server.mark_twin_descriptions:
                desc = "[TWIN] " + desc
            ann = None
            if spec.annotations:
                a = spec.annotations
                ann = ToolAnnotations(title=a.get("title"), read_only_hint=a.get("readOnlyHint"), destructive_hint=a.get("destructiveHint"),
                                      idempotent_hint=a.get("idempotentHint"), open_world_hint=a.get("openWorldHint"))
            tools.append(Tool(name=spec.name, title=spec.title, description=desc, input_schema=spec.input_schema, annotations=ann))
        return ListToolsResult(tools=tools)

    async def _call_tool(self, ctx: ServerRequestContext, params: CallToolRequestParams) -> CallToolResult | InputRequiredResult:
        name = params.name
        args = dict(params.arguments or {})
        session_id = self._session_from_ctx(ctx)
        spec = self.catalog.resolve(name)

        # Elicitation-based confirmation for write tools.
        if spec and spec.write and self.cfg.schema_.confirm_mode == "elicitation" and spec.handler != "tool_execute":
            answer = (params.input_responses or {}).get("confirm")
            if not isinstance(answer, ElicitResult):
                req = ElicitRequest(params=ElicitRequestFormParams(
                    message=f"Confirm: {restatement(spec.name, args)}",
                    requested_schema={"type": "object", "properties": {"confirm": {"type": "boolean", "title": "Execute this order?"}},
                                      "required": ["confirm"]}))
                return InputRequiredResult(input_requests={"confirm": req}, request_state=spec.name)
            if answer.action != "accept" or not (answer.content or {}).get("confirm"):
                self.engine.ledger.log_tool_call(spec.name, spec.category, args, "declined", {"status": "DECLINED"}, 0, None,
                                                 None, "agent", session_id)
                return CallToolResult(content=[TextContent(type="text", text=json.dumps({"status": "DECLINED", "msg": "User declined."}))],
                                      is_error=True)

        outcome = await anyio.to_thread.run_sync(self.execute, name, args, session_id, "agent")
        if outcome["status"] == "error" and self.cfg.server.error_style == "jsonrpc":
            # The real server returns Binance errors as JSON-RPC errors (-32603) whose message is the raw
            # Binance JSON, e.g. {"code":-1013,"msg":"Filter failure: LOT_SIZE"}.
            raise McpError(INTERNAL_ERROR, self.error_message(outcome["result"]))
        return self.to_result(outcome)

    @staticmethod
    def error_message(result: Any) -> str:
        if isinstance(result, dict) and result.get("_raw_message"):
            return str(result["_raw_message"])
        return json.dumps(result, separators=(",", ":"), ensure_ascii=False)

    def _session_from_ctx(self, ctx: ServerRequestContext) -> str | None:
        req = getattr(ctx, "request", None)
        try:
            if req is not None and hasattr(req, "headers"):
                h = req.headers.get("x-rehearsal-session")
                if h:
                    return h
        except Exception:
            pass
        return self.engine.ledger.current_session_id

    # ------------------------------------------------------------------ execution (sync; shared with shadow + demo)
    def execute(self, name: str, args: dict[str, Any], session_id: str | None, source: str = "agent",
                via: str | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        spec = self.catalog.resolve(name)
        mid = None
        status, error_code = "ok", None
        result: Any
        try:
            if spec is None:
                raise BinanceError(-9001, f"TWIN_UNSUPPORTED: {name}")
            if spec.write and spec.handler != "tool_execute":
                pending = self.confirmer.echo_check(spec.name, args)
                if pending is not None:
                    result = pending
                    status = "pending"
                    return self._finish(spec, name, args, status, result, None, t0, mid, source, session_id, via)
            ctx = ToolContext(engine=self.engine, catalog=self.catalog, session_id=session_id, source=source,
                              dispatch=self._dispatch_inner)
            call_args = self.confirmer.strip(args)
            self._check_numeric_formatting(call_args)
            if spec.handler in ("unsupported", "passthrough_public"):
                call_args = dict(call_args, _tool=spec.name)
            if spec.category == "trade" and call_args.get("symbol") and spec.market:
                try:
                    sym = str(call_args["symbol"]).upper()
                    if self.engine.feed.symbol_info(spec.market, sym):
                        mid = self.engine.feed.mid(spec.market, sym)
                except Exception:
                    mid = None
            fn = HANDLERS.get(spec.handler)
            if fn is None:
                raise BinanceError(-9001, f"TWIN_UNSUPPORTED: {spec.name} (no handler '{spec.handler}')")
            result = fn(ctx, call_args)
        except BinanceError as e:
            result, status, error_code = e.payload(), "error", e.code
        except ToolNotFound as e:
            result, status, error_code = {"code": -9004, "msg": str(e), "_raw_message": str(e)}, "error", -9004
        except Exception as e:  # pragma: no cover - defensive
            log.exception("handler crashed for %s: %s", name, e)
            result, status, error_code = {"code": -1000, "msg": "An unknown error occurred while processing the request."}, "error", -1000
        return self._finish(spec, name, args, status, result, error_code, t0, mid, source, session_id, via)

    @staticmethod
    def _check_numeric_formatting(args: dict[str, Any]) -> None:
        """Observed on the real server (2026-09-07): JSON numbers below 0.001 are formatted in exponent form
        (Java-style 1.0E-4) before the request is signed, and Binance rejects them with -1100. Strings pass."""
        for k, v in args.items():
            if isinstance(v, bool) or isinstance(v, str):
                continue
            if isinstance(v, (int, float)) and v != 0 and abs(v) < 0.001:
                raise BinanceError(-1100, f"Illegal characters found in parameter '{k}'; legal range is "
                                          "'^([0-9]{1,20})(\\.[0-9]{1,20})?$'.")

    def _finish(self, spec: ToolSpec | None, name: str, args: dict[str, Any], status: str, result: Any, error_code: int | None,
                t0: float, mid: Any, source: str, session_id: str | None, via: str | None) -> dict[str, Any]:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        canonical = spec.name if spec else name
        category = spec.category if spec else "unsupported"
        self.stats["calls"] += 1
        if status == "error":
            self.stats["errors"] += 1
            if error_code == -9001:
                self.stats["unsupported"] += 1
                self.engine.ledger.add_event("twin_unsupported", None, {"tool": canonical}, source=source, session_id=session_id)
        # tool_execute itself is not logged; the inner call is (tagged with via).
        if not (spec and spec.handler == "tool_execute"):
            log_args = dict(args)
            if via:
                log_args["_via"] = via
            self.engine.ledger.log_tool_call(canonical, category, log_args, status, result, latency_ms, error_code, mid, source, session_id)
        return {"tool": canonical, "status": status, "result": result, "error_code": error_code, "latency_ms": latency_ms,
                "write": bool(spec and spec.write), "category": category}

    def _dispatch_inner(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> Any:
        out = self.execute(name, args, ctx.session_id, ctx.source, via="tool_execute")
        if out["status"] == "error":
            r = out["result"]
            if isinstance(r, dict) and r.get("_raw_message"):
                raise ToolNotFound(str(r["_raw_message"]))
            raise BinanceError(r.get("code", -1000), r.get("msg", "error"))
        return out["result"]

    # ------------------------------------------------------------------ result shaping
    def to_result(self, outcome: dict[str, Any]) -> CallToolResult:
        result = outcome["result"]
        text = json.dumps(result, ensure_ascii=False, default=str)
        structured = result if isinstance(result, dict) else None
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=structured,
                              is_error=(outcome["status"] == "error"))

    # ------------------------------------------------------------------ transports
    async def run_stdio(self) -> None:
        from mcp.server.stdio import stdio_server

        server = self.make_server()
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "version": __version__, "uptime_s": int(time.time() - self.started_at), **self.stats,
                "confirm_mode": self.cfg.schema_.confirm_mode}
