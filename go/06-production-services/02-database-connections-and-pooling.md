---
title: Database Connections & Pooling
description: Chapter 3 — a database connection is your scarcest, most expensive resource. Learn why, how a pool turns scarcity into throughput, how to size it, and the real pgx code (plus the leaks and N+1 bugs) that decide whether your service scales.
tags: ["go", "postgres", "pgx", "connection-pool", "database", "production", "chapter-3"]
status: published
access: public
publishedAt: 2026-07-09
---

# Database Connections & Pooling

> **Chapter 3 of the Go Engineering Handbook.** Chapter 2 built the server that accepts requests. But a request almost always needs *data*, and the path to your data runs through the single most contended, most expensive resource your service owns: the database connection. This chapter is about managing that scarcity. Get it wrong and your service falls over under load no matter how fast your handlers are.

Let's start with a question most engineers never ask: **why can't every request just open its own database connection?**

The answer is the whole chapter. A connection is not free — not to create, and not to *hold*. Postgres can only support so many at once. So the entire game of production database access is: **share a small number of expensive connections across a large number of cheap requests, and never lose one.** That is what a connection pool does, and understanding it is the difference between a service that handles 10,000 requests per second and one that collapses at 200.

## In this chapter you will learn

- **Why a database connection is expensive** — the TCP + TLS + auth handshake, and why Postgres connections are especially precious.
- **What a connection pool is** and how it turns "one connection per query" from a bottleneck into throughput.
- The real **`pgxpool` setup**, setting by setting.
- **How to size a pool** — the math, the fleet-wide trap, and why bigger is *not* faster.
- Using the pool correctly, and the **#1 production bug: the connection leak**.
- **Transactions**, **query cancellation** via context, and the classic **N+1 problem**.
- **Pool exhaustion** — how to see it coming and what causes it.

---

## Part 1 — Why a Connection Is Expensive

Before we pool anything, let's understand what we're conserving. What *actually happens* when your Go program opens a connection to PostgreSQL? Walk through it, because every pooling decision later is a reaction to this cost.

```
YOUR APP                                          POSTGRES SERVER
   │                                                     │
   │  1. TCP handshake (SYN, SYN-ACK, ACK) ─────────────►│   ~1 round trip
   │                                                     │
   │  2. TLS handshake (certs, key exchange) ───────────►│   ~1-2 round trips
   │  ◄──────────────────────────────────────────────── │   (if TLS, which prod uses)
   │                                                     │
   │  3. Postgres startup message (user, db) ───────────►│
   │  ◄──── auth challenge (SCRAM / password) ────────── │   ~1-2 round trips
   │  4. auth response ─────────────────────────────────►│
   │  ◄──── authentication ok, backend ready ─────────── │
   │                                                     │
   │                                          5. POSTGRES FORKS A
   │                                             NEW OS PROCESS for
   │                                             this connection
   │                                             (~5-10 MB RAM)
   │                                                     │
   │  ═══ finally ready to run a query ═════════════════ │
```

Two costs, and both are brutal if you pay them per query:

1. **Latency to establish.** Steps 1–4 are a series of network round trips — TCP, then TLS (which production always uses), then the Postgres authentication exchange (SCRAM adds round trips). On a same-datacenter link that's easily **several milliseconds**; across a network boundary it can be **tens of milliseconds**. If your handler opens a fresh connection for a query that itself takes 1 ms, you just spent 20× the query's cost on *setup*. Do that per request and your latency is dominated by handshakes, not work.

2. **Memory on the server — this is the Postgres-specific killer.** PostgreSQL uses a **process-per-connection** model: for every connection, the server **forks a full OS process** (not a thread — a process). Each backend process consumes several megabytes of RAM at baseline, plus more for its work memory. This is why `max_connections` in Postgres defaults to a mere **100**, and why pushing it to thousands doesn't work — you'd run the server out of memory and drown it in process-scheduling overhead.

> **Note — the number that governs everything:** `max_connections` on your Postgres server is a **hard, fleet-wide ceiling**. Every connection from every one of your app instances, plus your migrations, your admin tools, your monitoring, and your read replicas' consumers, must fit under it. This one number is the constraint that pool sizing exists to respect. Write it on the wall.

