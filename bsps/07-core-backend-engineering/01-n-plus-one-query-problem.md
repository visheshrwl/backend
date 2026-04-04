# N+1 Query Problem

## Problem

The N+1 query problem occurs when an application issues **one query to fetch N parent records**, then issues **N additional queries** — one per parent — to fetch associated child records. The result is N+1 total database round trips where 1 would suffice.

```
Naive flow:
  SELECT * FROM users;          ← 1 query  (returns 100 users)
  SELECT * FROM posts WHERE user_id = 1;   ← query 2
  SELECT * FROM posts WHERE user_id = 2;   ← query 3
  SELECT * FROM posts WHERE user_id = 3;   ← query 4
  ...
  SELECT * FROM posts WHERE user_id = 100; ← query 101

Total: 101 queries for data that needs 1 or 2.
```

This is the single most common performance defect in ORM-backed applications. It looks innocuous in development (small datasets) and becomes catastrophic in production (large datasets, network latency).

---

## Why It Matters (Latency, Throughput, Cost)

**Latency math:**

Each database round trip over a LAN is approximately 0.5–2ms. Over an application-to-managed-database link (RDS, Cloud SQL), it is 2–5ms. Across availability zones: 5–10ms.

```
N=100 users, RTT=5ms:
  N+1 approach: 101 queries × 5ms = 505ms per request
  JOIN approach: 2 queries × 5ms  =  10ms per request
  Speedup: 50×
```

**Throughput impact:**

If each request holds a database connection for 505ms, and your pool has 20 connections, your maximum throughput is:

```
Pool-based throughput = pool_size / request_time
  N+1:  20 / 0.505s ≈  39 requests/second
  JOIN: 20 / 0.010s ≈ 2000 requests/second
```

A 50× latency regression becomes a 50× throughput collapse.

**Cost:**

Database services are priced on I/O operations and CPU time. On AWS RDS with io1 storage, each query parse+execute cycle consumes I/O credits. Running 101 queries instead of 1 can 100× your DB costs at scale.

---

## Mental Model

Think of it as the **loop-inside-a-loop antipattern at the database tier**:

```
# What the application is doing conceptually
for user in users:                        # O(N) loop
    user.posts = db.query(                # O(N) database calls
        "SELECT * FROM posts WHERE user_id = ?", user.id
    )

# What it should do
results = db.query("""
    SELECT u.*, p.*
    FROM users u
    LEFT JOIN posts p ON p.user_id = u.id
""")                                      # O(1) database calls
```

The fix is to move the loop from application code into the database query engine, which is designed to execute it efficiently using indexes, row batching, and buffer management.

---

## Underlying Theory (OS / CN / DSA / Math Linkage)

**Network layer (CN):** Each SQL query is a TCP message. Even on a LAN, each round trip involves:
- Kernel TCP send buffer flush
- Nagle algorithm delay (if not disabled: up to 40ms per message)
- NIC interrupt + DMA transfer
- Remote TCP ACK
- Response DMA + interrupt

A single RTT is not just wire latency — it is full kernel I/O path twice.

**OS layer:** Each query requires a system call sequence on both client and server:
```
Client: write() → read()  (2 syscalls per query)
Server: accept/recv() → parse → plan → execute → send()
```
N+1 means 2N syscalls on the client side versus 2 for a single batched query.

**Algorithm complexity (DSA):**
```
N+1 approach: O(N) queries, O(N) total rows fetched
JOIN approach: O(1) query,   O(N) total rows fetched
IN approach:   O(1) query,   O(N) total rows fetched
```

The asymptotic row count is identical — the difference is entirely in query overhead (parse, plan, execute per query).

**Database query planning:** The PostgreSQL planner for `SELECT * FROM posts WHERE user_id IN (1,2,...,100)` will:
1. Check statistics for `user_id` column
2. Choose index scan (if index exists) or seq scan (if fetching > ~5% of table)
3. Execute **once**, returning all rows in a single response buffer

Compare with 100 individual queries: 100 parse cycles, 100 planner invocations, 100 execute cycles.

---

## Naive Approach

### Python — SQLAlchemy (lazy loading, default ORM behavior)

