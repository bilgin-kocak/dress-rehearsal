"""Codex CLI adapter (experimental): `codex exec --json` with the MCP server injected via -c overrides."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from rehearsal.rehearsal.adapters.base import Adapter, SessionResult, is_write_tool, restated_in, strip_mcp_prefix

log = logging.getLogger("rehearsal.runner.codex")


class CodexAdapter(Adapter):
    name = "codex"

    def run_session(self, prompt: str, mcp_url: str, session_id: str, out_dir: Path, minutes: int, max_turns: int) -> SessionResult:
        exe = shutil.which("codex")
        if not exe:
            return SessionResult(exit_status="error", error="codex CLI not found on PATH")
        server = self.cfg.schema_.server_name
        transcript = out_dir / f"transcript_{session_id}.jsonl"
        cmd = [exe, "exec", "--json", "--skip-git-repo-check",
               "-c", f'mcp_servers.{server}.url="{mcp_url}"',
               "-c", f'mcp_servers.{server}.http_headers.X-Rehearsal-Session="{session_id}"',
               "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"', prompt]
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE",)}
        t0 = time.time()
        res = SessionResult(exit_status="success")
        recent: list[str] = []
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(out_dir))
            assert proc.stdout is not None
            with transcript.open("w") as tf:
                while True:
                    if time.time() > t0 + minutes * 60:
                        proc.kill()
                        res.exit_status = "timeout"
                        break
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        continue
                    tf.write(line)
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    item = ev.get("item") or ev.get("msg") or ev
                    typ = item.get("type") or ev.get("type") or ""
                    if "message" in typ and item.get("text"):
                        recent = (recent + [str(item["text"])])[-2:]
                        res.turns += 1
                    if "mcp_tool_call" in typ or item.get("tool") or item.get("name"):
                        name = str(item.get("tool") or item.get("name") or item.get("server", "") + "." + str(item.get("tool", "")))
                        args = item.get("arguments") or item.get("input") or {}
                        write = is_write_tool(name)
                        restated = restated_in(recent, args) if write else None
                        res.tool_uses.append({"name": strip_mcp_prefix(name), "input": args, "write": write, "restated": restated})
                        if write:
                            res.confirmation["writes"] += 1
                            if restated:
                                res.confirmation["restated"] += 1
            if proc.returncode not in (0, None) and res.exit_status == "success":
                res.exit_status = "error"
                res.error = (proc.stderr.read() if proc.stderr else "")[-1500:]
        except Exception as e:  # pragma: no cover
            res.exit_status, res.error = "error", str(e)
        res.transcript_path = str(transcript)
        res.duration_ms = int((time.time() - t0) * 1000)
        return res
