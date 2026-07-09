---
title: Caching with Redis
description: Chapter 4 — the fastest query is the one you never send. Learn cache-aside with real go-redis code, TTL strategy, and the three failure modes that turn a cache into an outage — stampede, penetration, and avalanche — with the code that defends each.
tags: ["go", "redis", "caching", "cache-aside", "singleflight", "production", "chapter-4"]
status: published
access: public
publishedAt: 2026-07-09
---

# Caching with Redis

> **Chapter 4 of the Go Engineering Handbook.** Chapter 3 taught you that the database connection is your scarcest resource. This chapter is about not needing it so often. A cache sits between your app and your database and answers the reads it can, so the database only sees the reads it must. Done right, it turns a struggling service into a fast one. Done wrong, it becomes the thing that takes you down.

Start from the cheapest possible truth: **the fastest query is the one you never send.**

Your database can serve maybe a few thousand reads per second before it strains. Redis can serve *hundreds of thousands* — because it's in-memory, single-purpose, and answers by key lookup instead of parsing SQL and planning queries. So if you can answer 95% of your reads from Redis, your database only sees 5% of the traffic. That's the entire value proposition. But caching introduces a second copy of your data, and a second copy means staleness, invalidation, and a whole family of failure modes that only appear under load. This chapter is about getting the upside without the outages.

## In this chapter you will learn

- **When caching helps** (and the latency/load math that proves it).
- **Cache-aside** — the default pattern — with real `go-redis` code.
- Why **every key needs a TTL**, and how to set them.
- The three failure modes that turn a cache into an outage: **stampede**, **penetration**, **avalanche** — and the code that defends each.
- Why cache **invalidation** is genuinely hard, and how to handle writes.
- **Graceful degradation** when Redis itself goes down.
- The one metric that tells you if your cache is working: **hit rate**.

---

## Part 1 — Why Cache (the Math That Justifies It)

Let's make the value concrete instead of hand-wavy. Put a cache in front of your database and the average read latency becomes:

```
effective_latency = hit_rate × cache_latency + (1 − hit_rate) × db_latency
```

Plug in real numbers — Redis at ~0.5 ms, Postgres at ~10 ms:

```
   hit_rate = 0.50 :  0.50×0.5 + 0.50×10  =  5.25 ms
   hit_rate = 0.80 :  0.80×0.5 + 0.20×10  =  2.40 ms
   hit_rate = 0.95 :  0.95×0.5 + 0.05×10  =  0.98 ms
   hit_rate = 0.99 :  0.99×0.5 + 0.01×10  =  0.60 ms
```

Two lessons jump out. First, **hit rate is everything** — a 50% hit rate barely helps (5.25 ms vs 10 ms), while 95%+ transforms your latency. A cache you don't get a high hit rate from is barely worth the complexity. Second, the *load* reduction is even more dramatic than the latency: at a 95% hit rate, your database sees **1/20th** of the read traffic. That's the difference between one database instance and twenty.

```
        WITHOUT cache                      WITH cache (95% hit)
   1000 reads/s ─► [ DATABASE ]       1000 reads/s ─► [ REDIS ]
                    (all 1000)                          │  950 hits (fast)
                                                        ▼  50 misses
                                                   [ DATABASE ]
                                                    (only 50/s — 20× less)
```

