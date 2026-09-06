"""Tool catalog: loads tools.json (mirrored from the real server) or the fallback, plus tool_map.yaml."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rehearsal.config import Config

# Products seen on the real server; used to normalise "spot_newOrder" <-> "spot.newOrder".
PRODUCTS = ["futures_usds", "futures_coin", "sub_account", "spot", "margin", "convert", "wallet", "analysis", "algo", "earn"]

MARKET_DATA_HINTS = ("ticker", "depth", "kline", "price", "exchangeinfo", "fundingrate", "markprice", "trades",
                     "openinterest", "avgprice", "bookticker", "premium")


@dataclass
class ToolSpec:
    name: str  # canonical (as served)
    category: str
    handler: str
    market: str | None = None
    write: bool = False
    mapped: bool = True
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    title: str | None = None
    annotations: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def normalize(name: str) -> str:
    """Return the dotted canonical form for either 'spot_newOrder' or 'spot.newOrder'."""
    if "." in name:
        return name
    for p in PRODUCTS:
        if name.startswith(p + "_") and len(name) > len(p) + 1:
            return p + "." + name[len(p) + 1:]
    return name


def underscored(name: str) -> str:
    return name.replace(".", "_")


class ToolCatalog:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.source = "fallback"
        self.mirrored_at: str | None = None
        self.tools_path: Path | None = None
        self.tools: dict[str, ToolSpec] = {}
        self._index: dict[str, str] = {}
        self.unmapped_policy = "passthrough_public"
        self.map: dict[str, dict[str, Any]] = {}
        self.unmapped: list[str] = []
        self.load()

    # ------------------------------------------------------------------ loading
    def load(self) -> None:
        tools_file = self.cfg.path(self.cfg.schema_.tools_file)
        fallback = self.cfg.path(self.cfg.schema_.fallback_file)
        data: dict[str, Any] | None = None
        if tools_file and tools_file.exists():
            data = json.loads(tools_file.read_text())
            self.source = "mirrored"
            self.tools_path = tools_file
            self.mirrored_at = (data.get("_meta") or {}).get("dumped_at") or (data.get("_meta") or {}).get("generated")
        elif fallback and fallback.exists():
            data = json.loads(fallback.read_text())
            self.source = "fallback"
            self.tools_path = fallback
            self.mirrored_at = None
        else:
            data = {"tools": []}
        raw_tools = data.get("tools") or data.get("result", {}).get("tools") or []

        map_file = self.cfg.path(self.cfg.schema_.tool_map)
        m: dict[str, Any] = {}
        if map_file and map_file.exists():
            m = yaml.safe_load(map_file.read_text()) or {}
        self.map = m.get("tools", {})
        self.unmapped_policy = m.get("unmapped_policy", "passthrough_public")
        map_index = {normalize(k): v for k, v in self.map.items()}

        self.tools.clear()
        self._index.clear()
        self.unmapped = []
        for t in raw_tools:
            name = t["name"]
            canon = normalize(name)
            spec_map = map_index.get(canon)
            if spec_map:
                spec = ToolSpec(name=name, category=spec_map.get("category", "unsupported"), handler=spec_map.get("handler", "unsupported"),
                                market=spec_map.get("market"), write=bool(spec_map.get("write", False)))
            else:
                spec = self._infer(name)
                self.unmapped.append(name)
            spec.description = t.get("description", "") or ""
            spec.input_schema = t.get("inputSchema") or t.get("input_schema") or {"type": "object", "properties": {}}
            spec.title = t.get("title")
            spec.annotations = t.get("annotations")
            spec.raw = t
            self.tools[name] = spec
            self._index[name] = name
            self._index[canon] = name
            self._index[underscored(canon)] = name

    def _infer(self, name: str) -> ToolSpec:
        low = name.lower()
        if self.unmapped_policy == "passthrough_public" and any(h in low for h in MARKET_DATA_HINTS):
            return ToolSpec(name=name, category="market_data", handler="passthrough_public", mapped=False)
        write = any(w in low for w in ("neworder", "cancel", "delete", "transfer", "accept", "leverage", "margintype", "borrow", "repay"))
        return ToolSpec(name=name, category="unsupported", handler="unsupported", write=write, mapped=False)

    # ------------------------------------------------------------------ lookup
    def resolve(self, name: str) -> ToolSpec | None:
        key = self._index.get(name) or self._index.get(normalize(name)) or self._index.get(underscored(normalize(name)))
        if key:
            return self.tools[key]
        # Catalog-only name (tool_execute of something not in tools/list): infer.
        canon = normalize(name)
        spec_map = {normalize(k): v for k, v in self.map.items()}.get(canon)
        if spec_map:
            return ToolSpec(name=canon, category=spec_map.get("category", "unsupported"), handler=spec_map.get("handler", "unsupported"),
                            market=spec_map.get("market"), write=bool(spec_map.get("write", False)), mapped=True)
        return None

    def list_for_mcp(self) -> list[ToolSpec]:
        return list(self.tools.values())

    def search(self, category: str | None, query: str | None = None) -> list[dict[str, Any]]:
        cat = (category or "").lower()
        out = []
        for spec in self.tools.values():
            n = spec.name.lower()
            if spec.name in ("tool_search", "tool_execute"):
                continue
            ok = True
            if cat == "market":
                ok = spec.category == "market_data"
            elif cat == "account":
                ok = spec.category == "account"
            elif cat == "trade":
                ok = spec.category == "trade"
            elif cat == "transfer":
                ok = spec.category == "transfer" or "transfer" in n
            elif cat == "convert":
                ok = n.startswith("convert")
            elif cat == "futures":
                ok = n.startswith("futures")
            elif cat == "margin":
                ok = n.startswith("margin")
            elif cat == "wallet":
                ok = n.startswith("wallet")
            elif cat == "analysis":
                ok = n.startswith("analysis")
            elif cat in ("", "all"):
                ok = True
            if ok and query:
                q = query.lower()
                ok = q in n or q in spec.description.lower()
            if ok:
                out.append({"name": spec.name, "description": spec.description, "inputSchema": spec.input_schema})
        return out

    def status(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "tools_file": str(self.tools_path) if self.tools_path else None,
            "mirrored_at": self.mirrored_at,
            "tool_count": len(self.tools),
            "unmapped": self.unmapped,
            "write_tools": [t.name for t in self.tools.values() if t.write],
        }


def schema_diff(a_tools: list[dict[str, Any]], b_tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare two tools/list payloads by name and inputSchema (descriptions ignored)."""
    a = {t["name"]: t for t in a_tools}
    b = {t["name"]: t for t in b_tools}
    only_a = sorted(set(a) - set(b))
    only_b = sorted(set(b) - set(a))
    schema_mismatch = []
    for n in sorted(set(a) & set(b)):
        sa = a[n].get("inputSchema") or a[n].get("input_schema") or {}
        sb = b[n].get("inputSchema") or b[n].get("input_schema") or {}
        if json.dumps(sa, sort_keys=True) != json.dumps(sb, sort_keys=True):
            schema_mismatch.append(n)
    return {"only_in_a": only_a, "only_in_b": only_b, "schema_mismatch": schema_mismatch,
            "identical": not only_a and not only_b and not schema_mismatch, "common": len(set(a) & set(b))}


_slug = re.compile(r"[^a-zA-Z0-9_.-]")


def safe_name(name: str) -> str:
    return _slug.sub("_", name)