So the conclusion writes itself: **opening a connection per query is catastrophic**, and Postgres can't give you many connections anyway. We need to open a handful of connections *once*, keep them alive, and reuse them across thousands of requests. That is a pool.

---

## Part 2 — What a Connection Pool Is

A connection pool is a fixed-size set of already-established, ready-to-use connections that your requests **borrow and return** — they never open or close a connection themselves.

```
                        CONNECTION POOL (MaxConns = 5)
                  ┌─────────────────────────────────────────┐
   requests ────► │  [conn1: IN USE]  [conn2: IN USE]        │ ────► POSTGRES
   (goroutines)   │  [conn3: IDLE]    [conn4: IDLE]          │       (5 backend
                  │  [conn5: IDLE]                           │        processes,
                  └─────────────────────────────────────────┘        reused)
                        ▲                          │
                        │  acquire (borrow)        │  release (return)
                        └──────────────────────────┘
                     when all 5 are IN USE, the 6th request WAITS
```

The lifecycle of a single query through the pool:

1. **Acquire** — borrow an idle connection (or wait if none are free).
2. **Use** — run your query on it.
3. **Release** — return it to the pool *idle* (the connection stays open, ready for the next borrower).

The connection is **never closed** at step 3 — that's the entire point. The expensive handshake from Part 1 happens once, when the pool first fills, and then that connection serves query after query after query. A pool of 5 connections can serve millions of requests over its lifetime.

> **Tip — the mental shift:** stop thinking "a request needs a connection" and start thinking "a request needs a connection *for the few milliseconds it's actually querying*." A request that spends 5 ms querying and 50 ms doing other work only holds a connection for that 5 ms. This is why a **small** pool serves a **large** number of concurrent requests — connections are held briefly and recycled constantly. Hold them longer than necessary (a leak, a long transaction, a slow query) and the whole model breaks.

---

## Part 3 — Setting Up the Pool with pgx

In Go, two layers exist for Postgres. Know both:

- **`database/sql`** — the standard library's generic SQL interface. Works with any driver, includes a built-in pool. Generic, portable, but it doesn't expose Postgres-native features and has more overhead.
- **`pgx`** (`github.com/jackc/pgx`) — a Postgres-native driver. Faster, richer type support (arrays, JSONB, `hstore`), and `pgxpool` is a purpose-built, high-performance pool. **This is the production default for Postgres in Go.**

We'll use `pgxpool`. Here is real setup code — the kind that lives in your service's startup:

### Example

```go
package db

import (
    "context"
    "fmt"
    "time"

    "github.com/jackc/pgx/v5/pgxpool"
)

func NewPool(ctx context.Context, dsn string) (*pgxpool.Pool, error) {
    cfg, err := pgxpool.ParseConfig(dsn)
    if err != nil {
        return nil, fmt.Errorf("parse db config: %w", err)
    }

    // --- Pool sizing (Part 4 explains these numbers) ---
    cfg.MaxConns = 10                       // max open connections in this pool
    cfg.MinConns = 2                        // keep a few warm so bursts don't pay the handshake

    // --- Connection lifecycle ---
    cfg.MaxConnLifetime = 1 * time.Hour     // recycle connections hourly (avoids stale/leaky conns)
    cfg.MaxConnLifetimeJitter = 5 * time.Minute // stagger recycling so they don't all drop at once
    cfg.MaxConnIdleTime = 30 * time.Minute  // close connections idle this long (down to MinConns)
    cfg.HealthCheckPeriod = 1 * time.Minute // background check for dead connections

    pool, err := pgxpool.NewWithConfig(ctx, cfg)
    if err != nil {
        return nil, fmt.Errorf("create db pool: %w", err)
    }

    // Verify we can actually reach the database before we declare ourselves ready.
    if err := pool.Ping(ctx); err != nil {
        pool.Close()
        return nil, fmt.Errorf("ping db: %w", err)
    }
    return pool, nil
}
```

Each knob, and why it exists:

