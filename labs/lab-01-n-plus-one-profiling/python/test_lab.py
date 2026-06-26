#!/usr/bin/env python3
"""
Lab 01 checks.

By default this tests your work in stub.py:
    python -m unittest test_lab.py

To check against the reference solution instead:
    LAB_MODULE=solution python -m unittest test_lab.py
"""

import importlib
import os
import unittest

lab = importlib.import_module(os.environ.get("LAB_MODULE", "stub"))


class TestNPlusOne(unittest.TestCase):
    def setUp(self):
        self.conn = lab.create_database()
        self.tracker = lab.QueryTracker(self.conn)

    def tearDown(self):
        self.conn.close()

    @staticmethod
    def _normalize(results):
        return sorted(
            ({"id": u["id"], "post_count": len(u["posts"])} for u in results),
            key=lambda x: x["id"],
        )

    def test_join_uses_one_query(self):
        self.tracker.reset()
        result = lab.fetch_users_with_posts_join(self.tracker)
        self.assertEqual(self.tracker.count, 1, "JOIN must run exactly 1 query")
        self.assertEqual(len(result), 100)

    def test_in_batch_uses_two_queries(self):
        self.tracker.reset()
        result = lab.fetch_users_with_posts_in_batch(self.tracker)
        self.assertEqual(self.tracker.count, 2, "IN batch must run exactly 2 queries")
        self.assertEqual(len(result), 100)

    def test_all_approaches_return_identical_data(self):
        self.tracker.reset()
        n1 = lab.fetch_users_with_posts_n_plus_one(self.tracker)
        self.tracker.reset()
        join = lab.fetch_users_with_posts_join(self.tracker)
        self.tracker.reset()
        batch = lab.fetch_users_with_posts_in_batch(self.tracker)

        base = self._normalize(n1)
        self.assertEqual(base, self._normalize(join), "JOIN data must match N+1")
        self.assertEqual(base, self._normalize(batch), "IN batch data must match N+1")
        self.assertEqual(sum(u["post_count"] for u in base), 500)


class TestDataLoader(unittest.IsolatedAsyncioTestCase):
    """Part 2 — N async load() calls must collapse into one batched posts query."""

    def setUp(self):
        self.conn = lab.create_database()
        self.tracker = lab.QueryTracker(self.conn)

    def tearDown(self):
        self.conn.close()

    async def test_dataloader_batches_to_two_queries(self):
        self.tracker.reset()
        result = await lab.fetch_users_with_posts_dataloader(self.tracker)
        self.assertEqual(len(result), 100)
        self.assertEqual(
            self.tracker.count, 2,
            "DataLoader must run 1 users query + 1 batched posts query, regardless of N",
        )
        total_posts = sum(len(u["posts"]) for u in result)
        self.assertEqual(total_posts, 500)

    async def test_dataloader_data_matches_n_plus_one(self):
        self.tracker.reset()
        baseline = lab.fetch_users_with_posts_n_plus_one(self.tracker)
        result = await lab.fetch_users_with_posts_dataloader(self.tracker)

        def by_id(rows):
            return sorted(
                ({"id": u["id"], "post_count": len(u["posts"])} for u in rows),
                key=lambda x: x["id"],
            )

        self.assertEqual(by_id(baseline), by_id(result))


if __name__ == "__main__":
    unittest.main()
