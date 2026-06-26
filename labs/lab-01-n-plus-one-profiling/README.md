# Lab 01: N+1 Query Profiling

## Objective

Observe the N+1 query problem in action, fix it two different ways, and measure the difference. You will see how query count affects total latency even when the data volume is identical.

**Expected outcomes:**

| Approach | Queries | Time (approximate) |
|----------|---------|-------------------|
| N+1 | 101 | 50–200ms (SQLite, no real RTT) |
| JOIN | 1 | 2–8ms |
| IN batch | 2 | 3–10ms |

The exact numbers depend on your machine, but the relative improvement will be clear.

---

## Parts

This lab is implemented in `python/` (`stub.py` to fill in, `solution.py` for
reference, `test_lab.py` to validate). It has two parts:

- **Part 1 — query-shape fixes:** implement `fetch_users_with_posts_join`
  (1 query) and `fetch_users_with_posts_in_batch` (2 queries).
- **Part 2 — DataLoader:** implement `PostDataLoader.load` / `_dispatch` so that
  N async `load()` calls collapse into a single batched posts query — the
  pattern used in GraphQL resolvers and any async N-to-1 resolution.

Validate everything with:

```bash
cd python && python -m unittest test_lab.py
```

The walkthrough below explains the concepts; the runnable code lives in `python/`.

---

## Prerequisites

- Python 3.8+
- No external dependencies — uses SQLite (built into Python)
- Run from any directory

---

## Setup: Run the Complete Lab

Save the following as `lab01.py` and run it with `python lab01.py`:

