#!/usr/bin/env python3
"""
Lab 01: N+1 Query Profiling — reference solution (PostgreSQL).

Runs against real Postgres through labkit, so the query count maps to real
database round trips — exactly where N+1 hurts. `db.query_count` is the
instrument: it increments on every query, so you can prove 101 -> 1 -> 2.

The lab creates and seeds its own tables (n1_users, n1_posts); it does not
touch the shared `users` table used by other labs.

Run:   python solution.py
Test:  python -m unittest test_lab.py
"""

import asyncio
from collections import defaultdict

from labkit import db

USER_COUNT = 100
POSTS_PER_USER = 5


# ──────────────────────────────────────────────
# SETUP — create + seed this lab's own tables (given)
# ──────────────────────────────────────────────

def setup_dataset():
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


# ──────────────────────────────────────────────
# STEP 1 — Naive N+1 (the baseline you are fixing)
# ──────────────────────────────────────────────

def fetch_users_with_posts_n_plus_one() -> list[dict]:
    """1 query for users + N queries for each user's posts = N+1 round trips."""
    users = db.query("SELECT id, name FROM n1_users ORDER BY id")
    result = []
    for user in users:
        posts = db.query(
            "SELECT id, title FROM n1_posts WHERE user_id = %s",
            (user["id"],),
        )
        result.append({"id": user["id"], "name": user["name"], "posts": posts})
    return result


# ──────────────────────────────────────────────
# STEP 2 — Fix #1: JOIN (1 query)
# ──────────────────────────────────────────────

def fetch_users_with_posts_join() -> list[dict]:
    rows = db.query(
        "SELECT u.id AS user_id, u.name AS user_name, p.id AS post_id, p.title AS post_title "
        "FROM n1_users u LEFT JOIN n1_posts p ON p.user_id = u.id "
        "ORDER BY u.id, p.id"
    )
    users: dict[int, dict] = {}
    for row in rows:
        uid = row["user_id"]
        if uid not in users:
            users[uid] = {"id": uid, "name": row["user_name"], "posts": []}
        if row["post_id"] is not None:
            users[uid]["posts"].append({"id": row["post_id"], "title": row["post_title"]})
    return list(users.values())


# ──────────────────────────────────────────────
# STEP 3 — Fix #2: IN batch via = ANY (2 queries)
# ──────────────────────────────────────────────

def fetch_users_with_posts_in_batch() -> list[dict]:
    users = db.query("SELECT id, name FROM n1_users ORDER BY id")
    user_ids = [u["id"] for u in users]
    posts = db.query(
        "SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY(%s)",
        (user_ids,),
    )
    posts_by_user: dict[int, list] = {u["id"]: [] for u in users}
    for post in posts:
        posts_by_user[post["user_id"]].append({"id": post["id"], "title": post["title"]})
    return [
        {"id": u["id"], "name": u["name"], "posts": posts_by_user[u["id"]]}
        for u in users
    ]


# ──────────────────────────────────────────────
# PART 2 — DataLoader (batch N async loads into 1 query)
# ──────────────────────────────────────────────

class PostDataLoader:
    """Collect per-user lookups within one tick, then fire ONE = ANY query."""

    def __init__(self):
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
        rows = db.query(
            "SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY(%s)",
            (user_ids,),
        )
        posts_by_user: dict[int, list] = defaultdict(list)
        for row in rows:
            posts_by_user[row["user_id"]].append({"id": row["id"], "title": row["title"]})
        for user_id, future in self._futures.items():
            if not future.done():
                future.set_result(posts_by_user.get(user_id, []))
        self._futures.clear()


async def fetch_users_with_posts_dataloader() -> list[dict]:
    """1 query for users + 1 batched query for posts, despite N load() calls."""
    users = db.query("SELECT id, name FROM n1_users ORDER BY id")
    loader = PostDataLoader()
    posts_lists = await asyncio.gather(*[loader.load(u["id"]) for u in users])
    return [
        {"id": u["id"], "name": u["name"], "posts": posts}
        for u, posts in zip(users, posts_lists)
    ]


def main():
    print("=" * 60)
    print("LAB 01: N+1 Query Profiling (PostgreSQL)")
    print("=" * 60)
    setup_dataset()
    print(f"Seeded {USER_COUNT} users x {POSTS_PER_USER} posts in Postgres\n")

    for name, fn in [
        ("N+1 (naive)", fetch_users_with_posts_n_plus_one),
        ("JOIN (eager)", fetch_users_with_posts_join),
        ("IN batch (ANY)", fetch_users_with_posts_in_batch),
    ]:
        db.reset_counters()
        rows = fn()
        print(f"{name:<16} -> {len(rows)} users in {db.query_count} queries")

    db.reset_counters()
    dl = asyncio.run(fetch_users_with_posts_dataloader())
    print(f"{'DataLoader':<16} -> {len(dl)} users in {db.query_count} queries "
          f"(1 users + 1 batched), despite {len(dl)} load() calls")


if __name__ == "__main__":
    main()
