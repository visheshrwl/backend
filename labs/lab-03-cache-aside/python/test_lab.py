#!/usr/bin/env python3
"""
Lab 03 checks — exercises real Postgres + Redis through labkit.

By default tests your work in stub.py:
    python -m unittest test_lab.py

Check the reference instead:
    LAB_MODULE=solution python -m unittest test_lab.py

Requires the lab services (postgres, redis). In Codespaces/Gitpod they are
already up; locally run `docker compose -f .devcontainer/docker-compose.yml up -d`.
"""

import importlib
import os
import threading
import unittest

from labkit import db, cache

lab = importlib.import_module(os.environ.get("LAB_MODULE", "stub"))


class TestCacheAside(unittest.TestCase):
    def setUp(self):
        cache.flush()
        db.execute("UPDATE users SET plan = 'pro' WHERE id = 1")
        db.reset_counters()

    def test_first_read_hits_db_and_populates_cache(self):
        user = lab.get_user_profile(1)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], 1)
        self.assertGreater(db.query_count, 0, "first read should hit Postgres")
        self.assertTrue(cache.exists("user:1"), "first read should populate the cache")

    def test_second_read_served_from_cache(self):
        lab.get_user_profile(1)
        db.reset_counters()
        user = lab.get_user_profile(1)
        self.assertEqual(user["id"], 1)
        self.assertEqual(db.query_count, 0, "a cache hit must not query Postgres")

    def test_update_invalidates_cache(self):
        lab.get_user_profile(1)
        lab.update_user_plan(1, "enterprise")
        self.assertFalse(cache.exists("user:1"), "update must invalidate the cache")
        user = lab.get_user_profile(1)
        self.assertEqual(user["plan"], "enterprise")

    def test_missing_user_returns_none(self):
        self.assertIsNone(lab.get_user_profile(999999))

    def test_singleflight_collapses_concurrent_misses(self):
        """Part 2 — 50 concurrent cold-cache reads must cause exactly one DB query."""
        cache.flush()
        db.reset_counters()

        results = []
        barrier = threading.Barrier(50)

        def worker():
            barrier.wait()  # release all 50 threads on a cold cache at once
            results.append(lab.get_user_profile_singleflight(1))

        threads = [threading.Thread(target=worker) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 50)
        self.assertTrue(all(r and r["id"] == 1 for r in results), "all readers get the user")
        self.assertEqual(
            db.query_count, 1,
            "single-flight: only one thread may query Postgres on a cold miss",
        )


if __name__ == "__main__":
    unittest.main()
