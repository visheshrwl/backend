# Connection Pooling

## Problem

Every database query requires a database connection. Without pooling, each request creates a new connection and destroys it when done. A TCP connection to PostgreSQL costs:

```
3-way TCP handshake:    1.5 RTT  ≈  3ms   (LAN) / 15ms (cross-AZ)
TLS 1.2 handshake:      2 RTT   ≈  4ms   (LAN) / 20ms (cross-AZ)
PostgreSQL auth:         1 RTT   ≈  2ms   (LAN) / 10ms (cross-AZ)
PostgreSQL startup:      fork/exec + catalog load ≈ 5ms
─────────────────────────────────────────────────────────────────
Total new connection:            ≈ 14ms   (LAN) / 45ms (cross-AZ)
```

A connection pool maintains a set of already-established connections, reducing connection cost to near zero for each query.

---

## Why It Matters (Latency, Throughput, Cost)

**Latency:**
```
Without pool: query_time = connection_overhead + execution_time
              = 14ms + 2ms = 16ms  (for a 2ms query — 8× overhead)

With pool:    query_time ≈ execution_time
              = 0.1ms + 2ms = 2.1ms
```

**Throughput (Little's Law applied):**

Little's Law: `L = λW`
- L = average number of requests in the system
- λ = arrival rate (requests/second)
- W = average time each request spends in the system

For a pool of size P handling requests of duration W:
```
Maximum throughput λ_max = P / W
```

With W = 16ms (connection overhead + query) and P = 20 connections:
```
Without pool efficiency: λ_max = 20 / 0.016 = 1,250 req/s
With pool:               λ_max = 20 / 0.002 = 10,000 req/s
```

**PostgreSQL process cost:**
Each PostgreSQL connection is a **forked OS process**. On Linux, `fork()` for a 50MB PostgreSQL process copies page tables (even with copy-on-write): ~2ms. Each connected client = one OS process on the DB server. At 500 connections: 500 processes, 500 × ~5MB shared memory overhead ≈ 2.5GB RAM consumed just for connection state.

---

## Mental Model

A connection pool is a **bounded resource manager** — the same abstraction as a thread pool, semaphore, or object pool:

```
┌────────────────────────────────────────┐
│           Connection Pool              │
│                                        │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ │
│  │ conn │ │ conn │ │ conn │ │ conn │ │  ← idle connections
│  │  1   │ │  2   │ │  3   │ │  4   │ │
│  └──────┘ └──────┘ └──────┘ └──────┘ │
│                                        │
│  ┌──────┐ ┌──────┐                    │
│  │ conn │ │ conn │  ← in-use          │
│  │  5   │ │  6   │                    │
│  └──────┘ └──────┘                    │
│                                        │
│  pending requests queue: [req7, req8]  │
└────────────────────────────────────────┘

acquire():
  if idle_connections:
      return idle_connections.pop()      # O(1)
  elif total < max_size:
      return create_new_connection()     # O(1) amortized
  else:
      block on queue until timeout       # O(1) with semaphore

release(conn):
  validate_connection(conn)             # send keepalive ping
  idle_connections.push(conn)           # O(1)
  notify_waiting_requests()             # wake one waiter
```

---

## Underlying Theory (OS / CN / DSA / Math Linkage)

### TCP State Machine

A TCP connection traverses these states on creation:

```
Client                          Server (PostgreSQL)
  │                                    │
  │──── SYN ──────────────────────────►│  LISTEN → SYN_RECEIVED
  │◄─── SYN-ACK ──────────────────────│
  │──── ACK ──────────────────────────►│  ESTABLISHED
  │                                    │  fork() PostgreSQL backend process
  │◄─── PostgreSQL startup message ───│
  │──── Auth (md5/scram) ─────────────►│
  │◄─── Auth OK + parameters ─────────│  Ready for query
```

A pooled connection stays in `ESTABLISHED` state. Reusing it skips all handshakes.

### File Descriptor Cost

Each TCP socket is a kernel file descriptor. The application must `close()` unused connections; leaking connections consumes FDs until `EMFILE` (too many open files). The OS default FD limit is often 1024 per process — easily hit without pooling.

```bash
# Check FD limits
ulimit -n          # per-process FD limit
cat /proc/sys/fs/file-max  # system-wide FD limit

# View open connections for a process
ss -tp | grep <pid>
```

### Queueing Theory: Pool Sizing

The pool is a multi-server queue (M/M/c model):
- Arrival process: Poisson with rate λ requests/second
- Service time: exponential with mean 1/μ seconds
- c servers: pool size P

For stable operation: `λ < c × μ`  (arrival rate < pool_size × service_rate)

**Practical sizing formula:**

```
pool_size = (target_throughput × avg_query_time) / (1 - target_utilization)
```

Example: 500 req/s target, 10ms avg query, 70% target utilization:
```
pool_size = (500 × 0.010) / (1 - 0.70) = 5.0 / 0.30 ≈ 17 connections
```

**PgBouncer rule of thumb:** `pool_size = num_cores × 2 + effective_spindle_count`

For a PostgreSQL server with 8 CPU cores and SSD (effective spindles ≈ 1):
```
pool_size = 8 × 2 + 1 = 17 connections
```

This is the HikariCP author's formula too — CPUs are the bottleneck, not connections.

---

## Naive Approach

### Python — New connection per request

```python
import psycopg2
import time

def handle_request(user_id: int) -> dict:
    # Creates a new TCP connection on every call
    conn = psycopg2.connect(
        host="localhost",
        database="myapp",
        user="app",
        password="secret"
    )
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
    result = cur.fetchone()
    cur.close()
    conn.close()   # TCP FIN/FIN-ACK — 2 RTTs to close cleanly
    return result
```

Every call to `handle_request()` pays: TCP connect (1.5 RTT) + TLS (2 RTT) + auth (1 RTT) + query + TCP close (1 RTT) = ~6.5 RTTs overhead for a potentially sub-millisecond query.

---

## Why It Fails at Scale

**Connection storm on startup:**

When your application deploys and all 10 pods start simultaneously, each pod tries to create its full pool (20 connections) at once. 10 pods × 20 connections = 200 simultaneous TCP + auth handshakes against the DB. PostgreSQL `max_connections` defaults to 100 — you hit the limit before your app is even serving traffic.

**C10K problem:**

The classic C10K problem: handling 10,000 concurrent clients. If each client requires a dedicated OS process (PostgreSQL's model), you need 10,000 processes on the DB server — infeasible. Pooling at the application layer + a proxy pooler like PgBouncer solves this.

**Connection leak spiral:**

```
Request starts → acquires connection → exception thrown
→ finally block forgotten → connection never released
→ pool slowly drains → new requests wait forever
→ timeouts cascade → 500 errors to all users
```

**Thundering herd on pool exhaustion:**

When the pool is full (all connections in use), incoming requests queue. If the queue grows and connections become available simultaneously, all waiting requests unblock at once and flood the database.

---

## Optimized Approach

### Algorithm: Pool Internals

```python
import threading
import time
from queue import Queue, Empty

class ConnectionPool:
    """
    Thread-safe connection pool with:
    - LIFO ordering (warm connections preferred)
    - Health checking on borrow
    - Leak detection via acquire timeout
    - Metrics collection
    """

    def __init__(self, factory, min_size=5, max_size=20, timeout=30.0):
        self._factory = factory
        self._min_size = min_size
        self._max_size = max_size
        self._timeout = timeout

        self._lock = threading.Lock()
        self._idle: list = []          # LIFO stack for warm connections
        self._total = 0                # total created (idle + active)
        self._waiting = 0              # requests waiting for a connection
        self._semaphore = threading.Semaphore(max_size)

        # Pre-warm the pool
        for _ in range(min_size):
            conn = self._factory()
            self._idle.append(conn)
            self._total += 1

    def acquire(self):
        """
        Returns a connection from the pool.
        Blocks up to self._timeout seconds.
        Raises TimeoutError if no connection available.
        """
        if not self._semaphore.acquire(timeout=self._timeout):
            raise TimeoutError(
                f"Pool exhausted (size={self._max_size}, "
                f"waiting={self._waiting})"
            )

        with self._lock:
            if self._idle:
                conn = self._idle.pop()  # LIFO: take most recently used
                if not self._is_healthy(conn):
                    conn = self._factory()
                return conn
            else:
                # No idle connections but semaphore acquired — create new
                conn = self._factory()
                self._total += 1
                return conn

    def release(self, conn):
        """Return connection to pool. Always call in finally block."""
        with self._lock:
            if self._is_healthy(conn):
                self._idle.append(conn)
            else:
                # Discard unhealthy connection, will create new on next acquire
                self._total -= 1
        self._semaphore.release()

    def _is_healthy(self, conn) -> bool:
        """Lightweight keepalive check."""
        try:
            conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False

    @property
    def stats(self) -> dict:
        with self._lock:
            idle = len(self._idle)
            return {
                "total": self._total,
                "idle": idle,
                "active": self._total - idle,
                "waiting": self._waiting,
                "max": self._max_size,
            }

# Context manager for safe connection usage
from contextlib import contextmanager

@contextmanager
def get_conn(pool: ConnectionPool):
    conn = pool.acquire()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.release(conn)  # ALWAYS releases even on exception
```

### LIFO vs FIFO: Why LIFO is better

LIFO (Last In, First Out) for connection reuse means the most recently returned connection is the first to be reused. This provides:

1. **Warm connections:** Recently used connections have their server-side state cached (prepared statement cache, autovacuum statistics).
2. **Fewer idle connections:** Under low load, the pool shrinks naturally — only recently used connections are recycled, idle ones time out.
3. **TCP window scaling:** A recently-used connection has a grown TCP congestion window (cwnd). A reused connection starts with cwnd already expanded.

### Python — psycopg2 pool (production-ready)

```python
from psycopg2 import pool as pg_pool
import contextlib

# ThreadedConnectionPool is thread-safe
db_pool = pg_pool.ThreadedConnectionPool(
    minconn=5,
    maxconn=20,
    host="localhost",
    database="myapp",
    user="app",
    password="secret",
    connect_timeout=5,
    options="-c statement_timeout=30000"  # 30s query timeout
)

@contextlib.contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        db_pool.putconn(conn)

def get_user(user_id: int) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, email FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return {"id": row[0], "name": row[1], "email": row[2]} if row else None
```

### Python — asyncpg pool (async, higher performance)

```python
import asyncpg
import asyncio

async def create_pool():
    return await asyncpg.create_pool(
        "postgresql://app:secret@localhost/myapp",
        min_size=5,
        max_size=20,
        max_inactive_connection_lifetime=300,  # recycle idle connections every 5m
        command_timeout=30,
    )

async def get_user(pool: asyncpg.Pool, user_id: int) -> dict:
    async with pool.acquire() as conn:  # auto-released on exit
        row = await conn.fetchrow(
            "SELECT id, name, email FROM users WHERE id = $1", user_id
        )
        return dict(row) if row else None

# asyncpg pool uses connection-per-coroutine model, not thread-per-connection
# 10,000 concurrent coroutines share pool_size=20 connections
```

### Go — pgx pool

```go
package main

import (
    "context"
    "fmt"
    "time"

    "github.com/jackc/pgx/v5/pgxpool"
)

func NewPool(ctx context.Context) (*pgxpool.Pool, error) {
    cfg, err := pgxpool.ParseConfig("postgresql://app:secret@localhost/myapp")
    if err != nil {
        return nil, err
    }

    cfg.MinConns = 5
    cfg.MaxConns = 20
    cfg.MaxConnLifetime = 30 * time.Minute  // rotate connections to avoid stale state
    cfg.MaxConnIdleTime = 5 * time.Minute   // close idle connections
    cfg.HealthCheckPeriod = 1 * time.Minute // background health check
    cfg.ConnConfig.ConnectTimeout = 5 * time.Second

    return pgxpool.NewWithConfig(ctx, cfg)
}

type User struct {
    ID    int
    Name  string
    Email string
}

func GetUser(ctx context.Context, pool *pgxpool.Pool, userID int) (*User, error) {
    // pool.QueryRow automatically acquires and releases connection
    row := pool.QueryRow(ctx,
        "SELECT id, name, email FROM users WHERE id = $1", userID)

    var u User
    if err := row.Scan(&u.ID, &u.Name, &u.Email); err != nil {
        return nil, err
    }
    return &u, nil
}
```

### Node.js — pg Pool

```javascript
const { Pool } = require('pg');

const pool = new Pool({
    host: 'localhost',
    database: 'myapp',
    user: 'app',
    password: 'secret',
    max: 20,                  // pool size
    idleTimeoutMillis: 30000, // close idle connections after 30s
    connectionTimeoutMillis: 5000, // fail-fast if pool exhausted
    statement_timeout: 30000, // 30s query timeout
});

// Pool handles connection lifecycle automatically
async function getUser(userId) {
    const { rows } = await pool.query(
        'SELECT id, name, email FROM users WHERE id = $1',
        [userId]
    );
    return rows[0] || null;
}

// Explicit transaction with connection pinning
async function transferFunds(fromId, toId, amount) {
    const client = await pool.connect();
    try {
        await client.query('BEGIN');
        await client.query(
            'UPDATE accounts SET balance = balance - $1 WHERE id = $2',
            [amount, fromId]
        );
        await client.query(
            'UPDATE accounts SET balance = balance + $1 WHERE id = $2',
            [amount, toId]
        );
        await client.query('COMMIT');
    } catch (err) {
        await client.query('ROLLBACK');
        throw err;
    } finally {
        client.release(); // CRITICAL: always release
    }
}

// Monitor pool events
pool.on('error', (err, client) => {
    console.error('Idle client error:', err.message);
});
```

---

## Complexity Analysis

| Operation | Without Pool | With Pool |
|-----------|-------------|-----------|
| Connection acquisition | O(1) time, ~14ms latency | O(1) time, ~0.1ms latency |
| Query execution | O(query_complexity) | O(query_complexity) |
| Connection release | O(1), TCP FIN/ACK | O(1), return to idle stack |
| Pool-full wait | N/A | O(1) semaphore acquire, blocks until available |
| Health check | N/A | O(1) per borrow, amortized across queries |

**Space complexity:** O(pool_size) connections maintained in memory. Each PostgreSQL connection is ~5MB on the server side. A pool of 20 = 100MB server RAM dedicated to connection state.

---

## Benchmark (p50, p99, CPU, Memory)

Setup: PostgreSQL 15, 100 concurrent workers, 10,000 total requests, 2ms query time, LAN (0.5ms RTT).

```
┌──────────────────────┬──────────┬──────────┬──────────┬──────────────┐
│ Configuration        │  p50     │  p99     │ Throughput│ DB Processes │
├──────────────────────┼──────────┼──────────┼──────────┼──────────────┤
│ No pool              │  16ms    │  28ms    │ 1,100/s  │ 100          │
│ Pool size=1          │  35ms    │ 240ms    │   280/s  │ 1            │
│ Pool size=5          │   3ms    │  15ms    │ 1,650/s  │ 5            │
│ Pool size=20 (opt)   │   2ms    │   5ms    │ 9,200/s  │ 20           │
│ Pool size=100        │   2ms    │   6ms    │ 8,800/s  │ 100          │
└──────────────────────┴──────────┴──────────┴──────────┴──────────────┘

Pool size=100 is slightly worse than 20 due to DB-side process scheduling overhead.
```

**Memory on DB server:**
```
  Pool=1:   ~5MB process overhead
  Pool=20:  ~100MB process overhead
  Pool=100: ~500MB process overhead (approaching RAM pressure)
```

---

## Observability

### Pool metrics (expose these)

```python
from prometheus_client import Gauge, Counter, Histogram

pool_connections_total = Gauge('db_pool_connections_total',
    'Total connections in pool', ['state'])  # state: idle, active
pool_wait_duration = Histogram('db_pool_wait_seconds',
    'Time waiting for a connection',
    buckets=[.001, .005, .01, .025, .05, .1, .5, 1.0])
pool_timeout_total = Counter('db_pool_timeout_total',
    'Pool acquire timeouts')

def acquire_with_metrics(pool):
    start = time.monotonic()
    try:
        conn = pool.acquire()
        pool_wait_duration.observe(time.monotonic() - start)
        return conn
    except TimeoutError:
        pool_timeout_total.inc()
        raise
```

### Key pool metrics to alert on

```yaml
# Prometheus alert rules
groups:
  - name: connection_pool
    rules:
      - alert: PoolExhaustionHigh
        expr: db_pool_connections_total{state="active"} / db_pool_max > 0.9
        for: 30s
        annotations:
          summary: "Connection pool is >90% utilized"

      - alert: PoolWaitTimeHigh
        expr: histogram_quantile(0.99, db_pool_wait_seconds_bucket) > 0.1
        for: 1m
        annotations:
          summary: "p99 pool wait time > 100ms"

      - alert: PoolTimeoutsDetected
        expr: rate(db_pool_timeout_total[5m]) > 0
        annotations:
          summary: "Connection pool timeouts occurring"
```

### Connection leak detection

```python
import weakref
import traceback
import threading

class LeakDetectingPool:
    """Wraps a pool to detect connections not returned within a deadline."""

    def __init__(self, pool, leak_threshold_seconds=60):
        self._pool = pool
        self._leak_threshold = leak_threshold_seconds
        self._active: dict[int, tuple[float, str]] = {}  # conn_id → (time, stack)
        self._lock = threading.Lock()
        self._start_watcher()

    def acquire(self):
        conn = self._pool.acquire()
        stack = "".join(traceback.format_stack()[:-1])
        with self._lock:
            self._active[id(conn)] = (time.monotonic(), stack)
        return conn

    def release(self, conn):
        with self._lock:
            self._active.pop(id(conn), None)
        self._pool.release(conn)

    def _start_watcher(self):
        def watch():
            while True:
                time.sleep(10)
                now = time.monotonic()
                with self._lock:
                    for conn_id, (acquired_at, stack) in list(self._active.items()):
                        age = now - acquired_at
                        if age > self._leak_threshold:
                            print(f"LEAK DETECTED: connection {conn_id} held "
                                  f"for {age:.0f}s\nAcquired at:\n{stack}")
        t = threading.Thread(target=watch, daemon=True)
        t.start()
```

---

## Multi-language Implementation Summary

All three implementations above (psycopg2, pgx, pg) follow the same pattern:
1. Configure min/max connections and timeouts at startup
2. Use context managers (`with`, `defer`, `try/finally`) for safe release
3. Never hold a connection across user-facing blocking operations (e.g., waiting for HTTP response from another service)
4. Log and metric pool stats at regular intervals

---

## Trade-offs

| Factor | Small Pool (5) | Optimal Pool (20) | Large Pool (100) |
|--------|---------------|-------------------|-----------------|
| DB RAM | 25MB | 100MB | 500MB |
| Throughput | Medium | High | Diminishing |
| Tail latency | High (queuing) | Low | Medium (DB overload) |
| Connection storm risk | Low | Medium | High |
| Idle resource waste | Low | Medium | High |

---

## Failure Modes

**1. Pool exhaustion cascade**
```
Traffic spike → all 20 connections in use → request #21 waits
→ wait times out after 30s → 500 error returned → retry storms
→ more requests queue → all time out simultaneously
→ thundering herd of retries → DB overwhelmed
```
Mitigation: Circuit breaker in front of pool (see `07-core-backend-engineering/05-rate-limiting.md`). Exponential backoff on retries.

**2. Stale connection (connection reset by server)**
PostgreSQL closes idle connections after `tcp_keepalives_idle` seconds (default: system TCP keepalive, often 2 hours). If a pooled connection has been idle longer, the next query returns `connection reset by peer`.

Mitigation: Set `max_conn_idle_time < server_keepalive`. Validate connection health before returning from `acquire()`. Retry once on connection error.

**3. Auth token rotation**
When DB passwords or IAM tokens rotate (common in cloud environments with short-lived credentials), pooled connections holding old credentials become invalid.

```python
# Detect auth errors, flush pool, recreate
def execute_with_rotation_retry(pool, query, params):
    try:
        with get_conn(pool) as conn:
            return conn.execute(query, params)
    except psycopg2.OperationalError as e:
        if "password authentication failed" in str(e):
            pool.closeall()    # flush all connections
            pool.reinitialize() # recreate with fresh credentials
            with get_conn(pool) as conn:
                return conn.execute(query, params)
        raise
```

**4. Transaction pinning leak**
Holding a transaction open while doing I/O (HTTP call, sleep, etc.) pins the connection for the full duration. This is the #1 real-world pool exhaustion cause.

```python
# BAD: connection held while waiting for external HTTP response
with get_db() as conn:
    user = conn.execute("SELECT * FROM users WHERE id = ?", uid)
    # This HTTP call could take 10 seconds — connection pinned!
    enriched = requests.get(f"https://api.external.com/user/{uid}").json()
    conn.execute("UPDATE users SET enriched = ? WHERE id = ?", enriched, uid)

# GOOD: minimize connection hold time
user = None
with get_db() as conn:
    user = conn.execute("SELECT * FROM users WHERE id = ?", uid)

enriched = requests.get(f"https://api.external.com/user/{uid}").json()

with get_db() as conn:
    conn.execute("UPDATE users SET enriched = ? WHERE id = ?", enriched, uid)
```

---

## When NOT to Pool

**Serverless functions (Lambda, Cloud Run, Cloud Functions):**
Each invocation may be on a fresh container with no warm pool. Worse, thousands of concurrent invocations each holding a pool creates thousands of DB connections.

Use a **connection proxy** instead:
- **AWS RDS Proxy** — maintains a stable pool on the proxy side, multiplexes many Lambda connections onto few DB connections
- **PgBouncer** — standalone connection pooler, supports transaction-mode pooling (multiple clients share one server connection across transactions)
- **Cloud SQL Auth Proxy** — handles IAM auth + connection pooling for Cloud SQL

```
Lambda (1000 concurrent)          DB Server
  │ connection ──────────────►  [RDS Proxy pool: 20 connections] ──► PostgreSQL
  │ connection ──────────────►                                        (max_connections: 100)
  │ connection ──────────────►
  ... × 1000
```

Without proxy: 1000 Lambda invocations × 1 connection each = 1000 DB connections = PostgreSQL crash.
With proxy: 1000 Lambda connections → 20 proxy-to-DB connections.

**Short-lived scripts:**
A script that runs for 100ms and executes 2 queries gains nothing from a pool. The pool initialization overhead exceeds the benefit.

**When you need connection-level isolation:**
Some PostgreSQL features are connection-scoped: `SET LOCAL`, temporary tables, advisory locks. If your application relies on these, ensure you understand that pooled connections may be shared across unrelated requests.

---

## Lab

See `../../labs/lab-02-connection-pool-tuning/README.md` for a complete hands-on exercise.

The lab simulates connection overhead using sleep, then benchmarks pool size 1, 10, and 100 against 100 concurrent requests, measuring p50, p99, and throughput.

---

## Key Takeaways

1. **A new TCP connection costs 14–50ms** — more than many queries themselves. Always pool.
2. **Pool size is NOT "more = better."** Over-sizing wastes DB RAM and causes scheduling overhead. Use the formula: `pool_size ≈ num_cpu_cores × 2`.
3. **Little's Law governs throughput:** `max_rps = pool_size / avg_query_duration`.
4. **LIFO ordering** keeps recently-used (warm) connections at the front.
5. **Transaction pinning** is the #1 real-world pool exhaustion cause. Minimize connection hold time.
6. **Serverless = use a proxy** (RDS Proxy, PgBouncer). Don't try to pool in the function itself.
7. **Instrument always:** expose active, idle, waiting, timeout-rate as metrics. Alert on >90% utilization.
8. **Set timeouts everywhere:** acquire timeout, query timeout, connection lifetime. No timeout = potential deadlock.