> **Note — when NOT to cache.** Caching is not free and not always right. Skip it when: the data changes on almost every read (write-heavy, low reuse — you'd cache things that are stale before they're read again); you need **strong consistency** (a cache is eventually consistent by nature — there's always a window where it disagrees with the database); or the data is cheap to compute anyway. Caching trades *staleness and complexity* for *speed and load reduction*. If you're not getting the speed/load win, you're paying the complexity for nothing.

---

## Part 2 — Setting Up go-redis

The standard client is `go-redis` (`github.com/redis/go-redis/v9`). Like `pgxpool` in Chapter 3, **it has its own connection pool** — the same principle applies: you create one client at startup, share it, and it manages a pool of connections to Redis for you.

### Example

```go
package cache

import (
    "context"
    "time"

    "github.com/redis/go-redis/v9"
)

func NewClient(ctx context.Context, addr string) (*redis.Client, error) {
    rdb := redis.NewClient(&redis.Options{
        Addr:         addr,          // "localhost:6379"
        PoolSize:     10,            // max connections (like MaxConns in Chapter 3)
        MinIdleConns: 2,             // keep a few warm

        // TIMEOUTS ARE CRITICAL: Redis is on the hot path of every request.
        // A slow Redis must fail FAST so you can fall back to the DB, not hang.
        DialTimeout:  2 * time.Second,
        ReadTimeout:  200 * time.Millisecond, // a cache read should be sub-ms; 200ms = something's wrong
        WriteTimeout: 200 * time.Millisecond,
    })

    if err := rdb.Ping(ctx).Err(); err != nil {
        _ = rdb.Close()
        return nil, err
    }
    return rdb, nil
}
```

> **Tip — the short `ReadTimeout` is deliberate and important.** Redis sits on the hot path of *every cached read*. If Redis gets slow (network blip, Redis under memory pressure), a long timeout means every request now waits on it — your cache, meant to make you faster, is now making you *slower than not having it*. A tight timeout (say 200 ms) means "if the cache can't answer almost instantly, give up and go to the database." Fail fast, degrade gracefully. We'll build on this in Part 8.

---

## Part 3 — Cache-Aside (the Default Pattern)

**Cache-aside** (also called lazy loading) is the pattern you'll use 90% of the time. The application manages the cache explicitly. Here's the logic, and it's worth memorizing as a shape:

```
READ:
  1. Look in the cache.
  2. HIT?  → return it. Done. (the fast, common path)
  3. MISS? → query the database,
             store the result in the cache (with a TTL),
             return it.

WRITE:
  1. Write to the database.
  2. DELETE the cache key (invalidate it).
```

```
   ┌────────┐  1.get   ┌───────┐   miss   ┌──────────┐
   │ handler├─────────►│ REDIS │─────────►│ DATABASE │
   │        │◄─────────┤       │◄─────────┤          │
   └────────┘  hit✓    └───────┘  2.fill  └──────────┘
                          ▲   (set key with TTL on the way back)
```

### Example — cache-aside read

```go
const userTTL = 10 * time.Minute

func (c *UserCache) GetUser(ctx context.Context, id int64) (User, error) {
    key := fmt.Sprintf("user:%d", id)

    // 1. Try the cache.
    b, err := c.rdb.Get(ctx, key).Bytes()
    if err == nil {
        var u User
        if json.Unmarshal(b, &u) == nil {
            metrics.CacheHit()      // HIT — the fast path
            return u, nil
        }
        // corrupt cache entry: fall through to the DB and overwrite it
    } else if !errors.Is(err, redis.Nil) {
        // NOTE: redis.Nil means "key not found" = a normal miss.
        // Any OTHER error is Redis itself misbehaving — log it, but DEGRADE
        // to the database rather than failing the request (Part 8).
        log.Printf("cache get %s: %v", key, err)
    }

    metrics.CacheMiss()

    // 2. Miss → go to the database (Chapter 3).
    u, err := c.repo.GetUser(ctx, id)
    if err != nil {
        return User{}, err
    }

    // 3. Populate the cache for next time (best-effort — never fail the
    //    request if the cache write fails).
    if data, err := json.Marshal(u); err == nil {
        if err := c.rdb.Set(ctx, key, data, jitter(userTTL)).Err(); err != nil {
            log.Printf("cache set %s: %v", key, err)
        }
    }
    return u, nil
}
```

Two production-grade decisions are baked in here: a Redis error (that isn't a plain miss) **degrades to the database** instead of failing the request, and the cache write is **best-effort** — a failed `Set` logs but never breaks the read. The cache is an optimization, not a source of truth; the code treats it that way.

### The write path — and why you DELETE, not UPDATE

On a write, you have two choices for the cache: overwrite the key with the new value, or delete it (so the next read repopulates it). **Prefer delete.** Here's the reasoning:

```go
func (c *UserCache) UpdateEmail(ctx context.Context, id int64, email string) error {
    if err := c.repo.UpdateEmail(ctx, id, email); err != nil { // 1. DB is source of truth
        return err
    }
    // 2. Invalidate — delete the key. The next read repopulates from the DB.
    if err := c.rdb.Del(ctx, fmt.Sprintf("user:%d", id)).Err(); err != nil {
        log.Printf("cache invalidate user:%d: %v", id, err)
    }
    return nil
}
```

> **Note — delete beats update, and here's why.** If two writes happen close together and you *update* the cache with each, they can race: write A updates the DB, write B updates the DB, then B's cache-update lands, then A's cache-update lands — and now the cache holds A's (older) value while the DB holds B's. Stale cache, silent bug. **Deleting** the key sidesteps this: whoever reads next repopulates from the DB, which is the source of truth. Simpler and race-free. This is why "write to DB, delete cache key" is the standard cache-aside write.

> **Warning — invalidation is the hard part.** "There are only two hard things in computer science: cache invalidation and naming things." The trap: forgetting to invalidate. Every code path that writes the underlying data must invalidate the cache — including background jobs, admin tools, and migrations, not just your main write handler. A single write path that forgets to `Del` the key means users see stale data until the TTL saves you. Which is exactly why the next part exists.

---

## Part 4 — TTLs: Every Key Must Expire

**Set a TTL (time-to-live) on every cache key. No exceptions.** A key without an expiry is a bug waiting to happen. The TTL is your safety net and does three jobs at once:

1. **Bounds staleness.** Even if you forget to invalidate on some write path, the data self-corrects when the TTL expires. The TTL is the *maximum* time a user can see stale data.
2. **Bounds memory.** Redis holds everything in RAM. Without expiry, your cache grows forever until Redis hits `maxmemory` and starts evicting (or refusing writes). TTLs keep the working set bounded.
3. **Self-heals bad entries.** A corrupt or wrong cache value can't live forever — it expires and gets repopulated correctly.

How to pick a TTL: short enough that stale data isn't harmful, long enough to get a good hit rate. Minutes for frequently-changing data, hours for stable reference data. And crucially — **add jitter** (we'll see why in Part 7 — the avalanche):

```go
// jitter spreads expiry so a batch of keys set together doesn't all expire
// at the same instant (which would cause a stampede — Parts 5 & 7).
func jitter(base time.Duration) time.Duration {
    delta := time.Duration(rand.Int63n(int64(base) / 5)) // up to ±20%
    return base + delta
}
```

You should also configure Redis itself with a `maxmemory` limit and an eviction policy (commonly `allkeys-lru` — evict least-recently-used keys when full) so a runaway cache degrades gracefully instead of falling over. That's a Redis-config concern, but know it's the backstop under your TTLs.

---

## Part 5 — Failure Mode 1: Cache Stampede (Thundering Herd)

Now the failure modes — the reason caching is a systems-engineering topic and not a `Set`/`Get` tutorial. This first one is the most common and the most dangerous.

**The scenario:** you have a *hot* key — say the homepage feed — served from cache, hit by 5,000 requests per second. The TTL expires. In that instant, **all 5,000 concurrent requests miss the cache at the same time**, and all 5,000 stampede to the database to recompute the same value simultaneously. Your database, which was comfortably serving 50 misses per second, is suddenly hit by 5,000 identical expensive queries at once. It falls over. And because it's down, the cache never gets repopulated, so the *next* wave stampedes too. This is a **cache stampede** (or thundering herd), and it takes down real services regularly — often right after a deploy that flushes the cache.

```
   TTL expires ──►  5000 requests all MISS at once
                         │  │  │  │  │  ...
                         ▼  ▼  ▼  ▼  ▼
                    [ DATABASE ]  ← 5000 identical queries at once → 💥
```

### The defense: singleflight (collapse duplicate work)

The key insight: those 5,000 requests all want the *same* value. So compute it **once** and share the result with all of them. Go's standard tool for exactly this is `golang.org/x/sync/singleflight`: it ensures that for a given key, only **one** function call runs at a time, and all concurrent callers for that key wait and receive that one result.

### Example

```go
import "golang.org/x/sync/singleflight"

type UserCache struct {
    rdb  *redis.Client
    repo *Repo
    sf   singleflight.Group // collapses duplicate concurrent DB loads
}

func (c *UserCache) GetUser(ctx context.Context, id int64) (User, error) {
    key := fmt.Sprintf("user:%d", id)

    // 1. Try cache (unchanged from Part 3).
    if b, err := c.rdb.Get(ctx, key).Bytes(); err == nil {
        var u User
        if json.Unmarshal(b, &u) == nil {
            return u, nil
        }
    }

    // 2. MISS. Use singleflight so that if 5000 goroutines miss this same key
    //    at once, only ONE actually queries the DB and fills the cache; the
    //    other 4999 wait and get that one result. Stampede collapsed.
    v, err, _ := c.sf.Do(key, func() (any, error) {
        u, err := c.repo.GetUser(ctx, id) // runs ONCE for all concurrent callers
        if err != nil {
            return User{}, err
        }
        if data, err := json.Marshal(u); err == nil {
            _ = c.rdb.Set(ctx, key, data, jitter(userTTL)).Err()
        }
        return u, nil
    })
    if err != nil {
        return User{}, err
    }
    return v.(User), nil
}
```

**The difference under a stampede:**

```
┌──────────────────────┬────────────────────────────┐
│ Without singleflight │ 5000 misses → 5000 DB queries → DB dies │
│ With singleflight    │ 5000 misses →    1 DB query  → all served │
└──────────────────────┴────────────────────────────┘
```

> **Tip — singleflight is per-instance; know its limit.** `singleflight.Group` collapses duplicates *within one process*. If you run 10 pods, you can still get up to 10 concurrent DB queries for a hot key (one per pod), not 5,000 — a 500× reduction, usually plenty. For truly brutal hot keys you add a second layer: a short distributed lock in Redis (`SET key value NX PX ttl`) so only one pod recomputes across the whole fleet, or **probabilistic early expiration** (recompute the value slightly *before* it expires, randomly, so it never expires under all callers at once). Start with singleflight; reach for the distributed variants only if a single hot key still hurts.

---

## Part 6 — Failure Mode 2: Cache Penetration

**The scenario:** requests keep asking for keys that **don't exist**. Cache-aside only caches things it *finds*. So a request for a nonexistent user (`user:999999999`) misses the cache, queries the DB, finds nothing, caches nothing, and returns "not found." Fine once. But if these requests keep coming — a bug, a scraper, or an attacker deliberately requesting random nonexistent IDs — *every one of them* bypasses the cache and hits the database. The cache is "penetrated"; it protects nothing. An attacker who floods you with requests for random IDs can drive your full read load straight through to the database.

### The defense: cache the negative result

Cache the *absence* too. When the DB says "not found," store a short-lived sentinel in the cache so repeat requests for that missing key are answered by Redis, not the DB:

```go
var errNotFound = errors.New("not found")
const negativeTTL = 30 * time.Second // short — the row might get created soon

func (c *UserCache) GetUser(ctx context.Context, id int64) (User, error) {
    key := fmt.Sprintf("user:%d", id)

    b, err := c.rdb.Get(ctx, key).Bytes()
    if err == nil {
        if string(b) == "\x00" { // our "known-missing" sentinel
            return User{}, errNotFound
        }
        var u User
        if json.Unmarshal(b, &u) == nil {
            return u, nil
        }
    }

    u, err := c.repo.GetUser(ctx, id)
    if errors.Is(err, sql.ErrNoRows) || errors.Is(err, pgx.ErrNoRows) {
        // Cache the NEGATIVE result briefly so repeat lookups for this missing
        // id don't keep hitting the DB. Short TTL so a later insert isn't hidden long.
        _ = c.rdb.Set(ctx, key, "\x00", negativeTTL).Err()
        return User{}, errNotFound
    }
    if err != nil {
        return User{}, err
    }
    if data, err := json.Marshal(u); err == nil {
        _ = c.rdb.Set(ctx, key, data, jitter(userTTL)).Err()
    }
    return u, nil
}
```

> **Note — keep the negative TTL short, and consider a Bloom filter at scale.** The negative cache TTL should be *short* (seconds to a minute): if the missing row gets created moments later, you don't want to keep serving "not found" for ten minutes. For very high-volume penetration (many distinct nonexistent keys), a negative cache can itself bloat memory; the heavier defense is a **Bloom filter** — a compact probabilistic set of "IDs that definitely exist" you check *before* touching cache or DB, rejecting known-nonexistent keys instantly. Start with negative caching; reach for a Bloom filter only if the missing-key space is huge.

---

## Part 7 — Failure Mode 3: Cache Avalanche

**The scenario:** a *large number* of keys expire at the **same moment**, causing a mass simultaneous miss that stampedes the database. How does this happen? The classic cause: you warm the cache at startup (or after a flush) and set every key with the *same* TTL — say exactly 10 minutes. Ten minutes later, they **all expire in the same second**, and every request misses at once. It's a stampede, but across *many* keys at once instead of one hot key — an avalanche.

```
   all keys set at T=0 with TTL=10m
        │
   T=10m: EVERY key expires simultaneously
        │
        ▼
   mass miss → database avalanche → 💥
```

### The defense: TTL jitter (you already have it)

This is why Part 4's `jitter()` matters — and note it's the *same idea* as the `MaxConnLifetimeJitter` from Chapter 3. By randomizing each key's TTL by ±20%, expirations spread out over a window instead of firing all at once. No single moment sees a mass miss.

```go
// Instead of: rdb.Set(ctx, key, data, 10*time.Minute)   ← all expire together
// Always:     rdb.Set(ctx, key, data, jitter(10*time.Minute)) ← spread out
```

> **Tip — the pattern to internalize.** "Add jitter so things don't all happen at the same instant" shows up everywhere in production systems: cache TTLs (here), connection recycling (Chapter 3), retry backoff (next chapter — jittered exponential backoff), cron staggering. Any time many actors would otherwise synchronize on the same moment, jitter desynchronizes them. Recognizing this one pattern will save you from a whole class of "why does everything spike at the same time?" incidents.

---

## Part 8 — When the Cache Is Down (Graceful Degradation)

A question that separates senior from junior design: **what happens when Redis itself goes down?**

The principle: **Redis is an optimization, not your source of truth. If it's unavailable, you degrade to the database — you do not fail.** The cache-aside code in Part 3 already does this: a Redis error (that isn't a plain miss) logs and falls through to the DB. Good.