| Setting | What it controls | Why you set it |
|---|---|---|
| `MaxConns` | Ceiling on open connections in **this** pool | The single most important knob. Too low = requests queue; too high = you overwhelm Postgres. See Part 4. |
| `MinConns` | Connections kept warm even when idle | Avoids paying the handshake on the first request after a quiet period; smooths bursts. |
| `MaxConnLifetime` | Max age before a connection is recycled | Prevents connections from living forever; sidesteps slow memory creep and stale server-side state. |
| `MaxConnLifetimeJitter` | Randomness added to lifetime | So all connections don't hit `MaxConnLifetime` **at the same instant** and reconnect in a thundering herd. |
| `MaxConnIdleTime` | How long an idle connection survives | Releases connections back to Postgres during quiet periods (down to `MinConns`). |
| `HealthCheckPeriod` | How often the pool checks connection health | Detects and replaces connections the server or a network device silently killed. |

> **Note — the `MaxConnLifetimeJitter` detail is not decorative.** Without jitter, if your pool fills all at once (say, during a traffic spike), every connection is created within the same second — and then, one hour later, they *all* expire within the same second and reconnect together, hammering Postgres with a burst of handshakes at the worst possible moment. Jitter spreads that out. This is a small setting that prevents a real, periodic latency spike. Details like this are the difference between "it works" and "it works at 3 AM under load."

---

## Part 4 — Sizing the Pool (the Counterintuitive Math)

Here is the question that trips up almost everyone: **how big should the pool be?** The instinct is "bigger = faster." The instinct is **wrong**, and understanding why is the most valuable thing in this chapter.

### Bigger is not faster

Postgres processes queries with a finite number of CPU cores and disks. If you have 8 cores and you throw 200 concurrent connections at it, those 200 queries don't run in parallel — they fight over 8 cores, thrashing the CPU with context switches, contending for locks, and each running *slower*. Beyond a point, **adding connections decreases total throughput** because the server spends its time coordinating instead of working.

A widely-used starting heuristic for a single database:

```
connections ≈ (number_of_cpu_cores × 2) + effective_number_of_disks
```

For an 8-core Postgres box on SSD, that's roughly **~16–20 connections total** — often far fewer than people guess. This is a *starting point*, not a law; you tune from there with real measurements. But the shape of the truth holds: the right pool is **small**.

### The fleet-wide trap (the one that causes outages)

Now the trap that gets teams in production. Your `MaxConns` is **per pool**, and each app instance has its own pool. So:

```
total DB connections  =  number_of_app_instances  ×  MaxConns_per_instance
```

This must stay under Postgres `max_connections` (minus headroom for migrations, admin, monitoring). Watch how this bites:

```
   MaxConns = 20 per pod,  Postgres max_connections = 100

   10 pods  × 20  =  200 connections  needed
                     100               available
                     ─────────────────────────────
                     ⛔ Postgres refuses connections; requests fail
```

The killer part: this often appears **when you scale up to handle load**. Traffic spikes, your orchestrator adds pods, and each new pod's pool tries to grab 20 more connections — until you blow past `max_connections` and Postgres starts **refusing new connections**, taking down the very pods you added to help. Autoscaling and a naive pool size are a loaded gun.

> **Warning — do the fleet math before you set `MaxConns`.** The formula that must always hold: `max_pods × MaxConns + reserved < postgres_max_connections`. If you autoscale to 30 pods, and Postgres allows 100 connections, your per-pod `MaxConns` cannot exceed ~3. When that math gets impossible — when you have too many app instances for direct connections — you put a connection proxy like **PgBouncer** between your apps and Postgres. PgBouncer multiplexes thousands of client connections onto a small number of real Postgres connections (transaction pooling), decoupling your pod count from your Postgres connection count. That's the standard answer at scale.

### The framing that makes sizing intuitive

Little's Law connects the pieces: `concurrency = throughput × latency`. If each query holds a connection for 5 ms (`0.005 s`) and you need 2,000 queries/second of throughput, the concurrency you need is `2000 × 0.005 = 10` connections. That's it — 10 connections sustain 2,000 QPS *if* queries stay fast. Notice the lever: if a query slows from 5 ms to 50 ms, you suddenly need **100** connections for the same throughput. **Slow queries don't just hurt themselves — they multiply your connection demand and drain the pool.** Keeping queries fast is keeping the pool healthy.

---

## Part 5 — Using the Pool Correctly (and the #1 Bug)

For most queries, the pool handles acquire/release for you. These convenience methods borrow a connection, run the query, and return it automatically:

### Example — the common case

