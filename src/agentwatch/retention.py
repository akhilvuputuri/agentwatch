"""Retention: deletes traces older than a number of days, in the background."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from agentwatch.store import Store

log = logging.getLogger(__name__)

DAY_NS = 24 * 3600 * 1_000_000_000


class Retention:
    """Prunes traces that ended more than `days` ago: at start, then every `interval_s`."""

    def __init__(
        self,
        store: Store,
        days: int,
        *,
        clock: Callable[[], int] = time.time_ns,
    ) -> None:
        if not 1 <= days <= 36500:
            raise ValueError("retention must be from one day to 100 years")
        self.store = store
        self.days = days
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        removed = self.store.prune(self.clock() - self.days * DAY_NS)
        if removed:
            log.info("deleted %d traces older than %d days", removed, self.days)
        return removed

    def start(self, interval_s: float = 6 * 3600) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while True:
                try:
                    self.run_once()
                except Exception:  # a failed pass is retried at the next interval
                    log.exception("retention pass failed")
                if self._stop.wait(interval_s):
                    return

        self._thread = threading.Thread(target=loop, name="agentwatch-retention", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            if not self._thread.is_alive():
                self._thread = None

    @property
    def running(self) -> bool:
        """Whether the background thread is still working (after stop(): mid-call)."""
        return self._thread is not None and self._thread.is_alive()