```python
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, relationship, Session

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    posts = relationship("Post", back_populates="user")  # lazy="select" by default

class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    title = Column(String)
    user_id = Column(Integer, ForeignKey("users.id"))
    user = relationship("User", back_populates="posts")

engine = create_engine("sqlite:///:memory:", echo=True)
Base.metadata.create_all(engine)

with Session(engine) as session:
    users = session.query(User).all()    # Query 1: SELECT * FROM users
    for user in users:
        # Each access to user.posts fires a new SELECT — lazy loading
        print(user.posts)               # Query 2, 3, 4, ... N+1
```

SQLAlchemy's `lazy="select"` strategy is the default. The `posts` attribute is a Python descriptor that issues a new `SELECT` every time it is accessed on an unloaded instance.

### Node.js — Sequelize (lazy loading)

```javascript
const { Sequelize, DataTypes } = require('sequelize');
const sequelize = new Sequelize('sqlite::memory:', { logging: console.log });

const User = sequelize.define('User', { name: DataTypes.STRING });
const Post = sequelize.define('Post', { title: DataTypes.STRING });
User.hasMany(Post);
Post.belongsTo(User);

async function naive() {
  const users = await User.findAll();   // 1 query
  for (const user of users) {
    const posts = await user.getPosts(); // N queries — one per user
    console.log(posts);
  }
}
```

### Go — GORM (lazy loading)

```go
package main

import (
    "gorm.io/driver/sqlite"
    "gorm.io/gorm"
)

type User struct {
    gorm.Model
    Name  string
    Posts []Post // association — not preloaded by default
}

type Post struct {
    gorm.Model
    Title  string
    UserID uint
}

func naive(db *gorm.DB) {
    var users []User
    db.Find(&users) // 1 query: SELECT * FROM users

    for i := range users {
        // GORM does NOT auto-load associations — developer must explicitly fetch
        db.Where("user_id = ?", users[i].ID).Find(&users[i].Posts)
        // This is the N+1: 1 query per user
    }
}
```

---

## Why It Fails at Scale

| Users | Queries | RTT=1ms | RTT=5ms | RTT=10ms |
|-------|---------|---------|---------|----------|
| 10    | 11      | 11ms    | 55ms    | 110ms    |
| 100   | 101     | 101ms   | 505ms   | 1,010ms  |
| 1,000 | 1,001   | 1,001ms | 5,005ms | 10,010ms |
| 10,000| 10,001  | 10s     | 50s     | 100s     |

At 100 users and 5ms RTT, the endpoint takes 505ms — already above a typical p99 SLA of 200ms.

**Connection pool starvation:** Each N+1 request holds a connection for the full 505ms. With a pool of 20 connections and 40 concurrent users, every connection is held, new requests queue, timeouts cascade. See `02-connection-pooling.md`.

**Database CPU:** Each query invocation runs through the PostgreSQL query executor:
```
parse → analyze → rewrite → plan → execute → return
```
For 100 trivially-identical queries, the planner runs 100 identical analyses. The plan cache helps somewhat in PostgreSQL 14+, but the execute+return cycles still dominate.

---

## Optimized Approach

### Strategy 1: Eager Loading with JOIN

```python
# SQLAlchemy — eager loading via joinedload
from sqlalchemy.orm import joinedload

with Session(engine) as session:
    users = (
        session.query(User)
        .options(joinedload(User.posts))  # generates a LEFT OUTER JOIN
        .all()
    )
    # No additional queries when accessing user.posts
    for user in users:
        print(user.posts)  # already populated from join result

# SQL generated:
# SELECT users.*, posts.*
# FROM users
# LEFT OUTER JOIN posts ON posts.user_id = users.id
```

**Trade-off:** JOIN multiplies rows — if each user has 5 posts, 100 users returns 500 rows. This is fine for small result sets but expensive for wide associations.

### Strategy 2: Batch Loading with IN clause

```python
# SQLAlchemy — subqueryload (separate IN query)
from sqlalchemy.orm import subqueryload

with Session(engine) as session:
    users = (
        session.query(User)
        .options(subqueryload(User.posts))  # generates IN clause query
        .all()
    )

# SQL generated:
# Query 1: SELECT * FROM users
# Query 2: SELECT * FROM posts WHERE posts.user_id IN (1, 2, 3, ..., 100)
```

This is preferred when the parent result set is large — two queries total regardless of N.