But there's a brutal subtlety, and it's exactly the kind of second-order thinking production demands. If your database *depends* on the cache absorbing 95% of reads, then when Redis dies, **100% of read traffic hits the database at once** — and the database, sized for 5% of the load, collapses. Your Redis outage just became a database outage. The cache that made you fast also made you fragile.

Defenses, combining tools from earlier chapters:

- **Fail fast on Redis** (the short timeout from Part 2) so a *slow* Redis doesn't add latency to every request while you wait to discover it's down.
- **Load-shed at the database** — the concurrency-limiting semaphore from Chapter 2, or a **circuit breaker**, so that when the DB is suddenly overwhelmed by cache-bypass traffic, it sheds the excess (fast 503s) and stays alive for the requests it *can* serve, rather than grinding to a halt serving none.
- **Capacity-plan for partial cache loss.** Know your database's real capacity without the cache. If a Redis outage would instantly kill the DB, that's a design risk to address *before* the outage — not during.

> **Warning — the cache-dependency trap.** The most dangerous caches are the ones so effective that the team forgets the database can't survive without them. Then a routine Redis restart during a deploy takes down the whole service. Always ask: "if the cache vanished right now, does the database survive the load?" If the answer is no, you need load shedding, a circuit breaker, and a capacity plan — the cache being *down* must degrade performance, not availability.

