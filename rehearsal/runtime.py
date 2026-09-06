"""Runtime: builds engine + catalog + twin + dashboard app for `serve`, `run`, `demo`."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

from rehearsal.config import Config
from rehearsal.engine.engine import Engine, build_engine
from rehearsal.server.catalog import ToolCatalog
from rehearsal.server.mcp_server import Twin
from rehearsal.shadow.receiver import ShadowReceiver

log = logging.getLogger("rehearsal.runtime")


class Runtime:
    def __init__(self, cfg: Config, db_path: str | Path | None = None):
        self.cfg = cfg
        self.db_path = db_path or cfg.path(cfg.engine.db_path)
        self.engine: Engine = build_engine(cfg, self.db_path)
        self.catalog = ToolCatalog(cfg)
        self.twin = Twin(cfg, self.engine, self.catalog)
        self.shadow = ShadowReceiver(self.twin, cfg)
        self._dash = None
        self._app = None

    def mode_label(self) -> str:
        return "REPLAY" if self.cfg.market.mode == "replay" else "PAPER"

    def start(self) -> None:
        self.engine.feed.start()
        # Pre-subscribe the allowlisted symbols so the first agent call is instant.
        for s in self.cfg.policy.symbol_allowlist:
            try:
                self.engine.feed.subscribe("spot", s)
                if self.engine.feed.symbol_info("usdm", s):
                    self.engine.feed.subscribe("usdm", s)
            except Exception as e:  # pragma: no cover
                log.debug("presubscribe %s: %s", s, e)
        self.engine.snapshot_equity(force=True)

    def app(self):
        if self._app is None:
            from rehearsal.dashboard.app import create_app

            self._app = create_app(self.twin, self.cfg, self.shadow, mount_mcp=True)
        return self._app

    def start_dashboard_thread(self) -> None:
        from rehearsal.dashboard.app import DashboardThread

        self._dash = DashboardThread(self.app(), self.cfg.server.http_host, self.cfg.server.http_port)
        self._dash.start()

    def serve_http_blocking(self) -> None:
        import uvicorn

        uvicorn.run(self.app(), host=self.cfg.server.http_host, port=self.cfg.server.http_port, log_level="warning")

    def stop(self) -> None:
        if self._dash:
            self._dash.stop()
        try:
            self.engine.close()
        except Exception:
            pass