### Strategy 3: DataLoader Pattern (GraphQL / batching across resolvers)

The DataLoader pattern defers all individual loads within a single execution tick, collects them into a batch, and dispatches one query:

```python
from collections import defaultdict
import asyncio

class PostLoader:
    """
    Collects user_id lookups within a single async tick,
    then fires a single batched query.
    """
    def __init__(self, db):
        self.db = db
        self._queue: list[int] = []
        self._futures: dict[int, asyncio.Future] = {}
        self._scheduled = False

    async def load(self, user_id: int) -> list[dict]:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._futures[user_id] = future
        self._queue.append(user_id)

        if not self._scheduled:
            self._scheduled = True
            # Schedule dispatch on next tick — collect all synchronous loads first
            loop.call_soon(asyncio.ensure_future, self._dispatch())

        return await future

    async def _dispatch(self):
        user_ids = list(set(self._queue))
        self._queue.clear()
        self._scheduled = False

        # Single batched query — O(1) queries regardless of loader calls
        posts_by_user = defaultdict(list)
        rows = await self.db.fetch_all(
            "SELECT * FROM posts WHERE user_id = ANY(:ids)",
            {"ids": user_ids}
        )
        for row in rows:
            posts_by_user[row["user_id"]].append(row)

        for user_id, future in self._futures.items():
            if not future.done():
                future.set_result(posts_by_user.get(user_id, []))
        self._futures.clear()


# Usage in async context (e.g., GraphQL resolver)
async def resolve_user_posts(users, loader):
    tasks = [loader.load(user["id"]) for user in users]
    return await asyncio.gather(*tasks)
    # Despite N individual .load() calls, only 1 DB query fires
```

**DataLoader algorithm details:**
1. Call `loader.load(id)` — returns a future, enqueues the id, schedules `_dispatch` via `call_soon` (deferred to next event loop tick)
2. All synchronous code in the same tick calls `load()` multiple times — each just enqueues
3. On the next tick, `_dispatch` runs: deduplicates ids, fires ONE query, resolves all futures
4. All `await loader.load(id)` calls resume with their results

This is the JavaScript `dataloader` npm library algorithm, implemented in Python.

### Go — GORM with Preload

```go
func optimized(db *gorm.DB) {
    var users []User
    // Preload fires: SELECT * FROM posts WHERE user_id IN (1,2,...,N)
    db.Preload("Posts").Find(&users)

    for _, user := range users {
        // Posts already populated — no additional queries
        fmt.Println(user.Posts)
    }
}
```

### Node.js — Sequelize with include

```javascript
async function optimized() {
    const users = await User.findAll({
        include: [{ model: Post }]  // generates LEFT JOIN or subquery
    });
    for (const user of users) {
        console.log(user.Posts);  // populated, no additional queries
    }
}

// Or with DataLoader for GraphQL
const DataLoader = require('dataloader');

const postLoader = new DataLoader(async (userIds) => {
    const posts = await Post.findAll({
        where: { UserId: userIds }
    });
    // Must return array in same order as keys
    return userIds.map(id => posts.filter(p => p.UserId === id));
});

// In resolvers — batches automatically
async function resolvePosts(user) {
    return postLoader.load(user.id);
}
```

---

## Complexity Analysis

| Approach | Query Count | Total Rows Fetched | Time Complexity |
|----------|-------------|-------------------|-----------------|
| N+1      | O(N)        | O(N×M)            | O(N) RTTs       |
| JOIN     | O(1)        | O(N×M)            | O(1) RTTs       |
| IN batch | O(1)        | O(N×M)            | O(1) RTTs       |
| DataLoader | O(1)     | O(N×M)            | O(1) RTTs       |

Where N = number of parent records, M = average children per parent.

**Space complexity:** All approaches materialize O(N×M) rows in application memory. JOIN may increase wire bytes slightly due to repeated parent columns per child row.

---

## Benchmark (p50, p99, CPU, Memory)

Test setup: 100 users, 5 posts each, PostgreSQL 15, application on same host (RTT ≈ 0.5ms), 10 concurrent workers.

