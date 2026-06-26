#!/usr/bin/env python3
"""
Lab 01: N+1 Query Profiling — YOUR TURN.

The N+1 baseline is given. Your job is to implement the fixes so they return
identical data with far fewer queries:

  Part 1 — query-shape fixes:
    1. fetch_users_with_posts_join     -> exactly 1 query
    2. fetch_users_with_posts_in_batch -> exactly 2 queries

  Part 2 — the DataLoader pattern (GraphQL / async N-to-1 resolution):
    3. PostDataLoader.load + _dispatch -> N async load() calls collapse into
       ONE batched posts query

When all are done:
  python -m unittest test_lab.py     # should pass
  python solution.py                  # compare against the reference

You never touch DB connections or setup — create_database() and the tracker
are wired for you. Implement only the marked functions.
"""

import asyncio
import sqlite3
import statistics
import time
from collections import defaultdict


def create_database() -> sqlite3.Connection:
    """In-memory SQLite database with users and posts tables. (given)"""
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


class QueryTracker:
    """Counts queries executed. (given)"""

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


def fetch_users_with_posts_n_plus_one(tracker: QueryTracker) -> list[dict]:
    """The baseline you are fixing — 1 + N queries. (given)"""
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


def fetch_users_with_posts_join(tracker: QueryTracker) -> list[dict]:
    """
    TODO: Return the same shape as the N+1 version using EXACTLY ONE query.

    Hint: LEFT JOIN posts onto users, then regroup the flat rows into the
    nested {id, name, posts: [...]} structure in Python.
    """
    raise NotImplementedError("Implement fetch_users_with_posts_join")


def fetch_users_with_posts_in_batch(tracker: QueryTracker) -> list[dict]:
    """
    TODO: Return the same shape using EXACTLY TWO queries.

    Hint: query 1 -> all users; query 2 -> all posts WHERE user_id IN (...);
    then group posts by user_id in Python.
    """
    raise NotImplementedError("Implement fetch_users_with_posts_in_batch")


# ── Part 2 — DataLoader ──────────────────────────────────────────────

class PostDataLoader:
    """
    Batch N async post lookups into ONE query.

    TODO: implement load() and _dispatch() so that calling load() once per user
    (see fetch_users_with_posts_dataloader) results in a single posts query.
    """

    def __init__(self, tracker: QueryTracker):
        self.tracker = tracker
        self._queue: list[int] = []
        self._futures: dict[int, asyncio.Future] = {}
        self._scheduled = False

    async def load(self, user_id: int) -> list[dict]:
        """
        TODO:
          1. Create a future on the running loop; store it in self._futures[user_id].
          2. Append user_id to self._queue.
          3. The first call schedules _dispatch on the next tick:
             loop.call_soon(lambda: asyncio.ensure_future(self._dispatch()))
             (guard with self._scheduled so it only schedules once).
          4. return await future
        """
        raise NotImplementedError("Implement PostDataLoader.load")

    async def _dispatch(self):
        """
        TODO:
          1. Deduplicate self._queue into user_ids; clear the queue; reset _scheduled.
          2. Run ONE query: SELECT id, title, user_id FROM posts WHERE user_id IN (...).
          3. Group rows by user_id, then resolve each future with its user's posts
             (use [] for users with no posts).
        """
        raise NotImplementedError("Implement PostDataLoader._dispatch")


async def fetch_users_with_posts_dataloader(tracker: QueryTracker) -> list[dict]:
    """Wiring is given — implement PostDataLoader above to make this 2 queries total."""
    users = tracker.execute("SELECT id, name FROM users")
    loader = PostDataLoader(tracker)
    posts_lists = await asyncio.gather(*[loader.load(u["id"]) for u in users])
    return [
        {"id": u["id"], "name": u["name"], "posts": posts}
        for u, posts in zip(users, posts_lists)
    ]


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
        "queries": query_count,
    }


def main():
    conn = create_database()
    tracker = QueryTracker(conn)
    for name, fn in [
        ("N+1 (naive)", fetch_users_with_posts_n_plus_one),
        ("JOIN (eager)", fetch_users_with_posts_join),
        ("IN batch", fetch_users_with_posts_in_batch),
    ]:
        try:
            s = benchmark(fn, tracker)
            print(f"{name:<20} queries={s['queries']:>4}  p50={s['p50_ms']:.2f}ms")
        except NotImplementedError as e:
            print(f"{name:<20} not implemented yet ({e})")
    conn.close()


if __name__ == "__main__":
    main()
