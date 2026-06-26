#!/usr/bin/env python3
"""
Lab 01: N+1 Query Profiling — reference solution.

Demonstrates the N+1 problem and two fix strategies.
No external dependencies required — uses SQLite in-memory.

Run:   python solution.py
Test:  python -m unittest test_lab.py
"""

import asyncio
import sqlite3
import statistics
import time
from collections import defaultdict


# ──────────────────────────────────────────────
# SETUP: Create schema and populate test data
# ──────────────────────────────────────────────

def create_database() -> sqlite3.Connection:
    """In-memory SQLite database with users and posts tables."""
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

        CREATE INDEX idx_posts_user_id ON posts(user_id);
    """)

    for i in range(1, 101):
        cur.execute(
            "INSERT INTO users VALUES (?, ?, ?)",
            (i, f"User {i}", f"user{i}@example.com"),
        )
        for j in range(5):
            cur.execute(
                "INSERT INTO posts VALUES (?, ?, ?, ?)",
                (
                    i * 10 + j,
                    f"Post {j+1} by User {i}",
                    f"This is the body of post {j+1} written by user {i}. " * 3,
                    i,
                ),
            )

    conn.commit()
    return conn


# ──────────────────────────────────────────────
# INSTRUMENTATION: Track query count
# ──────────────────────────────────────────────

class QueryTracker:
    """Wraps a SQLite connection to count queries executed."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.count = 0
        self.queries = []

    def execute(self, sql: str, params=()) -> list:
        self.count += 1
        self.queries.append(sql.strip()[:80])
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def reset(self):
        self.count = 0
        self.queries.clear()


# ──────────────────────────────────────────────
# STEP 1: Naive N+1 approach (the baseline)
# ──────────────────────────────────────────────

def fetch_users_with_posts_n_plus_one(tracker: QueryTracker) -> list[dict]:
    """1 query for users + N queries for each user's posts = N+1 queries."""
    users = tracker.execute("SELECT id, name, email FROM users")

    result = []
    for user in users:
        posts = tracker.execute(
            "SELECT id, title FROM posts WHERE user_id = ?",
            (user["id"],),
        )
        result.append({
            "id": user["id"],
            "name": user["name"],
            "posts": [dict(p) for p in posts],
        })
    return result


# ──────────────────────────────────────────────
# STEP 2: Fix #1 — JOIN (1 query)
# ──────────────────────────────────────────────

def fetch_users_with_posts_join(tracker: QueryTracker) -> list[dict]:
    """Eager loading with LEFT JOIN: 1 query, reconstructed in Python."""
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

    users: dict[int, dict] = {}
    for row in rows:
        uid = row["user_id"]
        if uid not in users:
            users[uid] = {"id": uid, "name": row["user_name"], "posts": []}
        if row["post_id"] is not None:
            users[uid]["posts"].append({
                "id": row["post_id"],
                "title": row["post_title"],
            })
    return list(users.values())


# ──────────────────────────────────────────────
# STEP 3: Fix #2 — IN batch (2 queries)
# ──────────────────────────────────────────────

def fetch_users_with_posts_in_batch(tracker: QueryTracker) -> list[dict]:
    """Two-query batch: SELECT users, then SELECT posts WHERE user_id IN (...)."""
    users = tracker.execute("SELECT id, name, email FROM users")
    user_ids = [u["id"] for u in users]

    placeholders = ",".join("?" * len(user_ids))
    posts = tracker.execute(
        f"SELECT id, title, user_id FROM posts WHERE user_id IN ({placeholders})",
        user_ids,
    )

    posts_by_user: dict[int, list] = {u["id"]: [] for u in users}
    for post in posts:
        posts_by_user[post["user_id"]].append({
            "id": post["id"],
            "title": post["title"],
        })

    return [
        {"id": u["id"], "name": u["name"], "posts": posts_by_user[u["id"]]}
        for u in users
    ]