```
┌─────────────────┬────────┬────────┬──────────────┬────────┐
│ Approach        │  p50   │  p99   │ Queries/req  │ DB CPU │
├─────────────────┼────────┼────────┼──────────────┼────────┤
│ N+1 (lazy)      │ 52ms   │ 115ms  │ 101          │ 18%    │
│ JOIN (eager)    │  2ms   │   4ms  │ 1            │  2%    │
│ IN batch        │  3ms   │   6ms  │ 2            │  2%    │
│ DataLoader      │  3ms   │   7ms  │ 2            │  2%    │
└─────────────────┴────────┴────────┴──────────────┴────────┘

With RTT=5ms (cross-AZ):
  N+1:  p50=510ms, p99=620ms
  JOIN: p50=12ms,  p99=18ms
```

CPU reduction is proportional: fewer parse/plan cycles on the DB server.

---

## Observability

### Metrics to instrument

```python
# Prometheus counters and histograms
from prometheus_client import Counter, Histogram

db_query_count = Counter(
    'db_queries_total',
    'Total database queries',
    ['endpoint', 'query_type']
)
db_query_duration = Histogram(
    'db_query_duration_seconds',
    'Query duration',
    ['query_type'],
    buckets=[.001, .005, .010, .025, .050, .100, .250, .500, 1.0]
)

# Alert rule: query count per request > 10 for any endpoint
# ALERT: avg(db_queries_total) / avg(http_requests_total) > 10
```

### Slow query log (PostgreSQL)

```sql
-- postgresql.conf
log_min_duration_statement = 10  -- log queries taking > 10ms
log_statement = 'none'           -- don't log all statements

-- View slow queries
SELECT query, calls, mean_exec_time, total_exec_time
FROM pg_stat_statements
ORDER BY calls DESC
LIMIT 20;

-- Identify N+1 pattern: same query template with high call count
-- "SELECT * FROM posts WHERE user_id = $1" with calls=10000 in 1 minute
-- suggests N+1 with N=100 and 100 requests/second
```

### APM trace pattern

In distributed tracing (Jaeger, Datadog APM), N+1 appears as a **fan of identical spans**:

```
HTTP GET /users/feed       [520ms]
  ├── db.query users       [5ms]
  ├── db.query posts(1)    [5ms]
  ├── db.query posts(2)    [5ms]
  ├── db.query posts(3)    [5ms]
  │   ... × 100
  └── db.query posts(100)  [5ms]
```

Detection rule: more than 5 spans with identical operation name and different `user_id` parameter.

### Structured log pattern

```python
import structlog
log = structlog.get_logger()

class QueryCountMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        ctx = {"query_count": 0}
        # Inject into request context
        response = await self.app(scope, receive, send, ctx=ctx)
        log.info("request_complete",
                 path=scope["path"],
                 query_count=ctx["query_count"],
                 n_plus_one_suspected=ctx["query_count"] > 10)
        return response
```

---

## Multi-language Implementation

### Python — Full working example with measurement

```python
"""
N+1 demonstration and fix — SQLite, no external dependencies.
Run: python n_plus_one_demo.py
"""
import sqlite3
import time
from contextlib import contextmanager

# Setup
conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT, user_id INTEGER,
                        FOREIGN KEY (user_id) REFERENCES users(id));
""")

# Insert 100 users, 5 posts each
for i in range(1, 101):
    cur.execute("INSERT INTO users VALUES (?, ?)", (i, f"User {i}"))
    for j in range(5):
        cur.execute("INSERT INTO posts VALUES (?, ?, ?)",
                    (i * 10 + j, f"Post {j} by User {i}", i))
conn.commit()

query_counter = {"count": 0}

def tracked_execute(sql, params=()):
    query_counter["count"] += 1
    return cur.execute(sql, params).fetchall()

# === NAIVE: N+1 ===
query_counter["count"] = 0
start = time.perf_counter()
users = tracked_execute("SELECT * FROM users")
for user in users:
    posts = tracked_execute("SELECT * FROM posts WHERE user_id = ?", (user["id"],))
elapsed_n1 = time.perf_counter() - start
queries_n1 = query_counter["count"]

# === OPTIMIZED: JOIN ===
query_counter["count"] = 0
start = time.perf_counter()
rows = tracked_execute("""
    SELECT u.id, u.name, p.title
    FROM users u
    LEFT JOIN posts p ON p.user_id = u.id
""")
elapsed_join = time.perf_counter() - start
queries_join = query_counter["count"]

# === OPTIMIZED: IN clause ===
query_counter["count"] = 0
start = time.perf_counter()
users = tracked_execute("SELECT * FROM users")
user_ids = [u["id"] for u in users]
placeholders = ",".join("?" * len(user_ids))
posts = tracked_execute(f"SELECT * FROM posts WHERE user_id IN ({placeholders})", user_ids)
elapsed_in = time.perf_counter() - start
queries_in = query_counter["count"]

print(f"{'Approach':<12} {'Queries':>8} {'Time (ms)':>12}")
print("-" * 36)
print(f"{'N+1':<12} {queries_n1:>8} {elapsed_n1*1000:>12.2f}")
print(f"{'JOIN':<12} {queries_join:>8} {elapsed_join*1000:>12.2f}")
print(f"{'IN batch':<12} {queries_in:>8} {elapsed_in*1000:>12.2f}")
```

