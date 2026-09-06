"""Shadow mode installer: a Claude Code PostToolUse hook that mirrors live tool calls into the twin.

`rehearsal shadow install` writes to <project>/.claude/settings.json:
  hooks.PostToolUse += {matcher: "mcp__binance-mcp-server__.*", hooks: [{type: command, command: "<python> -m rehearsal.cli shadow hook --port 8765"}]}
The hook receives {session_id, tool_name, tool_input, tool_response} on stdin and POSTs it to
http://127.0.0.1:<port>/shadow/event. It never blocks or alters the live call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rehearsal.config import Config

MARKER = "dress-rehearsal-shadow"


def hook_command(cfg: Config) -> str:
    py = sys.executable
    return f'"{py}" -m rehearsal.cli shadow hook --port {cfg.server.http_port}   # {MARKER}'


def _settings_path(project: Path) -> Path:
    return project / ".claude" / "settings.json"


def _load(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text() or "{}")
        except json.JSONDecodeError:
            return {}
    return {}


def install(cfg: Config, project: Path) -> Path:
    path = _settings_path(project.resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load(path)
    hooks = data.setdefault("hooks", {})
    post = hooks.setdefault("PostToolUse", [])
    post[:] = [h for h in post if MARKER not in json.dumps(h)]
    post.append({"matcher": f"mcp__{cfg.schema_.server_name}__.*",
                 "hooks": [{"type": "command", "command": hook_command(cfg), "timeout": 10}]})
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"shadow hook installed in {path}")
    print(f"  matcher: mcp__{cfg.schema_.server_name}__.*  →  POST http://127.0.0.1:{cfg.server.http_port}/shadow/event")
    print("Keep `rehearsal serve` running (the twin re-executes every live call on the paper ledger).")
    return path


def uninstall(cfg: Config, project: Path) -> None:
    path = _settings_path(project.resolve())
    data = _load(path)
    post = data.get("hooks", {}).get("PostToolUse", [])
    n = len(post)
    post[:] = [h for h in post if MARKER not in json.dumps(h)]
    if data.get("hooks", {}).get("PostToolUse") == []:
        data["hooks"].pop("PostToolUse", None)
    path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"removed {n - len(post)} shadow hook(s) from {path}")


def status(cfg: Config, project: Path) -> None:
    path = _settings_path(project.resolve())
    data = _load(path)
    post = data.get("hooks", {}).get("PostToolUse", [])
    ours = [h for h in post if MARKER in json.dumps(h)]
    print(f"{path}: {'installed' if ours else 'not installed'}")
    for h in ours:
        print("  " + json.dumps(h))
    try:
        import httpx
        r = httpx.get(f"http://127.0.0.1:{cfg.server.http_port}/api/shadow", timeout=2.0)
        s = r.json()
        print(f"twin: reachable, shadow events received: {s.get('events')}, active: {s.get('active')}")
    except Exception:
        print(f"twin: not reachable on port {cfg.server.http_port} (start `rehearsal serve`)")


def forward_stdin(port: int) -> None:
    """Hook entrypoint. Must never fail the live tool call: swallow every error, exit 0."""
    try:
        raw = sys.stdin.read()
        body = json.loads(raw) if raw.strip() else {}
        import httpx
        httpx.post(f"http://127.0.0.1:{port}/shadow/event", json=body, timeout=8.0)
    except Exception as e:  # pragma: no cover
        print(f"shadow hook: {e}", file=sys.stderr)
    sys.exit(0)
