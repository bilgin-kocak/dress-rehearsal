"""Claude Code adapter: `claude -p <prompt> --mcp-config <generated.json> --output-format stream-json`.

The generated MCP config uses the SAME server name as the real one (binance-mcp-server) so the
strategy prompt never changes between rehearsal and live.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from rehearsal.rehearsal.adapters.base import Adapter, SessionResult, analyse_transcript

log = logging.getLogger("rehearsal.runner.claude")

DISALLOWED = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite"]


FALLBACK_SERVER_NAME = "binance-twin"


def _real_server_registered(exe: str, name: str) -> bool:
    """Claude Code caches an OAuth 'needs-auth' state per server NAME. If the real server is registered
    under `name` (any scope), a twin with the same name is refused even with --strict-mcp-config."""
    try:
        r = subprocess.run([exe, "mcp", "get", name], capture_output=True, text=True, timeout=20,
                           env={k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")})
        return r.returncode == 0 and "agent.binance.com" in (r.stdout + r.stderr)
    except Exception:
        return False


class ClaudeCodeAdapter(Adapter):
    name = "claude-code"

    def run_session(self, prompt: str, mcp_url: str, session_id: str, out_dir: Path, minutes: int, max_turns: int) -> SessionResult:
        exe = shutil.which("claude")
        if not exe:
            return SessionResult(exit_status="error", error="claude CLI not found on PATH")
        server_name = self.cfg.schema_.server_name
        if _real_server_registered(exe, server_name):
            log.warning("Claude Code has the LIVE server registered as '%s' (needs-auth is cached per name); "
                        "rehearsing under the MCP name '%s' instead. Tool names stay identical (spot.newOrder, ...).",
                        server_name, FALLBACK_SERVER_NAME)
            server_name = FALLBACK_SERVER_NAME
        res = self._launch(exe, prompt, mcp_url, session_id, out_dir, minutes, max_turns, server_name)
        if res.error == "needs-auth" and server_name != FALLBACK_SERVER_NAME:
            log.warning("Claude Code refused '%s' (needs-auth cached); retrying as '%s'", server_name, FALLBACK_SERVER_NAME)
            res = self._launch(exe, prompt, mcp_url, session_id, out_dir, minutes, max_turns, FALLBACK_SERVER_NAME)
        return res

    def _launch(self, exe: str, prompt: str, mcp_url: str, session_id: str, out_dir: Path, minutes: int, max_turns: int,
                server_name: str) -> SessionResult:
        mcp_cfg = self.write_mcp_config(out_dir / f"mcp_{session_id}.json", server_name, mcp_url, session_id)
        transcript = out_dir / f"transcript_{session_id}.jsonl"
        cmd = [exe, "-p", prompt, "--mcp-config", str(mcp_cfg), "--strict-mcp-config", "--output-format", "stream-json",
               "--verbose", "--max-turns", str(max_turns), "--allowedTools", f"mcp__{server_name}",
               "--disallowedTools", *DISALLOWED]
        if self.cfg.runner.model:
            cmd += ["--model", self.cfg.runner.model]
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
        log.info("session %s: launching claude -p as MCP '%s' (max_turns=%s, timeout=%smin)", session_id, server_name, max_turns, minutes)
        t0 = time.time()
        lines: list[dict] = []
        status = "success"
        err = None
        with transcript.open("w") as tf:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                        text=True, env=env, cwd=str(out_dir))
                deadline = t0 + minutes * 60
                assert proc.stdout is not None
                while True:
                    if time.time() > deadline:
                        proc.kill()
                        status = "timeout"
                        break
                    line = proc.stdout.readline()
                    if not line:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                        continue
                    tf.write(line)
                    tf.flush()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    lines.append(ev)
                    _log_event(ev)
                    if ev.get("type") == "system" and ev.get("subtype") == "init":
                        st = {s.get("name"): s.get("status") for s in ev.get("mcp_servers") or []}
                        if st.get(server_name) == "needs-auth":
                            proc.kill()
                            status, err = "error", "needs-auth"
                            break
                        if st.get(server_name) not in ("connected", None):
                            log.warning("MCP server status: %s", st)
                stderr = proc.stderr.read() if proc.stderr else ""
                if proc.returncode not in (0, None) and status == "success":
                    status = "error"
                    err = (stderr or "")[-1500:]
                    log.warning("claude exited %s: %s", proc.returncode, err)
            except Exception as e:  # pragma: no cover
                status, err = "error", str(e)
        res = analyse_transcript(lines)
        if status != "success":
            res.exit_status = status
            res.error = res.error or err
        res.transcript_path = str(transcript)
        res.duration_ms = res.duration_ms or int((time.time() - t0) * 1000)
        log.info("session %s: %s turns=%s writes=%s restated=%s cost=%s", session_id, res.exit_status, res.turns,
                 res.confirmation["writes"], res.confirmation["restated"], res.cost_usd)
        return res


def _log_event(ev: dict) -> None:
    t = ev.get("type")
    if t == "assistant":
        for c in (ev.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                log.info("  → %s %s", c.get("name"), json.dumps(c.get("input"))[:160])
            elif isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                log.info("  💬 %s", c["text"].strip().replace("\n", " ")[:160])
    elif t == "result":
        log.info("  ■ result: %s turns=%s cost=%s", ev.get("subtype"), ev.get("num_turns"), ev.get("total_cost_usd"))
