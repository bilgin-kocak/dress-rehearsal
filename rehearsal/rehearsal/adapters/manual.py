"""Manual adapter: the user drives the agent; the runner just tags the time window as a session."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from rehearsal.rehearsal.adapters.base import Adapter, SessionResult


class ManualAdapter(Adapter):
    name = "manual"

    def run_session(self, prompt: str, mcp_url: str, session_id: str, out_dir: Path, minutes: int, max_turns: int) -> SessionResult:
        server = self.cfg.schema_.server_name
        print(f"\n=== MANUAL SESSION {session_id} ===", file=sys.stderr)
        print(f"Point your agent at the twin:  claude mcp add {server} --transport http {mcp_url}", file=sys.stderr)
        print(f"(or the generated config: {self.write_mcp_config(out_dir / f'mcp_{session_id}.json', server, mcp_url, session_id)})", file=sys.stderr)
        print(f"Run the strategy prompt, then press Enter here (auto-ends in {minutes} min).", file=sys.stderr)
        t0 = time.time()
        try:
            import select
            while time.time() - t0 < minutes * 60:
                r, _, _ = select.select([sys.stdin], [], [], 1.0)
                if r:
                    sys.stdin.readline()
                    break
        except Exception:
            time.sleep(minutes * 60)
        return SessionResult(exit_status="manual", duration_ms=int((time.time() - t0) * 1000), confirmation={"writes": 0, "restated": 0})
