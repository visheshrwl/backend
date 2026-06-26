#!/usr/bin/env python3
"""
Lab 03: Cache-Aside (lazy loading) — YOUR TURN.

Postgres is the source of truth; Redis sits in front as a cache. The platform
already gives you connected `db` and `cache` handles — you write zero setup.

Implement (two parts):
  Part 1 — cache-aside:
    get_user_profile(user_id)       -> cache first, fall back to Postgres, populate
    update_user_plan(user_id, plan) -> write Postgres, then invalidate the cache
  Part 2 — stampede protection:
    get_user_profile_singleflight(user_id) -> when many requests miss a cold hot
    key at once, only ONE queries Postgres; the rest wait and reuse its result.

Prove it works:
  python -m unittest test_lab.py     # checks cache hits + single-flight DB hits
  python solution.py                  # see the reference behaviour

Useful labkit API:
  db.queryone(sql, params) -> dict | None     db.query_count (read counter)
  db.execute(sql, params)  -> rowcount
  cache.get_json(key)      cache.set_json(key, value, ttl=...)
  cache.delete(key)        cache.exists(key)
"""

import threading

from labkit import db, cache

CACHE_TTL_SECONDS = 60


def get_user_profile(user_id: int) -> dict | None:
    """
    TODO: Implement cache-aside reads.
      1. key = f"user:{user_id}" — return cache.get_json(key) if present.
      2. On miss, SELECT id, name, email, plan FROM users WHERE id = %s.
      3. If the row exists, store it with cache.set_json(key, row, ttl=...).
      4. Return None when the user does not exist.
    """
    raise NotImplementedError("Implement get_user_profile")


def update_user_plan(user_id: int, plan: str) -> None:
    """
    TODO:
      1. UPDATE users SET plan = %s, updated_at = now() WHERE id = %s.
      2. cache.delete(f"user:{user_id}") so the next read refills the cache.
    """
    raise NotImplementedError("Implement update_user_plan")


# ── Part 2 — stampede protection (single-flight) ─────────────────────

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    """One lock per cache key, created safely under a global guard. (given)"""
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def get_user_profile_singleflight(user_id: int) -> dict | None:
    """
    TODO: stampede-safe cache-aside read.
      1. key = f"user:{user_id}" — return cache.get_json(key) if present.
      2. On a miss, acquire _key_lock(key) (with the lock as a context manager).
      3. Inside the lock, DOUBLE-CHECK the cache — another thread may have filled
         it while you waited; if so, return it without querying Postgres.
      4. Otherwise query Postgres once, populate the cache, and return the row
         (None if the user does not exist).
    The double-check inside the lock is what collapses the herd to one DB query.
    """
    raise NotImplementedError("Implement get_user_profile_singleflight")


def main():
    try:
        print(get_user_profile(1))
    except NotImplementedError as e:
        print(f"Not implemented yet: {e}")


if __name__ == "__main__":
    main()