---

## Part 9 — Observability: Hit Rate Is Everything

You cannot manage what you don't measure, and for a cache the number that matters is the **hit rate**: `hits / (hits + misses)`. Track it (the `metrics.CacheHit()` / `CacheMiss()` calls in the code). What it tells you:

- **Hit rate below ~80%** — your cache is barely helping (recall Part 1's math). Investigate: TTLs too short? Keys too granular? Caching the wrong things? A low hit rate means you're paying the complexity for little benefit.
- **Hit rate suddenly drops** — something flushed the cache (a deploy, a Redis restart, an invalidation bug), and you're now at risk of a stampede/avalanche on the database. This is an alert-worthy signal.
- **Hit rate climbing toward 99%** — the cache is doing its job; the database is well-protected.

Also watch Redis itself (`INFO` — memory usage, evictions, connected clients). Rising **evictions** mean Redis is at `maxmemory` and throwing out keys early, which *lowers your hit rate* — a sign you need more memory or shorter TTLs.

---

## Common Mistakes

- ❌ **No TTL on keys** — unbounded memory growth and stale data forever.
- ❌ **No stampede protection** — a hot key's expiry sends a thundering herd at the DB. Use `singleflight`.
- ❌ **Not caching negative results** — nonexistent-key requests penetrate straight to the DB.
- ❌ **Same TTL on many keys** — they expire together → avalanche. Add jitter.
- ❌ **Failing the request when Redis errors** — the cache is an optimization; degrade to the DB.
- ❌ **Updating the cache on write instead of deleting** — races produce stale entries. Delete the key.
- ❌ **Forgetting to invalidate on some write path** — background jobs and admin tools must invalidate too.
- ❌ **Long Redis timeouts** — a slow Redis then adds latency to every request.
- ❌ **Assuming the DB survives a Redis outage** — capacity-plan and load-shed for cache loss.

## Best Practices

- ✅ Cache-aside as the default: read-through-and-fill, write-through-DB-then-delete-key.
- ✅ A TTL on **every** key, with **jitter**; configure Redis `maxmemory` + `allkeys-lru`.
- ✅ Wrap misses in `singleflight` to collapse stampedes; add a distributed lock / early expiration for extreme hot keys.
- ✅ Cache negative results with a short TTL; Bloom filter for large missing-key spaces.
- ✅ Treat Redis as optional: short timeouts, degrade to the DB on error, best-effort writes.
- ✅ Protect the DB against cache loss with a circuit breaker / load shedding; know your no-cache capacity.
- ✅ Measure **hit rate** and Redis evictions; alert on hit-rate drops.

## Production Case Study

A service cached expensive homepage data in Redis with a 5-minute TTL and ran comfortably — the database saw a trickle of traffic. Then a routine deploy included a cache flush (a common "clear the cache to be safe" step). The instant the cache emptied, **every** request became a miss simultaneously — a textbook avalanche — and all of them stampeded the database with the same expensive query at once. The database's connection pool (Chapter 3!) exhausted immediately, queries queued and timed out, and because the DB was down the cache never refilled, so the next wave stampeded too. The service was down until traffic was throttled and the cache warmed manually. Three fixes came out of it, each from this chapter: `singleflight` around the miss path (so a mass miss recomputes each key once, not thousands of times), **TTL jitter** (so keys never again all expire together), and a **circuit breaker** in front of the database (so a stampede sheds load and the DB survives). The one-line lesson: **a cache doesn't just speed you up — it changes your failure modes, and the moment the cache is empty is the moment the database is in the most danger.**

## Chapter Summary

- Cache because **the fastest query is the one you never send**. Hit rate is everything: 95% hit rate ≈ 20× less DB load. Don't cache write-heavy or strong-consistency data.
- **Cache-aside** is the default: read → hit returns / miss fills-with-TTL; write → update DB, then **delete** the key (delete beats update — no races).
- **Every key gets a TTL** (bounds staleness + memory + self-heals) with **jitter**; set Redis `maxmemory` + LRU.
- Three failure modes and their fixes: **stampede** → `singleflight` (+ distributed lock/early expiry); **penetration** → cache negative results (+ Bloom filter); **avalanche** → TTL jitter.
- Treat Redis as **optional**: short timeouts, degrade to the DB on error — but **load-shed / circuit-break** so a Redis outage doesn't become a DB outage.
- Measure **hit rate**; a sudden drop is an early warning of a stampede in progress.

## Chapter 4 Quiz

**Q1.** Your cache has a 95% hit rate. Roughly how much read traffic does your database see compared to having no cache?

**Q2.** A single hot key expires and 3,000 concurrent requests miss it at once. What's this called, and what Go package collapses those 3,000 DB queries into 1?

**Q3.** On a write, should you update the cache key with the new value or delete it? Why?

**Q4.** Why does every cache key need a TTL — give at least two reasons — and why add jitter?

**Q5.** Redis goes down. Your code correctly degrades to the database — but the service still falls over. Why, and what two things prevent it?

### Answers

> **Try the questions first** — answers below.

- **A1.** About **1/20th** (5%). At a 95% hit rate, only the 5% of misses reach the database.
- **A2.** A **cache stampede** (thundering herd). `golang.org/x/sync/singleflight` ensures only one call per key runs at a time; the other 2,999 wait for and share that one result.
- **A3.** **Delete** it. Updating races: two near-simultaneous writes can land their cache updates out of order, leaving a stale value. Deleting forces the next read to repopulate from the DB (the source of truth) — simpler and race-free.
- **A4.** TTLs **bound staleness** (max time users see stale data), **bound memory** (Redis is RAM), and **self-heal** bad entries. **Jitter** spreads expirations so many keys don't expire at the same instant and cause an avalanche.
- **A5.** Because the database was sized for 5% of reads; when Redis dies, **100%** of reads hit it and it collapses (a Redis outage becomes a DB outage). Prevent it with **load shedding / a circuit breaker** in front of the DB and **capacity planning** for cache loss (plus fast Redis timeouts so a slow Redis fails quickly).

## Exercises

1. Implement cache-aside `GetUser` with `go-redis`. Add hit/miss counters and print the hit rate. Hammer it with repeated reads of the same IDs and watch the hit rate climb.
2. Simulate a stampede: 1,000 goroutines request the same key the instant after it expires, with an artificially slow (`time.Sleep`) DB load. Count DB calls. Add `singleflight` and confirm the count drops to 1.
3. Simulate penetration: request 10,000 random nonexistent IDs and count DB hits. Add negative caching and re-measure.
4. Set 100 keys with identical TTLs; log expiries and observe them cluster. Switch to `jitter()` and confirm they spread out.
5. Point your service at a Redis you then kill. Confirm it degrades to the DB (still serves, slower). Now add a concurrency limiter (Chapter 2) in front of the DB and confirm it sheds load instead of collapsing when Redis is down under high traffic.

---

Next chapter → *Chapter 5: Resilience — Timeouts, Retries & Circuit Breakers* — making calls to things that fail: retry with jittered backoff, circuit breakers, and the difference between a resilient service and one that amplifies outages. (Say **go ahead**.)

Back to → [The Go Engineering Handbook](/backend-guide/go/README)