### Go — Full working example

```go
package main

import (
    "database/sql"
    "fmt"
    "sync/atomic"
    "time"

    _ "github.com/mattn/go-sqlite3"
)

var queryCount int64

func query(db *sql.DB, sql string, args ...any) *sql.Rows {
    atomic.AddInt64(&queryCount, 1)
    rows, err := db.Query(sql, args...)
    if err != nil {
        panic(err)
    }
    return rows
}

func main() {
    db, _ := sql.Open("sqlite3", ":memory:")
    db.Exec(`CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)`)
    db.Exec(`CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT, user_id INTEGER)`)

    for i := 1; i <= 100; i++ {
        db.Exec("INSERT INTO users VALUES (?, ?)", i, fmt.Sprintf("User %d", i))
        for j := 0; j < 5; j++ {
            db.Exec("INSERT INTO posts VALUES (?, ?, ?)", i*10+j, fmt.Sprintf("Post %d", j), i)
        }
    }

    // N+1
    atomic.StoreInt64(&queryCount, 0)
    start := time.Now()
    rows := query(db, "SELECT id FROM users")
    var userIDs []int
    for rows.Next() {
        var id int
        rows.Scan(&id)
        userIDs = append(userIDs, id)
    }
    rows.Close()
    for _, uid := range userIDs {
        r := query(db, "SELECT * FROM posts WHERE user_id = ?", uid)
        r.Close()
    }
    fmt.Printf("N+1:   queries=%d  time=%v\n", queryCount, time.Since(start))

    // JOIN
    atomic.StoreInt64(&queryCount, 0)
    start = time.Now()
    r := query(db, "SELECT u.id, u.name, p.title FROM users u LEFT JOIN posts p ON p.user_id = u.id")
    for r.Next() {}
    r.Close()
    fmt.Printf("JOIN:  queries=%d  time=%v\n", queryCount, time.Since(start))
}
```

### Node.js — Full working example

```javascript
const Database = require('better-sqlite3');
const db = new Database(':memory:');

db.exec(`
  CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);
  CREATE TABLE posts (id INTEGER PRIMARY KEY, title TEXT, user_id INTEGER);
`);

for (let i = 1; i <= 100; i++) {
  db.prepare('INSERT INTO users VALUES (?, ?)').run(i, `User ${i}`);
  for (let j = 0; j < 5; j++) {
    db.prepare('INSERT INTO posts VALUES (?, ?, ?)').run(i * 10 + j, `Post ${j}`, i);
  }
}

let queryCount = 0;
const origPrepare = db.prepare.bind(db);

// N+1
queryCount = 0;
let start = process.hrtime.bigint();
const users = db.prepare('SELECT * FROM users').all();
queryCount++;
for (const user of users) {
  db.prepare('SELECT * FROM posts WHERE user_id = ?').all(user.id);
  queryCount++;
}
console.log(`N+1:  queries=${queryCount}  time=${Number(process.hrtime.bigint() - start) / 1e6}ms`);

// JOIN
queryCount = 0;
start = process.hrtime.bigint();
db.prepare('SELECT u.*, p.* FROM users u LEFT JOIN posts p ON p.user_id = u.id').all();
queryCount++;
console.log(`JOIN: queries=${queryCount}  time=${Number(process.hrtime.bigint() - start) / 1e6}ms`);
```

---

## Trade-offs

