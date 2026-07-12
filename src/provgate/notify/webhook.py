"""Best-effort webhook POST of a sync summary. Never raises.

A dead/slow/erroring webhook must never affect sync correctness — this returns a
bool and swallows every exception, logging at warning level. The caller ignores
the return value beyond logging.
"""

from __future__ import annotations

import logging

import httpx

_log = logging.getLogger("provgate.notify")


def post_summary(
    url: str,
    content: str,
    *,
    timeout_s: float,
    http: httpx.Client | None = None,
) -> bool:
    client = http if http is not None else httpx.Client(timeout=timeout_s)
    try:
        resp = client.post(url, json={"content": content})
        if resp.status_code // 100 == 2:
            return True
        _log.warning("webhook post returned HTTP %s", resp.status_code)
        return False
    except Exception as e:  # best-effort: a notify failure must never break a sync
        _log.warning("webhook post failed: %s", e)
        return False
    finally:
        if http is None:
            client.close()
