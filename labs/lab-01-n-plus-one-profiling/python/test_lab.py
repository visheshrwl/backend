#!/usr/bin/env python3
"""
Lab 01 checks — run against real Postgres through labkit.

By default tests your work in stub.py:
    python -m unittest test_lab.py

Check the reference instead:
    LAB_MODULE=solution python -m unittest test_lab.py

Requires the lab's Postgres service. In Codespaces/Gitpod it is already up;
locally run `docker compose -f .devcontainer/docker-compose.yml up -d postgres`.
"""

import asyncio
import importlib
import os
import unittest

from labkit import db

lab = importlib.import_module(os.environ.get("LAB_MODULE", "stub"))


def _by_id(rows):
    return sorted(
        ({"id": u["id"], "post_count": len(u["posts"])} for u in rows),
        key=lambda x: x["id"],
    )


class TestNPlusOne(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lab.setup_dataset()  # create + seed once; tests are read-only

    def setUp(self):
        db.reset_counters()

    def test_join_uses_one_query(self):
        result = lab.fetch_users_with_posts_join()
        self.assertEqual(db.query_count, 1, "JOIN must run exactly 1 query")
        self.assertEqual(len(result), 100)

    def test_in_batch_uses_two_queries(self):
        result = lab.fetch_users_with_posts_in_batch()
        self.assertEqual(db.query_count, 2, "IN batch must run exactly 2 queries")
        self.assertEqual(len(result), 100)

    def test_all_approaches_return_identical_data(self):
        baseline = lab.fetch_users_with_posts_n_plus_one()
        join = lab.fetch_users_with_posts_join()
        batch = lab.fetch_users_with_posts_in_batch()
        self.assertEqual(_by_id(baseline), _by_id(join), "JOIN data must match N+1")
        self.assertEqual(_by_id(baseline), _by_id(batch), "IN batch data must match N+1")
        self.assertEqual(sum(u["post_count"] for u in _by_id(baseline)), 500)

    def test_dataloader_batches_to_two_queries(self):
        result = asyncio.run(lab.fetch_users_with_posts_dataloader())
        self.assertEqual(len(result), 100)
        self.assertEqual(
            db.query_count, 2,
            "DataLoader must run 1 users query + 1 batched posts query, regardless of N",
        )
        self.assertEqual(sum(len(u["posts"]) for u in result), 500)


if __name__ == "__main__":
    unittest.main()