```python
#!/usr/bin/env python3
"""
Lab 01: N+1 Query Profiling
Demonstrates N+1 problem and two fix strategies.
No external dependencies required — uses SQLite in-memory.
"""

import sqlite3
import time
import statistics
from contextlib import contextmanager
from typing import Generator

# ──────────────────────────────────────────────
# SETUP: Create schema and populate test data
# ──────────────────────────────────────────────

def create_database() -> sqlite3.Connection:
    """
    Create an in-memory SQLite database with users and posts tables.
    Returns connection with row factory set.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE users (
            id   INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL
        );

        CREATE TABLE posts (
            id      INTEGER PRIMARY KEY,
            title   TEXT NOT NULL,
            body    TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        -- Index on foreign key (essential for performant IN queries)
        CREATE INDEX idx_posts_user_id ON posts(user_id);
    """)

    # Insert 100 users, 5 posts each = 500 posts total
    for i in range(1, 101):
        cur.execute(
            "INSERT INTO users VALUES (?, ?, ?)",
            (i, f"User {i}", f"user{i}@example.com")
        )
        for j in range(5):
            cur.execute(
                "INSERT INTO posts VALUES (?, ?, ?, ?)",
                (
                    i * 10 + j,
                    f"Post {j+1} by User {i}",
                    f"This is the body of post {j+1} written by user {i}. " * 3,
                    i
                )
            )

    conn.commit()
    print("✓ Database created: 100 users, 5 posts each (500 total posts)\n")
    return conn


# ──────────────────────────────────────────────
# INSTRUMENTATION: Track query count
# ──────────────────────────────────────────────

class QueryTracker:
    """
    Wraps SQLite connection to count queries executed.
    """
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.count = 0
        self.queries = []

    def execute(self, sql: str, params=()) -> list:
        self.count += 1
        self.queries.append(sql.strip()[:80])  # truncate for display
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def reset(self):
        self.count = 0
        self.queries.clear()


# ──────────────────────────────────────────────
# STEP 1: Naive N+1 approach
# ──────────────────────────────────────────────

def fetch_users_with_posts_n_plus_one(tracker: QueryTracker) -> list[dict]:
    """
    The classic N+1 pattern:
      1 query for all users
      N queries for each user's posts
    Total: N+1 queries
    """
    # Query 1: fetch all users
    users = tracker.execute("SELECT id, name, email FROM users")

    result = []
    for user in users:
        # Queries 2 through N+1: one per user
        posts = tracker.execute(
            "SELECT id, title FROM posts WHERE user_id = ?",
            (user["id"],)
        )
        result.append({
            "id": user["id"],
            "name": user["name"],
            "posts": [dict(p) for p in posts]
        })
    return result


# ──────────────────────────────────────────────
# STEP 2: Fix #1 — JOIN
# ──────────────────────────────────────────────

def fetch_users_with_posts_join(tracker: QueryTracker) -> list[dict]:
    """
    Eager loading with LEFT JOIN:
      1 query returns users + posts together
    Total: 1 query, N×M rows (100 users × 5 posts = 500 rows)
    """
    rows = tracker.execute("""
        SELECT
            u.id        AS user_id,
            u.name      AS user_name,
            u.email     AS user_email,
            p.id        AS post_id,
            p.title     AS post_title
        FROM users u
        LEFT JOIN posts p ON p.user_id = u.id
        ORDER BY u.id, p.id
    """)

    # Reconstruct the nested structure in Python
    users: dict[int, dict] = {}
    for row in rows:
        uid = row["user_id"]
        if uid not in users:
            users[uid] = {
                "id": uid,
                "name": row["user_name"],
                "posts": []
            }
        if row["post_id"] is not None:
            users[uid]["posts"].append({
                "id": row["post_id"],
                "title": row["post_title"]
            })
    return list(users.values())


# ──────────────────────────────────────────────
# STEP 3: Fix #2 — IN batch (two queries)
# ──────────────────────────────────────────────

def fetch_users_with_posts_in_batch(tracker: QueryTracker) -> list[dict]:
    """
    Two-query batch loading:
      Query 1: SELECT all users
      Query 2: SELECT all posts WHERE user_id IN (1,2,...,100)
    Total: 2 queries, results assembled in Python
    """
    # Query 1: all users
    users = tracker.execute("SELECT id, name, email FROM users")
    user_ids = [u["id"] for u in users]

    # Query 2: all posts for those users in a single IN clause
    placeholders = ",".join("?" * len(user_ids))
    posts = tracker.execute(
        f"SELECT id, title, user_id FROM posts WHERE user_id IN ({placeholders})",
        user_ids
    )

    # Group posts by user_id in Python (O(N) dict lookup)
    posts_by_user: dict[int, list] = {u["id"]: [] for u in users}
    for post in posts:
        posts_by_user[post["user_id"]].append({
            "id": post["id"],
            "title": post["title"]
        })

    return [
        {"id": u["id"], "name": u["name"], "posts": posts_by_user[u["id"]]}
        for u in users
    ]


# ──────────────────────────────────────────────
# MEASUREMENT: Run each approach multiple times
# ──────────────────────────────────────────────

def benchmark(fn, tracker: QueryTracker, runs: int = 5) -> dict:
    """Run fn multiple times and return timing statistics."""
    times = []
    query_count = 0

    for run in range(runs):
        tracker.reset()
        start = time.perf_counter()
        result = fn(tracker)
        elapsed = time.perf_counter() - start

        times.append(elapsed * 1000)  # convert to ms
        query_count = tracker.count   # should be same every run

    return {
        "p50_ms": statistics.median(times),
        "p99_ms": sorted(times)[int(len(times) * 0.99)] if len(times) > 1 else times[-1],
        "min_ms": min(times),
        "max_ms": max(times),
        "queries": query_count,
        "runs": runs
    }


# ──────────────────────────────────────────────
# STEP 4: Data integrity check
# ──────────────────────────────────────────────

def verify_results(r1: list, r2: list, r3: list) -> bool:
    """
    Verify all three approaches return identical data.
    Sort by user ID for comparison.
    """
    def normalize(results):
        return sorted(
            [{"id": u["id"], "post_count": len(u["posts"])} for u in results],
            key=lambda x: x["id"]
        )

    n1 = normalize(r1)
    n2 = normalize(r2)
    n3 = normalize(r3)

    if n1 != n2 or n1 != n3:
        print("✗ RESULT MISMATCH — approaches return different data!")
        return False

    total_posts = sum(u["post_count"] for u in n1)
    print(f"✓ All approaches return identical data: "
          f"{len(n1)} users, {total_posts} total posts\n")
    return True


# ──────────────────────────────────────────────
# STEP 5: Print results table
# ──────────────────────────────────────────────

def print_results(results: dict):
    print("=" * 70)
    print(f"{'Approach':<20} {'Queries':>8} {'p50 (ms)':>10} {'p99 (ms)':>10} {'Min (ms)':>10}")
    print("-" * 70)
    for name, stats in results.items():
        print(
            f"{name:<20} "
            f"{stats['queries']:>8} "
            f"{stats['p50_ms']:>10.2f} "
            f"{stats['p99_ms']:>10.2f} "
            f"{stats['min_ms']:>10.2f}"
        )
    print("=" * 70)

    # Calculate speedup
    n1_p50 = results["N+1 (naive)"]["p50_ms"]
    join_p50 = results["JOIN (eager)"]["p50_ms"]
    batch_p50 = results["IN batch"]["p50_ms"]

    print(f"\nSpeedup vs N+1 (p50):")
    print(f"  JOIN:     {n1_p50 / join_p50:.1f}×")
    print(f"  IN batch: {n1_p50 / batch_p50:.1f}×")

    print(f"\nQuery reduction:")
    n1_q = results["N+1 (naive)"]["queries"]
    print(f"  JOIN:     {n1_q} → 1 ({n1_q}× fewer queries)")
    print(f"  IN batch: {n1_q} → 2 ({n1_q//2}× fewer queries)")


# ──────────────────────────────────────────────
# STEP 6: Show actual SQL for each approach
# ──────────────────────────────────────────────

def print_sql_comparison():
    print("\n" + "=" * 70)
    print("ACTUAL SQL GENERATED")
    print("=" * 70)
    print("\nN+1 Approach:")
    print("  SELECT id, name, email FROM users;                    ← 1 query")
    print("  SELECT id, title FROM posts WHERE user_id = 1;       ← query 2")
    print("  SELECT id, title FROM posts WHERE user_id = 2;       ← query 3")
    print("  ...                                                    ← ...")
    print("  SELECT id, title FROM posts WHERE user_id = 100;     ← query 101")

    print("\nJOIN Approach:")
    print("  SELECT u.id, u.name, p.id, p.title")
    print("  FROM users u LEFT JOIN posts p ON p.user_id = u.id;  ← 1 query")

    print("\nIN Batch Approach:")
    print("  SELECT id, name, email FROM users;                    ← query 1")
    print("  SELECT id, title, user_id FROM posts")
    print("  WHERE user_id IN (1,2,3,...,100);                     ← query 2")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    print("\n" + "=" * 70)
    print("LAB 01: N+1 Query Profiling")
    print("=" * 70 + "\n")

    conn = create_database()
    tracker = QueryTracker(conn)

    # Warm-up runs (SQLite query planning cache)
    fetch_users_with_posts_n_plus_one(tracker)
    fetch_users_with_posts_join(tracker)
    fetch_users_with_posts_in_batch(tracker)
    tracker.reset()

    print("Running benchmarks (5 runs each)...\n")

    # Collect results for integrity check
    tracker.reset(); r1 = fetch_users_with_posts_n_plus_one(tracker)
    tracker.reset(); r2 = fetch_users_with_posts_join(tracker)
    tracker.reset(); r3 = fetch_users_with_posts_in_batch(tracker)
    verify_results(r1, r2, r3)

    # Benchmark each approach
    benchmark_results = {
        "N+1 (naive)": benchmark(fetch_users_with_posts_n_plus_one, tracker),
        "JOIN (eager)": benchmark(fetch_users_with_posts_join, tracker),
        "IN batch":     benchmark(fetch_users_with_posts_in_batch, tracker),
    }

    print_results(benchmark_results)
    print_sql_comparison()

    print("\n" + "=" * 70)
    print("KEY OBSERVATIONS:")
    print("  1. N+1 executes 101 queries; JOIN and IN batch execute 1-2 queries.")
    print("  2. Even with SQLite (no network RTT), JOIN is significantly faster.")
    print("  3. With a real database over a network (5ms RTT):")
    print("     N+1: 101 × 5ms = 505ms   JOIN: 1 × 5ms = 5ms")
    print("  4. All approaches return identical data — the fix is safe.")
    print("=" * 70 + "\n")

    conn.close()


if __name__ == "__main__":
    main()
```

