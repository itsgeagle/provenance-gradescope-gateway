"""A minimal repeat-with-sleep loop for hosts without an external scheduler."""

from __future__ import annotations

import logging
from collections.abc import Callable

_log = logging.getLogger("provgate.loop")


def run_loop(
    sync_once: Callable[[], None],
    interval_s: float,
    *,
    sleep: Callable[[float], None],
    max_iters: int | None = None,
) -> int:
    iters = 0
    while max_iters is None or iters < max_iters:
        try:
            sync_once()
        except Exception:  # a bad pass must never kill the loop
            _log.exception("sync pass failed; continuing")
        iters += 1
        sleep(interval_s)
    return iters
