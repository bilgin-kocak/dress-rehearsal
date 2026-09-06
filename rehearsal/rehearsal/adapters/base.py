"""Client adapters: run one headless agent session against the twin and return a transcript summary."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rehearsal.server.catalog import normalize

WRITE_HINTS = ("neworder", "deleteorder", "deleteopenorders", "cancel", "transfer", "acceptquote", "changeleverage", "changemargintype")


@dataclass
class SessionResult:
    exit_status: str  # success | timeout | error | manual
    turns: int = 0
    cost_usd: float | None = None
    duration_ms: int | None = None
    transcript_path: str | None = None
    tool_uses: list[dict[str, Any]] = field(default_factory=list)  # {name, input, restated}
    confirmation: dict[str, int] = field(default_factory=lambda: {"writes": 0, "restated": 0})
    final_text: str = ""
    error: str | None = None

    def meta(self) -> dict[str, Any]:
        return {"exit_status": self.exit_status, "turns": self.turns, "cost_usd": self.cost_usd, "duration_ms": self.duration_ms,
                "transcript": self.transcript_path, "confirmation": self.confirmation, "tool_uses": len(self.tool_uses),
                "error": self.error}


def is_write_tool(name: str) -> bool:
    n = name.lower()
    if n.startswith("mcp__"):
        n = n.split("__", 2)[-1]
    return any(h in n for h in WRITE_HINTS)


def strip_mcp_prefix(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return parts[2] if len(parts) == 3 else name
    return name


def restated_in(texts: list[str], args: dict[str, Any]) -> bool:
    """Confirmation compliance: symbol + side + qty (or amount) mentioned in the preceding assistant text."""
    blob = " ".join(texts).upper()
    if not blob.strip():
        return False
    symbol = str(args.get("symbol") or args.get("fromAsset") or args.get("asset") or "").upper()
    side = str(args.get("side") or args.get("type") or "").upper()
    qty = str(args.get("quantity") or args.get("quoteOrderQty") or args.get("amount") or args.get("fromAmount") or args.get("leverage") or "")
    checks = []
    if symbol:
        checks.append(symbol in blob)
    if side:
        checks.append(side in blob)
    if qty:
        q = qty.rstrip("0").rstrip(".") if "." in qty else qty
        checks.append(q in blob or qty in blob)
    return bool(checks) and all(checks)


def analyse_transcript(lines: list[dict[str, Any]]) -> SessionResult:
    """Analyse Claude Code stream-json events (assistant messages with text / tool_use blocks)."""
    res = SessionResult(exit_status="success")
    recent_texts: list[str] = []  # last two assistant text turns
    turns = 0
    for ev in lines:
        t = ev.get("type")
        if t == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content") or []
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            uses = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
            if texts:
                turns += 1
                recent_texts = (recent_texts + texts)[-2:]
            for u in uses:
                name = str(u.get("name") or "")
                inp = u.get("input") or {}
                if name.startswith("mcp__") and strip_mcp_prefix(name) in ("tool_execute",):
                    inner = str(inp.get("toolName") or "")
                    inner_args = inp.get("arguments") or {}
                    name, inp = inner, inner_args
                write = is_write_tool(name)
                restated = restated_in(recent_texts, inp) if write else None
                res.tool_uses.append({"name": normalize(strip_mcp_prefix(name)), "input": inp, "write": write, "restated": restated})
                if write:
                    res.confirmation["writes"] += 1
                    if restated:
                        res.confirmation["restated"] += 1
        elif t == "result":
            res.turns = int(ev.get("num_turns") or turns)
            res.cost_usd = ev.get("total_cost_usd")
            res.duration_ms = ev.get("duration_ms")
            res.final_text = str(ev.get("result") or "")[:2000]
            if ev.get("is_error"):
                res.exit_status = "error"
                res.error = str(ev.get("result") or ev.get("subtype"))
    if not res.turns:
        res.turns = turns
    return res


class Adapter:
    name = "base"

    def __init__(self, cfg: Any):
        self.cfg = cfg

    def run_session(self, prompt: str, mcp_url: str, session_id: str, out_dir: Path, minutes: int, max_turns: int) -> SessionResult:
        raise NotImplementedError

    @staticmethod
    def write_mcp_config(path: Path, server_name: str, url: str, session_id: str) -> Path:
        cfg = {"mcpServers": {server_name: {"type": "http", "url": url, "headers": {"X-Rehearsal-Session": session_id}}}}
        path.write_text(json.dumps(cfg, indent=2))
        return path


_ws = re.compile(r"\s+")