| Factor | N+1 | JOIN | IN Batch | DataLoader |
|--------|-----|------|----------|------------|
| Simplicity | High | Medium | Medium | Low |
| Query count | O(N) | O(1) | O(1) | O(1) |
| Memory (wire) | Low | High (duplicated parent cols) | Low | Low |
| Works with pagination | Yes | Complex | Yes | Yes |
| Works with GraphQL | No | Complex | No | **Yes** |
| DB index usage | Excellent | Depends on join type | Good with idx on FK | Good |
| Large N (N>10k) | Catastrophic | Risk row explosion | Risk IN list too long | Best |

---

## Failure Modes

**1. Cache thundering herd on batch:**
If you batch 1000 IDs into one IN clause and that query misses the DB cache entirely, you materialize a huge result set at once. A slow query under load causes cascading connection pool exhaustion. See `02-connection-pooling.md`.

**Mitigation:** Chunk large IN clauses to maximum 1000 IDs per batch.

```python
def chunked_load(user_ids, chunk_size=1000):
    for i in range(0, len(user_ids), chunk_size):
        chunk = user_ids[i:i+chunk_size]
        placeholders = ",".join("?" * len(chunk))
        yield cur.execute(
            f"SELECT * FROM posts WHERE user_id IN ({placeholders})", chunk
        ).fetchall()
```

**2. Memory explosion on huge IN clause:**
PostgreSQL parses IN lists into an array. At 10,000+ items, the IN clause itself becomes slow. Use a temporary table or `= ANY(ARRAY[...])` instead.

```sql
-- Better for large sets:
SELECT * FROM posts WHERE user_id = ANY(ARRAY[1,2,3,...,10000]::int[])

-- Even better for very large sets: use a subquery or temp table
```

**3. JOIN row explosion:**
If each user has 100 posts and you JOIN, 1000 users returns 100,000 rows. Wide parent tables multiply wire bytes dramatically.

**4. DataLoader ordering bugs:**
DataLoader requires results in the **same order as input keys**. Returning unordered results causes data mismatches:

```javascript
// BUG: findAll returns results in database order, not key order
const postLoader = new DataLoader(async (userIds) => {
    const posts = await Post.findAll({ where: { UserId: userIds } });
    return userIds.map(id => posts.filter(p => p.UserId === id)); // CORRECT mapping
});
```

---

## When NOT to Use

**When NOT to batch with IN clause:**
- Result set is enormous (>100k rows) — prefer pagination + streaming
- When ordering is critical and cannot be replicated in application code
- When child table has no index on the foreign key — IN clause causes a seq scan N times

**When NOT to use JOIN:**
- When the join multiplies rows dramatically (many-to-many with large sets)
- When you only need aggregate counts (`SELECT user_id, COUNT(*) FROM posts GROUP BY user_id` is often better)

**When NOT to use DataLoader:**
- Outside of async contexts — DataLoader requires an event loop tick to batch
- For sequential, ordered processing where you need results immediately

**When N+1 is acceptable:**
- N is provably bounded small (e.g., fetching metadata for a fixed 3-item navbar)
- The query is cached at application level (result cached for N seconds)
- Development/admin tools where correctness beats performance

---

## Lab

See `../../labs/lab-01-n-plus-one-profiling/README.md` for a complete hands-on exercise with measurable outcomes.

The lab walks through:
1. Schema setup with 100 users × 5 posts
2. Running and profiling the N+1 pattern
3. Fixing with JOIN, measuring improvement
4. Fixing with IN batch, measuring improvement
5. Comparing all approaches in a result table

---

## Key Takeaways

1. **N+1 is O(N) queries where O(1) is possible.** The fix is always "move the loop into the database."
2. **The cost is RTT × N**, not just compute. On cross-AZ links (5ms RTT), 100 users = 500ms of pure wait.
3. **ORM lazy loading is the #1 source of N+1.** Always audit `.all()` calls followed by attribute access in a loop.
4. **Two solutions:** JOIN (one query, more wire bytes) vs IN batch (two queries, efficient wire).
5. **DataLoader** is the correct pattern for GraphQL and any N-to-1 resolution in async code.
6. **Observe it:** query count per request is the leading indicator. Alert when queries/request > 10.
7. **Chunk large IN clauses** to ≤1000 items. Beyond that, use `= ANY(ARRAY[...])` or a subquery.
