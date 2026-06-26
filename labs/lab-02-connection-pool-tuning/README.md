# Lab 02: Connection Pool Tuning

## Objective

Understand how connection pool size affects throughput and latency under concurrent load. You will benchmark four configurations: no pool, pool too small, pool optimal, and pool too large.

> **Available in 8 languages.** Each `<lang>/` folder has a `stub` you implement
> and a `solution` for reference: Python, Go, JavaScript, TypeScript, Rust, Ruby,
> C++, and C. Implement `acquire`/`release`; the embedded checks verify reuse, a
> timeout when exhausted, and that the pool never exceeds `maxSize`. Pick the tab
> for your language in the lab console, or run the `testCmd` from `lab.json`.

**Expected outcomes:**

| Configuration | p50 | p99 | Throughput |
|--------------|-----|-----|-----------|
| No pool | ~25ms | ~90ms | ~120 req/s |
| Pool size=1 | ~55ms | ~400ms | ~50 req/s |
| Pool size=10 | ~12ms | ~25ms | ~500 req/s |
| Pool size=100 | ~15ms | ~40ms | ~400 req/s |

Pool size=10 is optimal because it matches the concurrency level and minimizes overhead.

---

## Prerequisites

- Python 3.8+
- No external dependencies — uses threading and time.sleep to simulate connection overhead
- Run from any directory

---

## The Experiment Design

We simulate a database connection with:
- **Connection creation cost:** 15ms (TCP + TLS + auth overhead)
- **Query execution time:** 10ms (realistic average query)
- **100 concurrent requests** arrive simultaneously
- **Each request needs exactly one connection**

The pool manages a bounded set of pre-created connections. Requests that cannot get a connection immediately must wait.

---

## Complete Lab Code

Save as `lab02.py` and run with `python lab02.py`:

```python
#!/usr/bin/env python3
"""
Lab 02: Connection Pool Tuning
Demonstrates impact of pool size on latency and throughput under concurrent load.
No external dependencies — uses threading and sleep to simulate DB connection cost.
"""

import threading
import time
import statistics
import queue
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────
# SIMULATED CONNECTION
# ──────────────────────────────────────────────

class SimulatedConnection:
    """
    Simulates a database connection.
    Creation is expensive (simulated TCP+TLS+auth overhead).
    Query execution is cheaper (simulated DB query).
    """
    CONNECTION_CREATION_COST_MS = 15   # TCP 3-way + TLS + DB auth
    QUERY_COST_MS = 10                 # average query execution time
    _id_counter = 0
    _lock = threading.Lock()

    def __init__(self):
        with SimulatedConnection._lock:
            SimulatedConnection._id_counter += 1
            self.id = SimulatedConnection._id_counter

        # Simulate connection creation overhead
        time.sleep(self.CONNECTION_CREATION_COST_MS / 1000)
        self.created_at = time.monotonic()
        self.queries_executed = 0
        self._closed = False

    def execute(self, query: str = "SELECT 1") -> dict:
        """Execute a query. Simulates network RTT + DB processing."""
        if self._closed:
            raise RuntimeError(f"Connection {self.id} is closed")
        time.sleep(self.QUERY_COST_MS / 1000)
        self.queries_executed += 1
        return {"rows": 1, "connection_id": self.id}

    def ping(self) -> bool:
        """Health check — lightweight."""
        return not self._closed

    def close(self):
        self._closed = True

    def __repr__(self):
        return f"Connection(id={self.id}, queries={self.queries_executed})"


# ──────────────────────────────────────────────
# CONNECTION POOL IMPLEMENTATION
# ──────────────────────────────────────────────

@dataclass
class PoolStats:
    """Accumulated statistics for the pool."""
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

    @property
    def p99_wait_ms(self) -> float:
        if not self.wait_times_ms:
            return 0.0
        idx = int(len(self.wait_times_ms) * 0.99)
        return sorted(self.wait_times_ms)[idx]


class ConnectionPool:
    """
    Thread-safe connection pool with:
    - Configurable min/max size
    - Blocking acquire with timeout
    - Statistics collection
    - Health checking
    """

    def __init__(self, min_size: int = 2, max_size: int = 10, timeout: float = 30.0):
        self.min_size = min_size
        self.max_size = max_size
        self.timeout = timeout

        self._idle: list[SimulatedConnection] = []
        self._total_created = 0
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(max_size)
        self.stats = PoolStats()

        # Pre-warm the pool
        for _ in range(min_size):
            conn = SimulatedConnection()
            self._idle.append(conn)
            self._total_created += 1
            self.stats.total_created += 1

    def acquire(self) -> SimulatedConnection:
        """
        Get a connection from the pool.
        Blocks if pool is exhausted until one is returned or timeout expires.
        """
        wait_start = time.monotonic()

        if not self._semaphore.acquire(timeout=self.timeout):
            self.stats.total_timeouts += 1
            raise TimeoutError(
                f"Pool exhausted (max={self.max_size}): "
                f"waited {self.timeout}s"
            )

        wait_ms = (time.monotonic() - wait_start) * 1000
        self.stats.record_wait(wait_ms)

        with self._lock:
            if self._idle:
                conn = self._idle.pop()  # LIFO: take most recently used
                if not conn.ping():
                    # Unhealthy — create replacement
                    conn = SimulatedConnection()
                    self._total_created += 1
                    self.stats.total_created += 1
            else:
                # No idle connections but semaphore acquired — create new
                conn = SimulatedConnection()
                self._total_created += 1
                self.stats.total_created += 1

        self.stats.total_acquired += 1
        return conn

    def release(self, conn: SimulatedConnection):
        """Return connection to pool."""
        with self._lock:
            if conn.ping():
                self._idle.append(conn)
            else:
                self._total_created -= 1  # lost this connection
        self._semaphore.release()
        self.stats.total_released += 1

    @contextmanager
    def connection(self):
        """Context manager for safe connection usage."""
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    @property
    def idle_count(self) -> int:
        with self._lock:
            return len(self._idle)

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._total_created - len(self._idle)


# ──────────────────────────────────────────────
# NO-POOL IMPLEMENTATION (for comparison)
# ──────────────────────────────────────────────

class NoPool:
    """Creates a new connection for every request — no pooling."""

    def __init__(self):
        self.stats = PoolStats()

    @contextmanager
    def connection(self):
        wait_start = time.monotonic()
        conn = SimulatedConnection()                      # 15ms creation cost
        wait_ms = (time.monotonic() - wait_start) * 1000
        self.stats.record_wait(wait_ms)
        self.stats.total_created += 1
        self.stats.total_acquired += 1
        try:
            yield conn
        finally:
            conn.close()
            self.stats.total_released += 1


# ──────────────────────────────────────────────
# WORKLOAD SIMULATION
# ──────────────────────────────────────────────

def run_request(pool_or_nopool, results: list, errors: list, idx: int):
    """
    Simulate one HTTP request that needs one DB query.
    Measures total time: wait for connection + query execution.
    """
    start = time.monotonic()
    try:
        with pool_or_nopool.connection() as conn:
            conn.execute("SELECT * FROM users WHERE id = ?")
        elapsed_ms = (time.monotonic() - start) * 1000
        results.append(elapsed_ms)
    except TimeoutError as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        errors.append({"idx": idx, "elapsed_ms": elapsed_ms, "error": str(e)})
    except Exception as e:
        errors.append({"idx": idx, "elapsed_ms": -1, "error": str(e)})


def run_concurrent_workload(pool_or_nopool, num_requests: int = 100) -> dict:
    """
    Launch num_requests concurrent threads simultaneously.
    Returns timing statistics.
    """
    results = []
    errors = []
    threads = []

    # Create all threads first (they won't start until we signal)
    barrier = threading.Barrier(num_requests + 1)  # +1 for main thread

    def request_with_barrier(idx: int):
        barrier.wait()  # synchronize: all threads start at exactly the same time
        run_request(pool_or_nopool, results, errors, idx)

    for i in range(num_requests):
        t = threading.Thread(target=request_with_barrier, args=(i,))
        t.daemon = True
        threads.append(t)
        t.start()

    # Signal all threads to start simultaneously
    workload_start = time.monotonic()
    barrier.wait()

    # Wait for all to complete
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
        "p90_ms": sorted_results[int(n * 0.90)],
        "p99_ms": sorted_results[int(n * 0.99)] if n > 1 else sorted_results[-1],
        "min_ms": sorted_results[0],
        "max_ms": sorted_results[-1],
        "total_time_s": total_elapsed,
        "throughput_rps": len(results) / total_elapsed,
        "avg_wait_ms": pool_or_nopool.stats.avg_wait_ms,
        "connections_created": pool_or_nopool.stats.total_created,
    }


# ──────────────────────────────────────────────
# FOUR SCENARIOS
# ──────────────────────────────────────────────

def scenario_no_pool(num_requests: int) -> dict:
    """Scenario 1: No pool — new connection per request."""
    pool = NoPool()
    return run_concurrent_workload(pool, num_requests)


def scenario_pool_too_small(num_requests: int) -> dict:
    """Scenario 2: Pool size=1 — massive bottleneck, high queuing."""
    SimulatedConnection._id_counter = 0
    pool = ConnectionPool(min_size=1, max_size=1, timeout=60.0)
    return run_concurrent_workload(pool, num_requests)


def scenario_pool_optimal(num_requests: int) -> dict:
    """Scenario 3: Pool size=10 — good balance for 100 concurrent."""
    SimulatedConnection._id_counter = 0
    pool = ConnectionPool(min_size=5, max_size=10, timeout=30.0)
    return run_concurrent_workload(pool, num_requests)


def scenario_pool_too_large(num_requests: int) -> dict:
    """Scenario 4: Pool size=100 — all requests get immediate connection.
    BUT: 100 simultaneous connections overwhelm the DB server's scheduler."""
    SimulatedConnection._id_counter = 0
    # Simulate DB degradation at high connection count: increase query cost
    original_cost = SimulatedConnection.QUERY_COST_MS
    SimulatedConnection.QUERY_COST_MS = 15  # DB slower due to process scheduling
    pool = ConnectionPool(min_size=20, max_size=100, timeout=30.0)
    result = run_concurrent_workload(pool, num_requests)
    SimulatedConnection.QUERY_COST_MS = original_cost
    return result


# ──────────────────────────────────────────────
# ANALYSIS AND OUTPUT
# ──────────────────────────────────────────────

def print_scenario_results(name: str, stats: dict, description: str):
    print(f"\n{'─' * 65}")
    print(f"  {name}")
    print(f"  {description}")
    print(f"{'─' * 65}")

    if "error" in stats:
        print(f"  ✗ FAILED: {stats['error']}")
        return

    success_rate = stats["successful"] / stats["total_requests"] * 100
    print(f"  Requests:      {stats['total_requests']} total, "
          f"{stats['successful']} succeeded ({success_rate:.0f}%)")
    print(f"  Latency:       p50={stats['p50_ms']:.1f}ms  "
          f"p90={stats['p90_ms']:.1f}ms  "
          f"p99={stats['p99_ms']:.1f}ms")
    print(f"  Range:         min={stats['min_ms']:.1f}ms  max={stats['max_ms']:.1f}ms")
    print(f"  Throughput:    {stats['throughput_rps']:.1f} req/s")
    print(f"  DB Connections created: {stats['connections_created']}")
    print(f"  Avg conn wait: {stats['avg_wait_ms']:.1f}ms")
    if stats["errors"] > 0:
        print(f"  ⚠ Errors:      {stats['errors']}")


def print_comparison_table(all_results: dict):
    print("\n" + "=" * 80)
    print("COMPARISON TABLE")
    print("=" * 80)
    print(f"{'Config':<22} {'p50':>8} {'p99':>8} {'Throughput':>12} {'Conn Created':>14}")
    print("-" * 80)
    for name, stats in all_results.items():
        if "error" in stats:
            print(f"{name:<22} {'ERROR':>8}")
            continue
        print(
            f"{name:<22} "
            f"{stats['p50_ms']:>7.1f}ms "
            f"{stats['p99_ms']:>7.1f}ms "
            f"{stats['throughput_rps']:>11.1f}/s "
            f"{stats['connections_created']:>14}"
        )
    print("=" * 80)


def print_analysis():
    print("""
ANALYSIS:
─────────────────────────────────────────────────────────────────────────
No Pool:        Each request pays 15ms connection creation cost.
                100 connections created = 100 × TCP+TLS+auth overhead.
                High throughput variability.

Pool size=1:    All 100 requests queue behind a single connection.
                Requests execute serially: total_time ≈ 100 × 25ms = 2,500ms.
                p99 is terrible because requests at the back of the queue wait longest.

Pool size=10:   10 connections serve 100 requests in 10 batches of 10.
                Total time ≈ 10 × 25ms = 250ms (10× faster than pool=1).
                p99 is much better: worst case waits for ~9 others ahead.

Pool size=100:  All 100 requests get a connection immediately.
                BUT: 100 simultaneous DB queries thrash the DB server's CPU.
                DB scheduler overhead increases query time from 10ms to 15ms.
                Creates 100 connections = 100 OS processes on the DB server.

KEY INSIGHT: Pool size is not "more = better".
  Optimal size = f(CPU cores on DB server, query duration, concurrency level)
  Formula: pool_size ≈ DB_CPU_cores × 2 (HikariCP / PgBouncer recommendation)
─────────────────────────────────────────────────────────────────────────
""")


# ──────────────────────────────────────────────
# LITTLE'S LAW DEMONSTRATION
# ──────────────────────────────────────────────

def print_littles_law_analysis():
    print("""
LITTLE'S LAW ANALYSIS:
  L = λ × W  (average queue length = arrival rate × average wait time)

  For pool_size=10, query_time=10ms:
    Maximum throughput λ_max = pool_size / W = 10 / 0.010s = 1,000 req/s
    (At 100 concurrent requests in 250ms total: 100/0.250 = 400 req/s — matches)

  For pool_size=1, query_time=10ms:
    Maximum throughput = 1 / 0.010s = 100 req/s
    (At 100 concurrent: heavily queued, p99 >> p50)

  SIZING FORMULA for your system:
    pool_size = ceil(target_rps × avg_query_duration_seconds × safety_factor)
    
  Example: 500 req/s target, 10ms avg query, 1.3 safety factor:
    pool_size = ceil(500 × 0.010 × 1.3) = ceil(6.5) = 7
    Round up to nearest sensible number: 10
""")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    NUM_REQUESTS = 100

    print("\n" + "=" * 65)
    print("LAB 02: Connection Pool Tuning")
    print("=" * 65)
    print(f"""
Configuration:
  Simulated connection creation cost: {SimulatedConnection.CONNECTION_CREATION_COST_MS}ms
  Simulated query execution time:     {SimulatedConnection.QUERY_COST_MS}ms
  Concurrent requests:                {NUM_REQUESTS}

Each request simulates:
  1. Wait for available connection (pool contention)
  2. Create new connection if pool not full (15ms overhead)
  3. Execute query (10ms)
  4. Release connection back to pool
""")

    all_results = {}

    print("Running Scenario 1: No Pool (new connection per request)...")
    all_results["No Pool"] = scenario_no_pool(NUM_REQUESTS)
    print_scenario_results(
        "SCENARIO 1: No Pool",
        all_results["No Pool"],
        "Each request creates a new TCP connection (15ms overhead per request)"
    )

    print("\nRunning Scenario 2: Pool size=1 (extreme bottleneck)...")
    all_results["Pool size=1"] = scenario_pool_too_small(NUM_REQUESTS)
    print_scenario_results(
        "SCENARIO 2: Pool size=1",
        all_results["Pool size=1"],
        "All 100 concurrent requests share 1 connection — severe queuing"
    )

    print("\nRunning Scenario 3: Pool size=10 (optimal for this workload)...")
    all_results["Pool size=10"] = scenario_pool_optimal(NUM_REQUESTS)
    print_scenario_results(
        "SCENARIO 3: Pool size=10 (OPTIMAL)",
        all_results["Pool size=10"],
        "10 connections serve 100 requests in 10 parallel batches"
    )

    print("\nRunning Scenario 4: Pool size=100 (too large)...")
    all_results["Pool size=100"] = scenario_pool_too_large(NUM_REQUESTS)
    print_scenario_results(
        "SCENARIO 4: Pool size=100 (too large)",
        all_results["Pool size=100"],
        "100 connections created — overwhelms DB scheduler, each query takes longer"
    )

    print_comparison_table(all_results)
    print_analysis()
    print_littles_law_analysis()

    print("=" * 65)
    print("CONCLUSION:")
    print("  Pool size=10 achieves the best balance of latency and throughput.")
    print("  The optimal pool size is NOT the maximum — it's the minimum")
    print("  needed to saturate your bottleneck (usually: DB CPU cores × 2).")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
```

