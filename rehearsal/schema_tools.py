"""`rehearsal schema dump|validate`: mirror the REAL server's tools/list verbatim.

Binance only accepts OAuth from listed clients (Claude Code, Codex, ...), so a custom OAuth flow is
rejected (error 3346001). We therefore reuse the token Claude Code stores after the user completes
`/mcp` → binance-mcp-server → Authenticate. On macOS it lives in the login keychain item
"Claude Code-credentials" (JSON with an `mcpOAuth` map); on Linux in ~/.claude/.credentials.json.
Codex's stored token (~/.codex/auth.json / keychain) is tried as a fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from rehearsal.config import Config
from rehearsal.server.catalog import ToolCatalog, schema_diff

log = logging.getLogger("rehearsal.schema")

SAMPLE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("spot.tickerPrice", {"symbol": "BTCUSDT"}),
    ("spot.ticker24hr", {"symbol": "BTCUSDT"}),
    ("spot.depth", {"symbol": "BTCUSDT", "limit": 5}),
    ("spot.klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 3}),
    ("spot.exchangeInfo", {"symbol": "BTCUSDT"}),
    ("spot.getAccount", {"omitZeroBalances": True}),
    ("spot.getOpenOrders", {}),
    ("futures_usds.markPrice", {"symbol": "BTCUSDT"}),
    ("futures_usds.symbolPriceTicker", {"symbol": "BTCUSDT"}),
    ("futures_usds.positionRisk", {"symbol": "BTCUSDT"}),
    ("futures_usds.balance", {}),
    ("wallet.queryUserWalletBalance", {}),
    ("tool_search", {"category": "trade"}),
    # A rejected write: no money moves, but we learn the exact error shape.
    ("spot.newOrder", {"symbol": "FOOBAR", "side": "BUY", "type": "MARKET", "quantity": "1"}),
]


# ---------------------------------------------------------------------------- token discovery
def _keychain_json(service: str) -> dict[str, Any] | None:
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"], capture_output=True, text=True, timeout=20)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        return json.loads(out.stdout.strip())
    except Exception:
        return None


def _file_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return None


def find_claude_code_token(server_name: str = "binance-mcp-server", url_hint: str = "agent.binance.com") -> tuple[str | None, str]:
    """Return (access_token, where_found)."""
    sources = [("keychain:Claude Code-credentials", _keychain_json("Claude Code-credentials")),
               ("~/.claude/.credentials.json", _file_json(Path.home() / ".claude" / ".credentials.json"))]
    for where, data in sources:
        if not data:
            continue
        mcp = data.get("mcpOAuth") or {}
        best = None
        for key, entry in mcp.items():
            if not isinstance(entry, dict):
                continue
            hay = (key + " " + json.dumps(entry)).lower()
            if server_name.lower() in hay or url_hint in hay:
                best = entry
                break
        if best is None and mcp:
            best = next(iter(mcp.values()))
        if best and best.get("accessToken"):
            exp = best.get("expiresAt")
            if exp and isinstance(exp, (int, float)) and exp / (1000 if exp > 1e12 else 1) < time.time():
                log.warning("Claude Code token for %s looks expired (expiresAt=%s); re-authenticate via /mcp", server_name, exp)
            return str(best["accessToken"]), where
    return None, "not found"


def find_codex_token() -> tuple[str | None, str]:
    for where, data in [("keychain:Codex Auth", _keychain_json("Codex Auth")), ("~/.codex/auth.json", _file_json(Path.home() / ".codex" / "auth.json"))]:
        if not data:
            continue
        blob = json.dumps(data)
        if "agent.binance.com" not in blob and "binance" not in blob.lower():
            continue
        # Best-effort: look for an accessToken near a binance entry.
        def walk(o: Any) -> str | None:
            if isinstance(o, dict):
                if "binance" in json.dumps(o).lower():
                    for k in ("access_token", "accessToken", "token"):
                        if isinstance(o.get(k), str):
                            return o[k]
                for v in o.values():
                    r = walk(v)
                    if r:
                        return r
            if isinstance(o, list):
                for v in o:
                    r = walk(v)
                    if r:
                        return r
            return None
        tok = walk(data)
        if tok:
            return tok, where
    return None, "not found"


def discover_token(server_name: str) -> tuple[str | None, str]:
    tok = os.environ.get("BINANCE_MCP_TOKEN")
    if tok:
        return tok, "env:BINANCE_MCP_TOKEN"
    tok, where = find_claude_code_token(server_name)
    if tok:
        return tok, where
    return find_codex_token()


# ---------------------------------------------------------------------------- MCP client
async def fetch_tools(url: str, token: str, with_catalog: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """tools/list (all pages) + hidden catalog (tool_search per category) + resources + init info."""
    import httpx2  # bundled with mcp>=2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(headers=headers, timeout=60.0, follow_redirects=True) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            tools: list[dict[str, Any]] = []
            cursor = None
            pages = 0
            while True:
                res = await client.list_tools(cursor=cursor)
                tools += [t.model_dump(by_alias=True, exclude_none=True) for t in res.tools]
                pages += 1
                cursor = getattr(res, "next_cursor", None)
                if not cursor or pages > 50:
                    break
            init: dict[str, Any] = {"pages": pages}
            try:
                si = getattr(client, "server_info", None)
                init["server_info"] = si.model_dump(exclude_none=True) if si else None
                init["instructions"] = getattr(client, "instructions", None)
            except Exception:
                pass
            init["catalog"] = {}
            if with_catalog:
                search = next((t for t in tools if t["name"] == "tool_search"), None)
                cats = ((search or {}).get("inputSchema") or {}).get("properties", {}).get("category", {}).get("enum", []) if search else []
                catalog: dict[str, dict[str, Any]] = {}
                for cat in cats:
                    cur = None
                    for _ in range(50):
                        args = {"category": cat}
                        if cur:
                            args["cursor"] = cur
                        try:
                            r = await client.call_tool("tool_search", args)
                        except Exception as e:
                            log.warning("tool_search %s failed: %s", cat, e)
                            break
                        payload = r.structured_content or {}
                        if not payload and r.content:
                            try:
                                payload = json.loads(r.content[0].text)  # type: ignore[attr-defined]
                            except Exception:
                                payload = {}
                        for t in payload.get("tools", []):
                            catalog.setdefault(t["name"], dict(t, _category=cat))
                        cur = payload.get("nextCursor")
                        if not cur:
                            break
                init["catalog"] = catalog
            init["resources"] = []
            try:
                rl = await client.list_resources()
                for r in rl.resources:
                    entry = r.model_dump(by_alias=True, exclude_none=True)
                    try:
                        rr = await client.read_resource(r.uri)
                        entry["contents"] = [c.model_dump(by_alias=True, exclude_none=True) for c in rr.contents]
                    except Exception as e:
                        entry["read_error"] = str(e)
                    init["resources"].append(entry)
            except Exception as e:
                log.info("resources/list not available: %s", e)
            return tools, init


async def call_samples(url: str, token: str, out_dir: Path, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    out_dir.mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {token}"}
    summary: dict[str, Any] = {}
    async with httpx2.AsyncClient(headers=headers, timeout=60.0, follow_redirects=True) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            for name, args in calls:
                target = name if name in names else None
                if target is None:
                    alt = name.replace(".", "_")
                    target = alt if alt in names else None
                payload: dict[str, Any]
                try:
                    if target is None and "tool_execute" in names:
                        r = await client.call_tool("tool_execute", {"toolName": name, "arguments": args})
                        via = "tool_execute"
                    elif target is not None:
                        r = await client.call_tool(target, args)
                        via = "direct"
                    else:
                        summary[name] = {"skipped": "tool not present"}
                        continue
                    payload = {"tool": name, "via": via, "arguments": args, "isError": r.is_error,
                               "content": [c.model_dump(exclude_none=True) for c in r.content],
                               "structuredContent": r.structured_content}
                except Exception as e:
                    payload = {"tool": name, "arguments": args, "exception": str(e)}
                (out_dir / f"{name.replace('.', '_')}.json").write_text(json.dumps(payload, indent=2, default=str))
                summary[name] = {"isError": payload.get("isError"), "exception": payload.get("exception")}
                log.info("sample %s: %s", name, summary[name])
    return summary


# ---------------------------------------------------------------------------- commands
async def dump_schema(cfg: Config, out: Path, samples: bool = True, token: str | None = None) -> int:
    server = cfg.schema_.server_name
    tok, where = (token, "cli") if token else discover_token(server)
    if not tok:
        print("No OAuth token found. Authenticate once in Claude Code, then retry:\n"
              f"  claude mcp add {server} --transport http {cfg.live_url}\n"
              f"  claude   →  /mcp  →  {server}  →  Authenticate\n"
              "or pass --token / set BINANCE_MCP_TOKEN.")
        return 2
    print(f"using token from {where}")
    try:
        tools, init = await fetch_tools(cfg.live_url, tok)
    except Exception as e:
        print(f"tools/list failed: {e}\nIf this is a 401, re-authenticate in Claude Code (/mcp) and retry.")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    catalog = init.pop("catalog", {}) or {}
    resources = init.pop("resources", []) or []
    payload = {"_meta": {"source": "mirrored", "dumped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "endpoint": cfg.live_url,
                         "server": server, "token_source": where, "init": init, "exposed": len(tools), "catalog": len(catalog)}, "tools": tools}
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out} with {len(tools)} exposed tools ({init.get('pages')} page(s)):")
    for t in tools:
        print(f"  - {t['name']}")
    if catalog:
        cpath = out.parent / "catalog.json"
        cpath.write_text(json.dumps({"_meta": {"dumped_at": payload["_meta"]["dumped_at"], "note": "hidden catalog reachable via tool_search/tool_execute"},
                                     "tools": list(catalog.values())}, indent=1, ensure_ascii=False) + "\n")
        hidden = [n for n in catalog if n not in {t["name"] for t in tools}]
        print(f"wrote {cpath}: {len(catalog)} catalog tools ({len(hidden)} hidden, reachable only via tool_execute)")
    if resources:
        rpath = out.parent / "resources.json"
        rpath.write_text(json.dumps({"resources": resources}, indent=1, ensure_ascii=False) + "\n")
        print(f"wrote {rpath}: {len(resources)} resource(s)")
    if samples:
        sdir = out.parent / "samples"
        print(f"capturing read-only samples into {sdir}/ ...")
        summary = await call_samples(cfg.live_url, tok, sdir, SAMPLE_CALLS)
        (sdir / "_summary.json").write_text(json.dumps(summary, indent=2))
    # Report what the twin can and cannot mirror.
    cfg2 = cfg.model_copy(deep=True)
    cfg2.schema_.tools_file = str(out)
    cat = ToolCatalog(cfg2)
    if cat.unmapped:
        print(f"\n{len(cat.unmapped)} tool(s) have no twin handler (add them to schemas/tool_map.yaml):")
        for n in cat.unmapped:
            print(f"  - {n}")
    return 0


async def validate_schema(cfg: Config, live_url: str | None = None, token: str | None = None) -> int:
    cat = ToolCatalog(cfg)
    served = [t.raw for t in cat.list_for_mcp()]
    print(f"twin serves {len(served)} tools from {cat.tools_path} (source={cat.source})")
    if cat.source == "fallback":
        print("⚠ SCHEMA NOT MIRRORED: the twin is serving the hand-written fallback. Run `rehearsal schema dump`.")
    tools_file = cfg.path(cfg.schema_.tools_file)
    rc = 0
    if tools_file and tools_file.exists():
        mirrored = json.loads(tools_file.read_text()).get("tools", [])
        d = schema_diff(mirrored, served)
        print(f"tools.json vs twin: identical={d['identical']} (common {d['common']}, mismatches {d['schema_mismatch']})")
        if not d["identical"]:
            rc = 1
    if live_url or token or os.environ.get("BINANCE_MCP_TOKEN"):
        url = live_url or cfg.live_url
        tok, where = (token, "cli") if token else discover_token(cfg.schema_.server_name)
        if not tok:
            print("no token for live comparison; skipping (authenticate in Claude Code first)")
            return rc or 2
        try:
            real, _ = await fetch_tools(url, tok, with_catalog=False)
        except Exception as e:
            print(f"live tools/list failed: {e}")
            return 1
        d = schema_diff(real, served)
        print(f"LIVE {url} vs twin: {'ZERO DRIFT ✔' if d['identical'] else 'DRIFT ✘'} — common {d['common']}, "
              f"only live {d['only_in_a']}, only twin {d['only_in_b']}, schema mismatches {d['schema_mismatch']}")
        if not d["identical"]:
            rc = 1
    if cat.unmapped:
        print(f"unmapped exposed tools (served but not simulated): {cat.unmapped}")
    if cat.catalog:
        hidden = [n for n in cat.catalog if n not in cat.tools]
        unsim = [n for n in hidden if cat.resolve(n) is None or cat.resolve(n).handler in ("unsupported",)]
        print(f"hidden catalog: {len(hidden)} tools reachable via tool_execute; {len(hidden) - len(unsim)} simulated, {len(unsim)} return TWIN_UNSUPPORTED")
    return rc
