#!/usr/bin/env python3
"""
Lab 02: Connection Pool Tuning — reference solution.

Shows how pool size affects latency and throughput under concurrent load.
No external dependencies — uses threading + sleep to simulate DB connection cost.

Run:   python solution.py
Test:  python -m unittest test_lab.py
"""

import threading
import time
import statistics
from contextlib import contextmanager
from dataclasses import dataclass, field


# ──────────────────────────────────────────────
# SIMULATED CONNECTION
# ──────────────────────────────────────────────

class SimulatedConnection:
    """Connection whose creation is expensive and queries are cheaper."""

    CONNECTION_CREATION_COST_MS = 15
    QUERY_COST_MS = 10
    _id_counter = 0
    _lock = threading.Lock()

    def __init__(self):
        with SimulatedConnection._lock:
            SimulatedConnection._id_counter += 1
            self.id = SimulatedConnection._id_counter
        time.sleep(self.CONNECTION_CREATION_COST_MS / 1000)
        self.created_at = time.monotonic()
        self.queries_executed = 0
        self._closed = False

    def execute(self, query: str = "SELECT 1") -> dict:
        if self._closed:
            raise RuntimeError(f"Connection {self.id} is closed")
        time.sleep(self.QUERY_COST_MS / 1000)
        self.queries_executed += 1
        return {"rows": 1, "connection_id": self.id}

    def ping(self) -> bool:
        return not self._closed

    def close(self):
        self._closed = True


# ──────────────────────────────────────────────
# POOL STATISTICS
# ──────────────────────────────────────────────

@dataclass
class PoolStats:
    total_created: int = 0
    total_acquired: int = 0
    total_released: int = 0
    total_timeouts: int = 0
    total_wait_time_ms: float = 0.0
    wait_times_ms: list = field(default_factory=list)

    def record_wait(self, wait_ms: float):
        self.wait_times_ms.append(wait_ms)
        self.total_wait_time_ms += wait_ms

    @property
    def avg_wait_ms(self) -> float:
        return self.total_wait_time_ms / max(1, len(self.wait_times_ms))


# ──────────────────────────────────────────────
# CONNECTION POOL
# ──────────────────────────────────────────────

class ConnectionPool:
    """Thread-safe pool with blocking acquire + timeout and stats."""

    def __init__(self, min_size: int = 2, max_size: int = 10, timeout: float = 30.0):
        self.min_size = min_size
        self.max_size = max_size
        self.timeout = timeout

        self._idle: list[SimulatedConnection] = []
        self._total_created = 0
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(max_size)
        self.stats = PoolStats()

        for _ in range(min_size):
            conn = SimulatedConnection()
            self._idle.append(conn)
            self._total_created += 1
            self.stats.total_created += 1

    def acquire(self) -> SimulatedConnection:
        wait_start = time.monotonic()
        if not self._semaphore.acquire(timeout=self.timeout):
            self.stats.total_timeouts += 1
            raise TimeoutError(
                f"Pool exhausted (max={self.max_size}): waited {self.timeout}s"
            )

        wait_ms = (time.monotonic() - wait_start) * 1000
        self.stats.record_wait(wait_ms)

        with self._lock:
            if self._idle:
                conn = self._idle.pop()
                if not conn.ping():
                    conn = SimulatedConnection()
                    self._total_created += 1
                    self.stats.total_created += 1
            else:
                conn = SimulatedConnection()
                self._total_created += 1
                self.stats.total_created += 1

        self.stats.total_acquired += 1
        return conn

    def release(self, conn: SimulatedConnection):
        with self._lock:
            if conn.ping():
                self._idle.append(conn)
            else:
                self._total_created -= 1
        self._semaphore.release()
        self.stats.total_released += 1

    @contextmanager
    def connection(self):
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._total_created - len(self._idle)


class NoPool:
    """Creates a new connection for every request — no pooling. (baseline)"""

    def __init__(self):
        self.stats = PoolStats()

    @contextmanager
    def connection(self):
        wait_start = time.monotonic()
        conn = SimulatedConnection()
        self.stats.record_wait((time.monotonic() - wait_start) * 1000)
        self.stats.total_created += 1
        self.stats.total_acquired += 1
        try:
            yield conn
        finally:
            conn.close()
            self.stats.total_released += 1


# ──────────────────────────────────────────────
# WORKLOAD
# ──────────────────────────────────────────────

def run_request(pool, results, errors, idx):
    start = time.monotonic()
    try:
        with pool.connection() as conn:
            conn.execute("SELECT * FROM users WHERE id = ?")
        results.append((time.monotonic() - start) * 1000)
    except TimeoutError as e:
        errors.append({"idx": idx, "error": str(e)})
    except Exception as e:  # noqa: BLE001
        errors.append({"idx": idx, "error": str(e)})


def run_concurrent_workload(pool, num_requests: int = 100) -> dict:
    results, errors, threads = [], [], []
    barrier = threading.Barrier(num_requests + 1)

    def request_with_barrier(idx):
        barrier.wait()
        run_request(pool, results, errors, idx)

    for i in range(num_requests):
        t = threading.Thread(target=request_with_barrier, args=(i,), daemon=True)
        threads.append(t)
        t.start()

    workload_start = time.monotonic()
    barrier.wait()
    for t in threads:
        t.join(timeout=60)
    total_elapsed = time.monotonic() - workload_start

    if not results:
        return {"error": "All requests failed", "errors": errors}

    sorted_results = sorted(results)
    n = len(sorted_results)
    return {
        "total_requests": num_requests,
        "successful": len(results),
        "errors": len(errors),
        "p50_ms": statistics.median(sorted_results),
        "p99_ms": sorted_results[int(n * 0.99)] if n > 1 else sorted_results[-1],
        "throughput_rps": len(results) / total_elapsed,
        "connections_created": pool.stats.total_created,
    }


def scenario(min_size, max_size, num_requests=100, query_cost_ms=10):
    SimulatedConnection._id_counter = 0
    original = SimulatedConnection.QUERY_COST_MS
    SimulatedConnection.QUERY_COST_MS = query_cost_ms
    try:
        pool = ConnectionPool(min_size=min_size, max_size=max_size, timeout=60.0)
        return run_concurrent_workload(pool, num_requests)
    finally:
        SimulatedConnection.QUERY_COST_MS = original


def main():
    NUM = 100
    print("\n" + "=" * 65)
    print("LAB 02: Connection Pool Tuning")
    print("=" * 65 + "\n")

    runs = {
        "No Pool": run_concurrent_workload(NoPool(), NUM),
        "Pool size=1": scenario(1, 1, NUM),
        "Pool size=10": scenario(5, 10, NUM),
        "Pool size=100": scenario(20, 100, NUM, query_cost_ms=15),
    }

    print(f"{'Config':<16} {'p50':>9} {'p99':>9} {'Throughput':>12} {'Conns':>7}")
    print("-" * 56)
    for name, s in runs.items():
        if "error" in s:
            print(f"{name:<16} ERROR")
            continue
        print(f"{name:<16} {s['p50_ms']:>7.1f}ms {s['p99_ms']:>7.1f}ms "
              f"{s['throughput_rps']:>10.1f}/s {s['connections_created']:>7}")

    print("\nPool size=10 wins: enough connections to saturate the workload,")
    print("not so many that connection creation and DB scheduling dominate.")


if __name__ == "__main__":
    main()