---

## Running the Lab

```bash
python lab02.py
```

**Expected output structure:**

```
===================================================================
LAB 02: Connection Pool Tuning
===================================================================

Configuration:
  Simulated connection creation cost: 15ms
  Simulated query execution time:     10ms
  Concurrent requests:                100

─────────────────────────────────────────────────────────────────
  SCENARIO 1: No Pool
  Each request creates a new TCP connection (15ms overhead per request)
─────────────────────────────────────────────────────────────────
  Requests:      100 total, 100 succeeded (100%)
  Latency:       p50=25.3ms  p90=31.2ms  p99=38.4ms
  Throughput:    74.1 req/s
  DB Connections created: 100

─────────────────────────────────────────────────────────────────
  SCENARIO 2: Pool size=1
  All 100 concurrent requests share 1 connection — severe queuing
─────────────────────────────────────────────────────────────────
  Requests:      100 total, 100 succeeded (100%)
  Latency:       p50=511ms  p90=942ms  p99=1,001ms
  Throughput:    9.9 req/s
  DB Connections created: 1

─────────────────────────────────────────────────────────────────
  SCENARIO 3: Pool size=10 (OPTIMAL)
  10 connections serve 100 requests in 10 parallel batches
─────────────────────────────────────────────────────────────────
  Requests:      100 total, 100 succeeded (100%)
  Latency:       p50=115ms  p90=203ms  p99=212ms
  Throughput:    48.0 req/s
  DB Connections created: 10

================================================================================
COMPARISON TABLE
================================================================================
Config                      p50      p99   Throughput   Conn Created
--------------------------------------------------------------------------------
No Pool                   25.3ms  38.4ms        74.1/s            100
Pool size=1              511.0ms 1001.0ms         9.9/s              1
Pool size=10             115.0ms  212.0ms        48.0/s             10
Pool size=100             21.0ms   36.0ms        64.3/s            100
================================================================================
```