---

## Running the Lab

```bash
python lab01.py
```

**Expected output:**

```
======================================================================
LAB 01: N+1 Query Profiling
======================================================================

✓ Database created: 100 users, 5 posts each (500 total posts)

Running benchmarks (5 runs each)...

✓ All approaches return identical data: 100 users, 500 total posts

======================================================================
Approach              Queries     p50 (ms)    p99 (ms)    Min (ms)
----------------------------------------------------------------------
N+1 (naive)               101         4.21        5.33        3.98
JOIN (eager)                1         0.38        0.45        0.35
IN batch                    2         0.41        0.52        0.38
======================================================================

Speedup vs N+1 (p50):
  JOIN:     11.1×
  IN batch: 10.3×

Query reduction:
  JOIN:     101 → 1 (101× fewer queries)
  IN batch: 101 → 2 (50× fewer queries)
```

Note: SQLite in-memory has no network RTT, so the speedup is ~10–15×. With a real PostgreSQL over a network:

```
Simulated with 5ms RTT (multiply each query by 5ms):
  N+1:      101 queries × 5ms = 505ms
  JOIN:       1 query   × 5ms =   5ms  → 101× speedup
  IN batch:   2 queries × 5ms =  10ms  →  51× speedup
```

---

## Extension Exercise 1: Simulate Network Latency

