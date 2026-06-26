#!/usr/bin/env python3
"""
Lab 02: Connection Pool Tuning — YOUR TURN.

Everything except the pool's acquire/release logic is given. Implement:

  ConnectionPool.acquire  -> block on the semaphore (with timeout), then hand
                             out an idle connection or create a new one.
  ConnectionPool.release  -> return a healthy connection to the pool and free
                             one semaphore permit.

Goal: a pool of size 10 should serve 100 concurrent requests far faster than
a pool of size 1, while never creating more than max_size connections.

When done:
  python -m unittest test_lab.py     # should pass
  python solution.py                  # compare against the reference
"""

import threading
import time
import statistics
from contextlib import contextmanager
from dataclasses import dataclass, field


class SimulatedConnection:
    """Expensive to create (15ms), cheap to query (10ms). (given)"""

    CONNECTION_CREATION_COST_MS = 15
    QUERY_COST_MS = 10
    _id_counter = 0
    _lock = threading.Lock()

    def __init__(self):
        with SimulatedConnection._lock:
            SimulatedConnection._id_counter += 1
            self.id = SimulatedConnection._id_counter
        time.sleep(self.CONNECTION_CREATION_COST_MS / 1000)
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


class ConnectionPool:
    """Thread-safe pool. You implement acquire() and release()."""

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
        """
        TODO:
          1. Try to acquire the semaphore with self.timeout. If it fails,
             record a timeout in stats and raise TimeoutError.
          2. Record the wait time in self.stats.
          3. Under self._lock: return an idle connection if one exists
             (replace it if it fails ping()), otherwise create a new
             SimulatedConnection and bump the created counters.
          4. Increment stats.total_acquired and return the connection.
        """
        raise NotImplementedError("Implement ConnectionPool.acquire")

    def release(self, conn: SimulatedConnection):
        """
        TODO:
          1. Under self._lock: if conn.ping(), push it back onto self._idle;
             otherwise decrement self._total_created (the connection is lost).
          2. Release one semaphore permit.
          3. Increment stats.total_released.
        """
        raise NotImplementedError("Implement ConnectionPool.release")

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
    """New connection per request — the baseline. (given)"""

    def __init__(self):
        self.stats = PoolStats()

    @contextmanager
    def connection(self):
        conn = SimulatedConnection()
        self.stats.total_created += 1
        self.stats.total_acquired += 1
        try:
            yield conn
        finally:
            conn.close()
            self.stats.total_released += 1


def run_request(pool, results, errors, idx):
    start = time.monotonic()
    try:
        with pool.connection() as conn:
            conn.execute("SELECT * FROM users WHERE id = ?")
        results.append((time.monotonic() - start) * 1000)
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
        "successful": len(results),
        "p50_ms": statistics.median(sorted_results),
        "p99_ms": sorted_results[int(n * 0.99)] if n > 1 else sorted_results[-1],
        "throughput_rps": len(results) / total_elapsed,
        "connections_created": pool.stats.total_created,
    }


def main():
    SimulatedConnection._id_counter = 0
    try:
        pool = ConnectionPool(min_size=5, max_size=10, timeout=60.0)
        print(run_concurrent_workload(pool, 100))
    except NotImplementedError as e:
        print(f"Not implemented yet: {e}")


if __name__ == "__main__":
    main()