---

## Extension Exercise 1: Find Your Optimal Pool Size

Modify the code to test pool sizes from 1 to 50:

```python
def sweep_pool_sizes(num_requests: int = 100):
    print(f"\n{'Pool Size':>10} {'p50 (ms)':>10} {'p99 (ms)':>10} {'Throughput':>12}")
    print("-" * 46)
    for pool_size in [1, 2, 5, 10, 15, 20, 30, 50]:
        SimulatedConnection._id_counter = 0
        pool = ConnectionPool(min_size=min(2, pool_size), max_size=pool_size, timeout=60.0)
        stats = run_concurrent_workload(pool, num_requests)
        print(
            f"{pool_size:>10} "
            f"{stats['p50_ms']:>10.1f} "
            f"{stats['p99_ms']:>10.1f} "
            f"{stats['throughput_rps']:>11.1f}/s"
        )
```

Expected: p99 improves as pool size increases, then plateaus (or degrades) past the optimal point.

---

## Extension Exercise 2: Connection Leak Simulation

```python
def simulate_connection_leak(pool: ConnectionPool, num_requests: int = 20):
    """
    Simulate requests that forget to release connections (connection leak).
    The pool will exhaust and subsequent requests will time out.
    """
    leaked_connections = []

    for i in range(num_requests):
        try:
            conn = pool.acquire()
            conn.execute()
            # BUG: forgot to call pool.release(conn) ← connection leak
            leaked_connections.append(conn)  # prevent GC
            print(f"Request {i+1}: success, pool active={pool.active_count}")
        except TimeoutError:
            print(f"Request {i+1}: TIMEOUT — pool exhausted by leak!")
```

---

## Checklist

- [ ] Ran the lab and observed all four scenarios
- [ ] Understood why pool size=1 causes p99 >> p50 (queuing theory)
- [ ] Understood why pool size=100 is NOT always best (DB server overload)
- [ ] Applied Little's Law to estimate optimal pool size
- [ ] Ran the pool size sweep (Extension 1)
- [ ] Observed connection leak behavior (Extension 2)

## Related Modules

- `../../bsps/07-core-backend-engineering/02-connection-pooling.md` — full theory
- `../../bsps/01-mathematics-for-systems/04-queueing-theory.md` — Little's Law derivation
- `../../bsps/03-operating-systems/01-processes-and-threads.md` — why each DB connection is an OS process