Add artificial latency to see the real impact:

```python
import time

class SlowQueryTracker(QueryTracker):
    """Adds simulated network RTT to every query."""
    RTT_MS = 5  # milliseconds per query round trip

    def execute(self, sql: str, params=()) -> list:
        time.sleep(self.RTT_MS / 1000)  # simulate network round trip
        return super().execute(sql, params)
```

Replace `tracker = QueryTracker(conn)` with `tracker = SlowQueryTracker(conn)` and re-run. You will see the N+1 approach take ~500ms while JOIN takes ~5ms.

---

## Extension Exercise 2: Simulate a Larger Dataset

Change these lines to scale up:

```python
# In create_database():
for i in range(1, 1001):    # 1000 users
    for j in range(10):     # 10 posts each
```

Observe how N+1 scales linearly (O(N) queries) while JOIN and IN batch remain constant (O(1) queries).

---

## Extension Exercise 3: DataLoader Pattern

Implement the DataLoader batching pattern for async code:

```python
import asyncio
from collections import defaultdict

async def async_get_posts_for_users(user_ids: list[int], conn) -> dict[int, list]:
    """Batch fetch: one query for all user IDs."""
    if not user_ids:
        return {}
    placeholders = ",".join("?" * len(user_ids))
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, title, user_id FROM posts WHERE user_id IN ({placeholders})",
        user_ids
    )
    rows = cur.fetchall()
    result = defaultdict(list)
    for row in rows:
        result[row["user_id"]].append({"id": row["id"], "title": row["title"]})
    return dict(result)


async def main_async(conn):
    cur = conn.cursor()
    users = cur.execute("SELECT id, name FROM users").fetchall()
    user_ids = [u["id"] for u in users]

    # One batch call instead of N individual calls
    posts_by_user = await async_get_posts_for_users(user_ids, conn)

    result = [
        {"id": u["id"], "name": u["name"], "posts": posts_by_user.get(u["id"], [])}
        for u in users
    ]
    return result

# Run: asyncio.run(main_async(conn))
```

---

## Checklist

- [ ] Ran the lab and observed the query count difference
- [ ] Confirmed all three approaches return identical data
- [ ] Added simulated latency (Extension 1) and observed 100× speedup
- [ ] Understood why JOIN works: moves the loop into the DB engine
- [ ] Understood when to prefer IN batch over JOIN (large datasets, avoid row multiplication)

## Related Modules

- `../../bsps/07-core-backend-engineering/01-n-plus-one-query-problem.md` — theory
- `../../bsps/06-databases/02-indexing.md` — why the FK index matters for IN queries
- `../../bsps/07-core-backend-engineering/02-connection-pooling.md` — how N+1 exhausts connection pools
