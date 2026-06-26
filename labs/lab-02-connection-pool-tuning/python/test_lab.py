#!/usr/bin/env python3
"""
Lab 02 checks.

By default this tests your work in stub.py:
    python -m unittest test_lab.py

To check against the reference solution instead:
    LAB_MODULE=solution python -m unittest test_lab.py
"""

import importlib
import os
import unittest

lab = importlib.import_module(os.environ.get("LAB_MODULE", "stub"))


class TestConnectionPool(unittest.TestCase):
    def setUp(self):
        lab.SimulatedConnection._id_counter = 0

    def test_acquire_release_roundtrip_reuses_connection(self):
        pool = lab.ConnectionPool(min_size=1, max_size=2, timeout=5.0)
        c1 = pool.acquire()
        pool.release(c1)
        c2 = pool.acquire()
        self.assertEqual(c1.id, c2.id, "released connection should be reused")
        pool.release(c2)

    def test_never_exceeds_max_size_under_load(self):
        pool = lab.ConnectionPool(min_size=2, max_size=10, timeout=30.0)
        result = lab.run_concurrent_workload(pool, 100)
        self.assertNotIn("error", result)
        self.assertEqual(result["successful"], 100)
        self.assertLessEqual(
            result["connections_created"], 10,
            "pool must never create more than max_size connections",
        )

    def test_timeout_when_pool_exhausted(self):
        pool = lab.ConnectionPool(min_size=1, max_size=1, timeout=0.2)
        held = pool.acquire()
        with self.assertRaises(TimeoutError):
            pool.acquire()
        pool.release(held)

    def test_larger_pool_beats_size_one(self):
        big = lab.ConnectionPool(min_size=5, max_size=10, timeout=60.0)
        big_res = lab.run_concurrent_workload(big, 60)
        lab.SimulatedConnection._id_counter = 0
        small = lab.ConnectionPool(min_size=1, max_size=1, timeout=60.0)
        small_res = lab.run_concurrent_workload(small, 60)
        self.assertGreater(
            big_res["throughput_rps"], small_res["throughput_rps"],
            "pool size=10 should out-throughput pool size=1",
        )


if __name__ == "__main__":
    unittest.main()