# ──────────────────────────────────────────────
# PART 2 — DataLoader (batch N async loads into 1 query)
# ──────────────────────────────────────────────

class PostDataLoader:
    """
    Collects per-user post lookups within a single event-loop tick, then fires
    ONE batched query for all of them — the dataloader algorithm used in GraphQL.

    Each load() returns a future and schedules a dispatch on the next tick. All
    synchronous load() calls in the same tick just enqueue; dispatch then runs a
    single IN query and resolves every future.
    """

    def __init__(self, tracker: QueryTracker):
        self.tracker = tracker
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
            loop.call_soon(lambda: asyncio.ensure_future(self._dispatch()))

        return await future

    async def _dispatch(self):
        user_ids = list(set(self._queue))
        self._queue.clear()
        self._scheduled = False

        placeholders = ",".join("?" * len(user_ids))
        rows = self.tracker.execute(
            f"SELECT id, title, user_id FROM posts WHERE user_id IN ({placeholders})",
            user_ids,
        )

        posts_by_user: dict[int, list] = defaultdict(list)
        for row in rows:
            posts_by_user[row["user_id"]].append({"id": row["id"], "title": row["title"]})

        for user_id, future in self._futures.items():
            if not future.done():
                future.set_result(posts_by_user.get(user_id, []))
        self._futures.clear()


async def fetch_users_with_posts_dataloader(tracker: QueryTracker) -> list[dict]:
    """1 query for users + 1 batched query for posts, despite N load() calls."""
    users = tracker.execute("SELECT id, name FROM users")
    loader = PostDataLoader(tracker)
    posts_lists = await asyncio.gather(*[loader.load(u["id"]) for u in users])
    return [
        {"id": u["id"], "name": u["name"], "posts": posts}
        for u, posts in zip(users, posts_lists)
    ]


# ──────────────────────────────────────────────
# MEASUREMENT
# ──────────────────────────────────────────────

def benchmark(fn, tracker: QueryTracker, runs: int = 5) -> dict:
    times = []
    query_count = 0
    for _ in range(runs):
        tracker.reset()
        start = time.perf_counter()
        fn(tracker)
        times.append((time.perf_counter() - start) * 1000)
        query_count = tracker.count
    return {
        "p50_ms": statistics.median(times),
        "p99_ms": sorted(times)[int(len(times) * 0.99)] if len(times) > 1 else times[-1],
        "min_ms": min(times),
        "queries": query_count,
    }


def main():
    print("\n" + "=" * 70)
    print("LAB 01: N+1 Query Profiling")
    print("=" * 70 + "\n")

    conn = create_database()
    tracker = QueryTracker(conn)
    print("Database created: 100 users, 5 posts each (500 total posts)\n")

    results = {
        "N+1 (naive)": benchmark(fetch_users_with_posts_n_plus_one, tracker),
        "JOIN (eager)": benchmark(fetch_users_with_posts_join, tracker),
        "IN batch": benchmark(fetch_users_with_posts_in_batch, tracker),
    }

    print(f"{'Approach':<20} {'Queries':>8} {'p50 (ms)':>10} {'p99 (ms)':>10}")
    print("-" * 50)
    for name, s in results.items():
        print(f"{name:<20} {s['queries']:>8} {s['p50_ms']:>10.2f} {s['p99_ms']:>10.2f}")

    n1 = results["N+1 (naive)"]["p50_ms"]
    print(f"\nSpeedup vs N+1 (p50): JOIN {n1 / results['JOIN (eager)']['p50_ms']:.1f}x  "
          f"IN batch {n1 / results['IN batch']['p50_ms']:.1f}x")

    # Part 2 — DataLoader
    tracker.reset()
    dl_result = asyncio.run(fetch_users_with_posts_dataloader(tracker))
    print(f"\nDataLoader: {len(dl_result)} users in {tracker.count} queries "
          f"(1 users + 1 batched posts), despite {len(dl_result)} load() calls")
    conn.close()


if __name__ == "__main__":
    main()
