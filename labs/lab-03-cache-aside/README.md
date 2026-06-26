# Lab 03: Cache-Aside with Postgres + Redis

## Objective

Place a Redis cache in front of a Postgres table using the **cache-aside**
(lazy-loading) pattern — the application checks and populates the cache itself.
This is strategy 1 in the chapter; it is *not* read-through (where the cache
layer loads on a miss). Prove two things with tests:

1. A **cache hit does not touch Postgres** (measured via `db.query_count`).
2. A **write invalidates** the cached copy so reads never go stale.

This is the most common caching pattern in production backends, and the place
most teams get invalidation wrong.

---

## The environment (zero setup)

Postgres and Redis are already running and seeded when you launch the lab — in
Codespaces/Gitpod they come up automatically; locally they start with
`docker compose -f .devcontainer/docker-compose.yml up -d`.

> **Available in 8 languages** — Python, Go, JavaScript, TypeScript, Ruby, Rust,
> C++, and C. Each `<lang>/` folder has a `stub` to implement and a `solution`
> for reference. The single-flight guard is written idiomatically per language
> (a per-key mutex in threaded runtimes; an in-flight-promise map in Node).

You never open a connection. The platform layer hands you ready clients:

```python
from labkit import db, cache
```

| Layer | Handle | What you use |
|-------|--------|--------------|
| Postgres (persistence) | `db` | `db.queryone(sql, params)`, `db.execute(sql, params)`, `db.query_count` |
| Redis (cache) | `cache` | `cache.get_json(key)`, `cache.set_json(key, value, ttl=...)`, `cache.delete(key)`, `cache.exists(key)` |

The `users` table is pre-seeded (5 rows). `db.query_count` increments on every
query — that is how the tests prove your cache actually saves a database hit.

---

## Your task

Open `python/stub.py`. The lab has two parts:

**Part 1 — cache-aside**
- `get_user_profile(user_id)` — read from cache first; on a miss read Postgres
  and populate the cache; return `None` if the user does not exist.
- `update_user_plan(user_id, plan)` — write to Postgres, then invalidate the
  cached entry.

**Part 2 — stampede protection (single-flight)**
- `get_user_profile_singleflight(user_id)` — when a popular key expires and many
  requests miss at once, all of them would hit Postgres simultaneously (a *cache
  stampede* / *thundering herd*). Use a per-key lock with a double-check so that
  **only one** thread queries Postgres and the rest reuse its result. The test
  fires 50 concurrent cold-cache reads and asserts exactly **one** DB query.

## Validate

```bash
python -m unittest test_lab.py
```

A green `OK` means cache hits skip Postgres and invalidation works. Compare your
approach with the reference:

```bash
python solution.py
```

## Related module

- `../../bsps/07-core-backend-engineering/03-caching-strategy.md` — theory
