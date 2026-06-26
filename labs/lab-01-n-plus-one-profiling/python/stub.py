#!/usr/bin/env python3
"""
Lab 01: N+1 Query Profiling — YOUR TURN (PostgreSQL).

Runs against real Postgres through labkit. `db.query_count` is your proof: each
query increments it, so the fixes must drive 101 -> 1 -> 2 round trips.

setup_dataset() (given) creates and seeds this lab's own tables (n1_users,
n1_posts) — you never write connection or setup code. Implement the fetches:

  Part 1:
    1. fetch_users_with_posts_join     -> exactly 1 query
    2. fetch_users_with_posts_in_batch -> exactly 2 queries (use = ANY(%s))
  Part 2:
    3. PostDataLoader.load / _dispatch -> N async load() calls -> ONE posts query

Validate:
  python -m unittest test_lab.py
  python solution.py        # compare to the reference

labkit API: db.query(sql, params) -> list[dict];  db.execute(sql, params);
            db.query_count (read counter);  db.reset_counters().
"""

import asyncio
from collections import defaultdict

from labkit import db

USER_COUNT = 100
POSTS_PER_USER = 5


def setup_dataset():
    """Create + seed n1_users / n1_posts. (given)"""
    db.execute("DROP TABLE IF EXISTS n1_posts")
    db.execute("DROP TABLE IF EXISTS n1_users")
    db.execute(
        "CREATE TABLE n1_users (id INT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE n1_posts ("
        " id INT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,"
        " user_id INT NOT NULL REFERENCES n1_users(id))"
    )
    db.execute("CREATE INDEX idx_n1_posts_user_id ON n1_posts(user_id)")
    db.execute(
        "INSERT INTO n1_users "
        "SELECT g, 'User ' || g, 'user' || g || '@example.com' "
        "FROM generate_series(1, %s) g",
        (USER_COUNT,),
    )
    db.execute(
        "INSERT INTO n1_posts "
        "SELECT u * 10 + p, 'Post ' || p || ' by User ' || u, 'body of post', u "
        "FROM generate_series(1, %s) u, generate_series(0, %s) p",
        (USER_COUNT, POSTS_PER_USER - 1),
    )
    db.reset_counters()


def fetch_users_with_posts_n_plus_one() -> list[dict]:
    """The baseline you are fixing — 1 + N queries. (given)"""
    users = db.query("SELECT id, name FROM n1_users ORDER BY id")
    result = []
    for user in users:
        posts = db.query(
            "SELECT id, title FROM n1_posts WHERE user_id = %s",
            (user["id"],),
        )
        result.append({"id": user["id"], "name": user["name"], "posts": posts})
    return result


def fetch_users_with_posts_join() -> list[dict]:
    """
    TODO: same shape as the N+1 version in EXACTLY ONE query.
    Hint: LEFT JOIN n1_posts onto n1_users, then regroup the flat rows in Python.
    """
    raise NotImplementedError("Implement fetch_users_with_posts_join")


def fetch_users_with_posts_in_batch() -> list[dict]:
    """
    TODO: same shape in EXACTLY TWO queries.
    Hint: query 1 -> users; query 2 -> SELECT ... WHERE user_id = ANY(%s) passing
    the list of user ids; then group posts by user_id in Python.
    """
    raise NotImplementedError("Implement fetch_users_with_posts_in_batch")


# ── Part 2 — DataLoader ──────────────────────────────────────────────

class PostDataLoader:
    """Batch N async post lookups into ONE query."""

    def __init__(self):
        self._queue: list[int] = []
        self._futures: dict[int, asyncio.Future] = {}
        self._scheduled = False

    async def load(self, user_id: int) -> list[dict]:
        """
        TODO:
          1. Create a future on the loop; store it in self._futures[user_id].
          2. Append user_id to self._queue.
          3. The first call schedules _dispatch on the next tick (guard with
             self._scheduled): loop.call_soon(lambda: asyncio.ensure_future(self._dispatch())).
          4. return await future
        """
        raise NotImplementedError("Implement PostDataLoader.load")

    async def _dispatch(self):
        """
        TODO:
          1. Deduplicate self._queue into user_ids; clear queue; reset _scheduled.
          2. Run ONE query: SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY(%s).
          3. Group rows by user_id and resolve each future ([] if a user has none).
        """
        raise NotImplementedError("Implement PostDataLoader._dispatch")


async def fetch_users_with_posts_dataloader() -> list[dict]:
    """Wiring is given — implement PostDataLoader to make this 2 queries total."""
    users = db.query("SELECT id, name FROM n1_users ORDER BY id")
    loader = PostDataLoader()
    posts_lists = await asyncio.gather(*[loader.load(u["id"]) for u in users])
    return [
        {"id": u["id"], "name": u["name"], "posts": posts}
        for u, posts in zip(users, posts_lists)
    ]


def main():
    setup_dataset()
    for name, fn in [
        ("N+1 (naive)", fetch_users_with_posts_n_plus_one),
        ("JOIN (eager)", fetch_users_with_posts_join),
        ("IN batch (ANY)", fetch_users_with_posts_in_batch),
    ]:
        db.reset_counters()
        try:
            rows = fn()
            print(f"{name:<16} -> {len(rows)} users in {db.query_count} queries")
        except NotImplementedError as e:
            print(f"{name:<16} -> not implemented ({e})")


if __name__ == "__main__":
    main()