```go
// QueryRow: single row. Acquire + release handled for you.
func (r *Repo) GetUser(ctx context.Context, id int64) (User, error) {
    var u User
    err := r.pool.QueryRow(ctx,
        `SELECT id, email, created_at FROM users WHERE id = $1`, id,
    ).Scan(&u.ID, &u.Email, &u.CreatedAt)
    if err != nil {
        return User{}, fmt.Errorf("get user %d: %w", id, err)
    }
    return u, nil
}
```

Note `$1` — a **parameterized** placeholder. Never build SQL with string concatenation (`"... WHERE id = " + id`) — that's a SQL-injection hole *and* it defeats query-plan caching. Parameters are non-negotiable in production.

### The multi-row case — and the leak that will page you

When you query multiple rows, you get a `Rows` object that **holds a connection until you close it.** This is the source of the #1 production database bug in Go: the **connection leak**.

```go
func (r *Repo) ListUsers(ctx context.Context) ([]User, error) {
    rows, err := r.pool.Query(ctx, `SELECT id, email FROM users LIMIT 100`)
    if err != nil {
        return nil, fmt.Errorf("list users: %w", err)
    }
    defer rows.Close() // ⚠️ CRITICAL — without this, the connection LEAKS

    var users []User
    for rows.Next() {
        var u User
        if err := rows.Scan(&u.ID, &u.Email); err != nil {
            return nil, fmt.Errorf("scan user: %w", err)
        }
        users = append(users, u)
    }
    return users, rows.Err() // ALWAYS check rows.Err() after the loop
}
```

Three things here are load-bearing:

