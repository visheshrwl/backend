#!/usr/bin/env python3
"""
Lab 03: Cache-Aside (lazy loading) — reference solution.

Postgres is the source of truth; Redis is a lazy-loaded cache in front of it.
(In cache-aside the *application* manages the cache — distinct from read-through,
where the cache layer itself loads on a miss. See the chapter for both.)
You never open a connection — `labkit` hands you ready `db` and `cache` layers.

Run:   python solution.py
Test:  python -m unittest test_lab.py
"""

import threading

from labkit import db, cache

CACHE_TTL_SECONDS = 60


def get_user_profile(user_id: int) -> dict | None:
    """
    Cache-aside read:
      1. Look in the cache first.
      2. On a miss, read Postgres (source of truth) and populate the cache.
      3. On a hit, return the cached copy without touching Postgres.
    """
    key = f"user:{user_id}"

    cached = cache.get_json(key)
    if cached is not None:
        return cached

    row = db.queryone(
        "SELECT id, name, email, plan FROM users WHERE id = %s",
        (user_id,),
    )
    if row is None:
        return None

    cache.set_json(key, row, ttl=CACHE_TTL_SECONDS)
    return row


def update_user_plan(user_id: int, plan: str) -> None:
    """
    Write-through-then-invalidate:
      1. Write to Postgres (source of truth).
      2. Invalidate the cached copy so the next read refills it.
    """
    db.execute(
        "UPDATE users SET plan = %s, updated_at = now() WHERE id = %s",
        (plan, user_id),
    )
    cache.delete(f"user:{user_id}")


# ──────────────────────────────────────────────
# PART 2 — Cache stampede / thundering herd protection (single-flight)
# ──────────────────────────────────────────────

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _key_lock(key: str) -> threading.Lock:
    """One lock per cache key, created safely under a global guard."""
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def get_user_profile_singleflight(user_id: int) -> dict | None:
    """
    Like get_user_profile, but when a hot key is cold and many requests miss at
    once, only ONE of them queries Postgres — the rest wait and reuse its result.
    This prevents a cache stampede / thundering herd on the database.
    """
    key = f"user:{user_id}"

    cached = cache.get_json(key)
    if cached is not None:
        return cached

    with _key_lock(key):
        # Double-check: another thread may have populated the cache while we waited.
        cached = cache.get_json(key)
        if cached is not None:
            return cached

        row = db.queryone(
            "SELECT id, name, email, plan FROM users WHERE id = %s",
            (user_id,),
        )
        if row is None:
            return None

        cache.set_json(key, row, ttl=CACHE_TTL_SECONDS)
        return row


def main():
    cache.flush()
    db.reset_counters()

    print("First read (cold cache):")
    print(" ", get_user_profile(1))
    print(f"  Postgres queries so far: {db.query_count}")

    print("\nSecond read (warm cache):")
    print(" ", get_user_profile(1))
    print(f"  Postgres queries so far: {db.query_count}  <- unchanged: served from Redis")

    print("\nUpdate plan -> cache invalidated, next read refills:")
    update_user_plan(1, "enterprise")
    print(" ", get_user_profile(1))

    print("\nSingle-flight: 50 concurrent cold-cache reads ->")
    cache.flush()
    db.reset_counters()
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()
        get_user_profile_singleflight(1)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"  Postgres queries: {db.query_count}  <- one DB hit, not 50 (stampede avoided)")


if __name__ == "__main__":
    main()
