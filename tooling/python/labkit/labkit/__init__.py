"""
labkit — the zero-setup platform layer for the backend labs.

    from labkit import db, cache

`db` is a ready Postgres handle, `cache` a ready Redis handle. Connections are
already wired from the environment the lab platform provides — no setup, no
connection strings, no devops.
"""

from .cache import cache
from .db import db


def ping() -> dict:
    """Health check for both layers."""
    return {"postgres": db.ping(), "redis": cache.ping()}


def reset():
    """Reset state between test runs: flush the cache, reset query counters."""
    cache.flush()
    db.reset_counters()


__all__ = ["db", "cache", "ping", "reset"]
