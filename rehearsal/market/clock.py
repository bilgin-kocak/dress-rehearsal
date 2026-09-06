"""Virtual clock. Engine code must take `now()` from here, never from time.time()."""

from __future__ import annotations

import threading
import time


class Clock:
    """Wall clock (live) or replay-driven virtual clock (ms since epoch)."""

    def __init__(self, virtual: bool = False, start_ms: int | None = None):
        self.virtual = virtual
        self._lock = threading.Lock()
        self._virtual_ms = start_ms if start_ms is not None else int(time.time() * 1000)
        self._offset_ms = 0

    def now_ms(self) -> int:
        if self.virtual:
            with self._lock:
                return self._virtual_ms
        return int(time.time() * 1000) + self._offset_ms

    def now_s(self) -> float:
        return self.now_ms() / 1000.0

    def set_ms(self, ms: int) -> None:
        """Advance the virtual clock (replay only). Never moves backwards."""
        with self._lock:
            if ms > self._virtual_ms:
                self._virtual_ms = ms

    def force_ms(self, ms: int) -> None:
        """Rewind/jump the virtual clock (replay restart)."""
        with self._lock:
            self._virtual_ms = ms

    def advance_ms(self, delta: int) -> None:
        with self._lock:
            self._virtual_ms += max(0, delta)

    def iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now_ms() / 1000))