- **`defer rows.Close()`** — until you close `rows`, the borrowed connection is **not returned to the pool**. Forget this on a path that runs often, and you leak one connection per call. Do it enough times and every connection in the pool is stuck holding an unclosed `rows` — **the pool is exhausted, and every request now blocks waiting for a connection that will never come free.** The service hangs. This is the classic Go database outage, and it's always the same root cause: an unclosed `rows`.
- **`rows.Err()` after the loop** — `rows.Next()` returns `false` both when it's done *and* when it hit an error mid-iteration. If you don't check `rows.Err()`, you'll silently return a partial result set as if it were complete.
- **The context** — `ctx` flows into the query. If the request is cancelled (client disconnects, Chapter 2's timeout fires), pgx cancels the query *at Postgres* and frees the connection immediately. More on this next.

> **Tip — pgx v5 has helpers that make the loop leak-proof.** `pgx.CollectRows` with `pgx.RowToStructByName` reads all rows into a slice of structs and closes `rows` for you:
> ```go
> rows, err := r.pool.Query(ctx, `SELECT id, email FROM users LIMIT 100`)
> if err != nil { return nil, err }
> return pgx.CollectRows(rows, pgx.RowToStructByName[User])
> ```
> Prefer these in new code — a helper that can't forget to close is safer than discipline that can.

### Query cancellation is a resource-management feature

Because `ctx` reaches Postgres, a cancelled request stops wasting a connection *instantly*. This closes the loop with Chapter 2: a client disconnect → the request's context is cancelled → pgx sends a cancel to Postgres → the query stops and the connection returns to the pool. Without this, a user who gives up on a slow page would leave a query running and a connection tied up until it finished on its own. Threading `ctx` everywhere isn't ceremony — it's what keeps your scarcest resource from being wasted on work nobody's waiting for.

---

## Part 6 — Transactions

A transaction groups statements so they succeed or fail together. The production pattern has one subtlety worth internalizing — the `defer rollback` idiom:

### Example — transfer money between two accounts

```go
func (r *Repo) Transfer(ctx context.Context, from, to int64, cents int64) error {
    tx, err := r.pool.Begin(ctx) // borrows a connection for the whole transaction
    if err != nil {
        return fmt.Errorf("begin tx: %w", err)
    }
    // Safety net: if we return early (any error), the transaction is rolled back.
    // If we already committed, Rollback is a harmless no-op. This ONE line
    // guarantees we never leak an open transaction (and its connection).
    defer tx.Rollback(ctx)

    _, err = tx.Exec(ctx,
        `UPDATE accounts SET balance = balance - $1 WHERE id = $2 AND balance >= $1`,
        cents, from)
    if err != nil {
        return fmt.Errorf("debit: %w", err)
    }

    _, err = tx.Exec(ctx,
        `UPDATE accounts SET balance = balance + $1 WHERE id = $2`, cents, to)
    if err != nil {
        return fmt.Errorf("credit: %w", err) // defer rolls back the debit too
    }

    return tx.Commit(ctx) // both updates land, or neither does
}
```

> **Note — why `defer tx.Rollback(ctx)` is the whole trick.** You want a guarantee: *no matter how this function returns* — an error, a panic, an early return — the transaction never stays open. So you `defer` a rollback immediately after `Begin`. If you reach `Commit` first, the deferred `Rollback` finds an already-completed transaction and does nothing. If you *don't* reach `Commit` (any error path), the rollback fires and cleans up. One line, total coverage. An open transaction holds a connection **and** holds database locks — leaking one is even worse than leaking a plain connection.

> **Warning — keep transactions short.** A transaction holds a connection for its *entire duration*, not just per statement. So never do slow work inside a transaction — no HTTP calls, no waiting on user input, no sleeping. A transaction that stays open for seconds holds a pool connection *and* database locks for seconds, throttling everyone. Open it, do the writes, commit, get out.

---

## Part 7 — The N+1 Problem (the Most Common DB Perf Bug)

This one is worth a section of its own because you *will* meet it, in every codebase, forever. The **N+1 query problem**: you run 1 query to get a list, then loop and run 1 query *per item* — N+1 queries total, when 1 or 2 would do.

### The bug

```go
// ❌ N+1: 1 query for users, then 1 query PER user for their orders.
users, _ := repo.ListUsers(ctx)              // 1 query
for i := range users {
    users[i].Orders, _ = repo.OrdersFor(ctx, users[i].ID) // N queries!
}
// 100 users → 101 queries → 101 network round trips → 101 pool acquisitions
```

Every one of those N queries is a network round trip and a pool acquire/release. At 100 users that's 101 round trips where 1 would suffice. Under load, N+1 is the difference between a 5 ms endpoint and a 500 ms one — and it *scales with your data*, so it passes tests with 3 rows and melts in production with 3,000.

### The fix — one batched query

Fetch all the children in a single query with `WHERE ... = ANY($1)`, then group them in memory:

```go
// ✅ 2 queries total, regardless of how many users.
func (r *Repo) UsersWithOrders(ctx context.Context) ([]User, error) {
    users, err := r.ListUsers(ctx) // query 1
    if err != nil {
        return nil, err
    }
    ids := make([]int64, len(users))
    for i, u := range users {
        ids[i] = u.ID
    }

    // query 2: ALL orders for ALL users in one shot. pgx maps []int64 → a PG array.
    rows, err := r.pool.Query(ctx,
        `SELECT user_id, id, total FROM orders WHERE user_id = ANY($1)`, ids)
    if err != nil {
        return nil, err
    }
    defer rows.Close()

    byUser := make(map[int64][]Order)
    for rows.Next() {
        var o Order
        var uid int64
        if err := rows.Scan(&uid, &o.ID, &o.Total); err != nil {
            return nil, err
        }
        byUser[uid] = append(byUser[uid], o)
    }
    if err := rows.Err(); err != nil {
        return nil, err
    }

    for i := range users {
        users[i].Orders = byUser[users[i].ID]
    }
    return users, nil
}
```

**The difference:**

```
┌──────────────┬──────────┬────────────────────┬──────────────────┐
│ Approach     │ Queries  │ Round trips        │ Pool acquisitions│
├──────────────┼──────────┼────────────────────┼──────────────────┤
│ N+1          │   101    │ 101                │ 101              │
│ Batched      │     2    │   2                │   2              │
└──────────────┴──────────┴────────────────────┴──────────────────┘
   (for 100 users; the gap GROWS with your data size)
```

> **Tip — how to catch N+1 before production:** any query inside a `for` loop is a red flag; look for it in review. In tests, count queries (pgx lets you hook query execution, or watch `pool.Stat().AcquireCount()` before/after). In production, an APM/tracing tool that shows per-request query counts makes N+1 jump out — one endpoint issuing 200 queries per request is almost always this bug.

---

## Part 8 — Pool Exhaustion & Observability

**Pool exhaustion** is the failure mode all the above prevents: every connection is checked out, and new requests **block** waiting for one, then time out. The symptom is a service that hangs or times out under load while Postgres itself looks *idle* (it's not the bottleneck — your pool is). The causes are always one of:

- **Connection leaks** — unclosed `rows` or un-rolled-back transactions (Parts 5–6).
- **Slow queries** — each holds its connection longer (Little's Law: slower queries drain the pool).
- **Undersized pool** for real concurrency, or an oversized pool that hit the Postgres ceiling.
- **Downstream slowness** — if a query waits on a lock or a slow disk, connections pile up.

You don't guess at this — you **measure** it. `pgxpool` exposes live stats you should export to your metrics:

### Example — pool metrics

```go
func exportPoolStats(pool *pgxpool.Pool) {
    s := pool.Stat()
    // Export these to Prometheus/your metrics system on a ticker:
    _ = s.TotalConns()           // connections currently open
    _ = s.AcquiredConns()        // connections currently checked out (in use)
    _ = s.IdleConns()            // connections free right now
    _ = s.EmptyAcquireCount()    // times a caller had to WAIT for a connection  ← watch this
    _ = s.CanceledAcquireCount() // acquires cancelled while waiting (ctx expired) ← and this
    _ = s.AcquireDuration()      // total time spent waiting to acquire
}
```

The two metrics that scream "pool problem": **`EmptyAcquireCount`** climbing (callers are waiting because the pool is empty) and **`AcquiredConns` pinned at `MaxConns`** (fully saturated). When you see those, the pool is the bottleneck — now go find *which* of the four causes above it is. If `AcquiredConns` is maxed but Postgres CPU is low, it's almost always a **leak** or a **slow query**, not a too-small pool.

---

## Common Mistakes

- ❌ **Opening a connection per query** (`pgx.Connect` in a handler) instead of using a shared pool. Pays the handshake every time.
- ❌ **Forgetting `defer rows.Close()`** — leaks a connection per call → pool exhaustion → the service hangs. The #1 DB bug.
- ❌ **Not checking `rows.Err()`** — silently returns partial result sets.
- ❌ **Leaving a transaction open** (no `defer tx.Rollback`) — holds a connection *and* locks.
- ❌ **Slow work inside a transaction** (HTTP calls, sleeps) — holds a connection and locks for the whole duration.
- ❌ **`MaxConns × pod_count > postgres max_connections`** — connection refusals, worst during autoscaling.
- ❌ **N+1 queries** — a query inside a loop; scales with data and melts in production.
- ❌ **String-concatenated SQL** — injection risk and no plan caching. Always use `$1` parameters.
- ❌ **Not threading `ctx`** — cancelled requests keep wasting connections.

## Best Practices

- ✅ One shared `pgxpool.Pool` for the process, created at startup, `Ping`ed before "ready," closed on shutdown.
- ✅ Size the pool **small**; respect the fleet math (`pods × MaxConns + reserved < max_connections`); reach for PgBouncer at scale.
- ✅ Always `defer rows.Close()` and check `rows.Err()`; prefer `pgx.CollectRows` helpers that can't forget.
- ✅ `defer tx.Rollback(ctx)` right after `Begin`; keep transactions short.
- ✅ Batch to kill N+1 (`WHERE id = ANY($1)`, joins, or `pgx.Batch`).
- ✅ Thread `ctx` into every query so cancellation frees connections.
- ✅ Export `pool.Stat()` metrics; alert on `EmptyAcquireCount` and saturation.
- ✅ Set `MaxConnLifetime` + jitter; use parameterized queries always.

## Production Case Study

A service started returning timeouts under moderate load, and the confusing part was that **Postgres looked idle** — low CPU, plenty of headroom. The Go service, meanwhile, was pinned: every request hung for seconds then failed. Pool metrics told the story instantly: `AcquiredConns` was stuck at `MaxConns` and `EmptyAcquireCount` was climbing fast — classic pool exhaustion, but the pool wasn't undersized and there were no slow queries. The cause was a **connection leak**: a recently added endpoint queried a list of rows but returned early on a validation error *before* reaching `defer rows.Close()` — actually, the `defer` was there, but a second code path used `pool.Acquire()` manually and returned on an error without calling `conn.Release()`. Each hit of that error path leaked one connection permanently. Within minutes of any traffic on that endpoint, all 10 connections were stranded and the whole service — every endpoint, not just the buggy one — hung waiting for a connection that would never return. The fix was one `defer conn.Release()`. The lesson: **your scarcest resource is the database connection, a leak of it takes down the entire service (not just the leaky path), and the pool's own metrics will point you straight at it** — if you're exporting them.

## Chapter Summary

- A database connection is **expensive to create** (TCP + TLS + auth round trips) and **expensive to hold** (Postgres forks a process per connection, ~5–10 MB, capped by `max_connections`, default 100). Never open one per query.
- A **pool** keeps a small set of connections alive and has requests **borrow and return** them — the handshake is paid once, the connection is reused millions of times.
- Size the pool **small** (`~2×cores` as a start) and respect the **fleet ceiling**: `pods × MaxConns + reserved < max_connections`. Bigger is *not* faster; use **PgBouncer** at scale.
- The #1 bug is the **connection leak** — an unclosed `rows` or un-rolled-back transaction — which exhausts the pool and hangs the **whole service**. Always `defer rows.Close()` / `defer tx.Rollback(ctx)`.
- Thread `ctx` so cancellation **frees connections**; keep transactions **short**; **batch** to kill N+1; always use **parameterized** SQL.
- **Measure** the pool (`pool.Stat()`): `EmptyAcquireCount` and saturation are your early-warning signals.

## Chapter 3 Quiz

**Q1.** Why is opening a new Postgres connection for every query catastrophic in production? Give both reasons.

**Q2.** You run `pool.Query(...)`, loop over `rows` with `rows.Next()`, and return — but forget `defer rows.Close()`. What happens after this code path runs many times?

**Q3.** Postgres `max_connections` is 100. You autoscale to 20 pods, each with `MaxConns = 10`. What goes wrong, and when?

**Q4.** What does `defer tx.Rollback(ctx)` do if you already called `tx.Commit(ctx)` successfully?

**Q5.** An endpoint issues 201 queries per request for a list of 200 items. What's the bug called and how do you fix it?

### Answers

> **Try the questions first** — answers below.

- **A1.** (1) **Latency:** each connection needs a TCP + TLS + Postgres-auth handshake — several to tens of milliseconds of round trips, often far more than the query itself. (2) **Server memory:** Postgres forks an OS process (~5–10 MB) per connection and caps them at `max_connections`; you'd exhaust memory and connection slots.
- **A2.** The connection is never returned to the pool — a **connection leak**. After enough calls, all pool connections are stranded holding unclosed `rows`, the pool is **exhausted**, and the entire service hangs waiting for connections.
- **A3.** `20 pods × 10 = 200` connections needed vs `100` available. Postgres **refuses new connections** — and it happens precisely when you scale up under load, taking down the new pods. Fix: lower `MaxConns`, or put **PgBouncer** in front.
- **A4.** **Nothing** — the transaction is already complete, so `Rollback` is a harmless no-op. That's what makes the `defer tx.Rollback` idiom safe as a universal cleanup.
- **A5.** The **N+1 query problem**. Fix it by batching: fetch all children in one query with `WHERE id = ANY($1)` (or a JOIN), then group in memory — 2 queries instead of 201.

## Exercises

1. Write a small program that opens a fresh `pgx.Connect` per query in a loop, and one that uses a pool. Time 1,000 queries each. Measure the difference and attribute it to the handshake.
2. Deliberately write a handler that leaks a connection (query rows, return early without `Close`). Fire requests at it and watch `pool.Stat().AcquiredConns()` climb to `MaxConns` and `EmptyAcquireCount` rise. Then fix it and watch the pool recover.
3. Set `MaxConns = 2`. Fire 10 concurrent slow queries and observe requests queuing on acquire (rising `AcquireDuration`). Raise `MaxConns` and re-measure.
4. Implement the N+1 version and the batched version of "users with their orders." Count queries for both at 5 rows and 500 rows. Confirm the batched version stays at 2 queries regardless.
5. Write `Transfer` with `defer tx.Rollback`. Force an error between the debit and credit; confirm the debit is rolled back and no money is lost. Then remove the rollback and observe the leaked, open transaction.

---

Next chapter → [Caching with Redis](/backend-guide/go/06-production-services/03-caching-with-redis)

Back to → [The Go Engineering Handbook](/backend-guide/go/README)
