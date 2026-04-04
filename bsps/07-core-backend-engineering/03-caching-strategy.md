# Caching Strategy

## Problem

Every data read that bypasses a cache hits the origin — a database, remote API, or expensive computation. At scale, the origin cannot serve all reads directly:

```
Without cache:
  1,000 req/s × 10ms DB latency = 10,000ms of DB time/second
  = 10 DB connections fully saturated just for reads

With 95% cache hit rate:
  1,000 req/s × 5% miss rate = 50 cache misses/s hitting DB
  = 0.5 DB connections needed for reads
  = 20× reduction in DB load
```

The problem is not just "add a cache and it works." The hard problems are: cache invalidation, cold starts, thundering herd on misses, and choosing the right placement strategy.

---

## Why It Matters (Latency, Throughput, Cost)

**The fundamental cache equation:**

```
effective_latency = hit_rate × cache_latency + (1 - hit_rate) × origin_latency
```

Concrete example with Redis (0.5ms) and PostgreSQL (10ms):

```
hit_rate=0.50: 0.50×0.5ms + 0.50×10ms = 0.25 + 5.00 = 5.25ms
hit_rate=0.80: 0.80×0.5ms + 0.20×10ms = 0.40 + 2.00 = 2.40ms
hit_rate=0.95: 0.95×0.5ms + 0.05×10ms = 0.475 + 0.50 = 0.975ms
hit_rate=0.99: 0.99×0.5ms + 0.01×10ms = 0.495 + 0.10 = 0.595ms
```

Going from 80% to 99% hit rate: 4× latency reduction (2.4ms → 0.6ms).

**The cache hit rate is everything.** A 50% hit rate barely helps. Target ≥90%.

**Cost:**
- Redis on AWS ElastiCache: ~$0.025/GB-hour
- RDS PostgreSQL: ~$0.25/GB-hour
- Read replica: $0.125/GB-hour

Serving reads from cache is 10× cheaper than read replicas.

---

## Mental Model

Think of the memory hierarchy:

```
Level          Latency    Capacity    Cost/GB
──────────────────────────────────────────────
CPU L1 cache    4 cycles     32 KB    $10,000+
CPU L2 cache   12 cycles    256 KB    $10,000+
CPU L3 cache   40 cycles      8 MB    $10,000+
RAM           200 cycles    32-512GB  $5-20
NVMe SSD     10,000 cycles  1-8 TB    $0.10-0.50
Network HDD 100,000 cycles  many TB   $0.02-0.10
Remote DB    500,000 cycles  ---       varies
────────────────────────────────────────────────────────────
Application cache (Redis):   500 cycles over network ≈ 0.5ms
```

Caching moves data "up" the hierarchy. Redis at 0.5ms is to PostgreSQL at 10ms what L1 cache is to RAM.

---

## Underlying Theory (OS / CN / DSA / Math Linkage)

### Zipf Distribution — Why Caches Work

Real-world access patterns follow a **Zipf (power law) distribution**: the most popular item is accessed proportionally more than the second most popular, which is more than the third, etc.

```
P(rank=k) ∝ 1/k^s   (s ≈ 1 for web workloads)

Rank 1 item:   30% of all accesses
Rank 2 item:   15% of all accesses
Rank 3 item:   10% of all accesses
Top 20% of items: ~80% of all accesses  ← Pareto principle
```

If you cache only the top 20% of your item catalog, you achieve ~80% hit rate. This is why a small cache (10% of data size) can yield excellent hit rates.

### LRU Algorithm: Doubly Linked List + Hash Map

The Least Recently Used (LRU) eviction algorithm in O(1) time:

```
Data structure:
  hash_map: key → node  (for O(1) lookup)
  doubly_linked_list:   (for O(1) move-to-front and evict-from-tail)

┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐
│ MRU  │◄──│  B   │◄──│  C   │◄──│ LRU  │
│ head │──►│      │──►│      │──►│ tail │
└──────┘   └──────┘   └──────┘   └──────┘
  (most recently used)             (evict next)

get(key):
  1. Look up node in hash_map → O(1)
  2. Move node to head of list → O(1)   (update 4 pointers)
  3. Return value

put(key, value):
  1. If key exists: update value, move to head → O(1)
  2. If cache full: remove tail from list AND hash_map → O(1)
  3. Insert new node at head, add to hash_map → O(1)
```

### LFU vs LRU

- **LRU** (Least Recently Used): evicts the item not accessed for the longest time. Simple, works well for temporal locality.
- **LFU** (Least Frequently Used): evicts the item with the fewest total accesses. Better for Zipf workloads but complex (requires frequency counter + min-heap or bucket structure).
- **Redis default:** LRU (approximated with 5-sample eviction for performance)

**ARC** (Adaptive Replacement Cache): self-tuning between LRU and LFU. Used in ZFS, Solaris.

---

## Cache Placement Strategies

### 1. Cache-Aside (Lazy Loading) — Most Common

```python
async def get_user(user_id: int) -> dict:
    cache_key = f"user:{user_id}"

    # 1. Check cache first
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)                   # cache hit

    # 2. Cache miss — fetch from DB
    user = await db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    if user is None:
        return None

    # 3. Populate cache
    await redis.setex(cache_key, 300, json.dumps(dict(user)))  # TTL: 5 min
    return dict(user)                                # cache miss + populate
```

**Pros:** Simple. Only caches what's actually requested. No startup overhead.
**Cons:** Cache miss adds DB latency. First request after cache cold is slow. Race condition possible (two requests both miss and both write).

### 2. Read-Through

The cache layer itself fetches from the origin on a miss. The application only talks to the cache.

```python
class ReadThroughCache:
    def __init__(self, redis, db, ttl=300):
        self._redis = redis
        self._db = db
        self._ttl = ttl

    async def get(self, key: str, loader_fn):
        value = await self._redis.get(key)
        if value:
            return json.loads(value)
        # Cache handles the load
        value = await loader_fn()
        if value is not None:
            await self._redis.setex(key, self._ttl, json.dumps(value))
        return value

# Usage:
cache = ReadThroughCache(redis, db)
user = await cache.get(f"user:{user_id}", lambda: db.get_user(user_id))
```

### 3. Write-Through

Every write goes to both cache and DB synchronously. Cache is always consistent.

```python
async def update_user(user_id: int, updates: dict) -> dict:
    # Write to DB first (source of truth)
    user = await db.execute(
        "UPDATE users SET name=$1 WHERE id=$2 RETURNING *",
        updates["name"], user_id
    )

    # Write to cache immediately (write-through)
    await redis.setex(f"user:{user_id}", 300, json.dumps(dict(user)))
    return dict(user)
```

**Pros:** Cache always consistent after write. No stale reads.
**Cons:** Every write pays cache write cost. Cache fills with data that may never be read.

### 4. Write-Behind (Write-Back)

Writes go to cache immediately, then asynchronously flush to DB. Highest write throughput.

```python
import asyncio
from collections import defaultdict

class WriteBehindCache:
    def __init__(self, redis, db, flush_interval=1.0):
        self._redis = redis
        self._db = db
        self._dirty: dict[str, dict] = {}  # pending writes
        self._lock = asyncio.Lock()
        asyncio.create_task(self._flush_loop(flush_interval))

    async def set(self, key: str, value: dict):
        await self._redis.set(key, json.dumps(value))
        async with self._lock:
            self._dirty[key] = value  # queue for async flush

    async def _flush_loop(self, interval: float):
        while True:
            await asyncio.sleep(interval)
            async with self._lock:
                pending = dict(self._dirty)
                self._dirty.clear()
            if pending:
                async with self._db.transaction():
                    for key, value in pending.items():
                        await self._db.upsert("users", value)
```

**Pros:** Writes are fast (in-memory only). DB batches many writes.
**Cons:** Data loss if cache crashes before flush. Complex. Risk of inconsistency.

---

## Thundering Herd and Cache Stampede

When a popular key expires, many concurrent requests all see a cache miss simultaneously. All fire DB queries at once — **thundering herd** / **cache stampede**.

### Solution 1: Mutex / Single-Flight

```python
import asyncio

_locks: dict[str, asyncio.Lock] = {}

async def get_with_lock(key: str, loader_fn, ttl=300):
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)

    # Ensure only one coroutine fetches at a time
    if key not in _locks:
        _locks[key] = asyncio.Lock()

    async with _locks[key]:
        # Double-check after acquiring lock (another coroutine may have loaded)
        cached = await redis.get(key)
        if cached:
            return json.loads(cached)

        value = await loader_fn()
        await redis.setex(key, ttl, json.dumps(value))
        return value
```

### Solution 2: Probabilistic Early Revalidation (PER)

Refresh the cache slightly before expiry, based on probability that scales as TTL approaches zero:

```python
import math
import random
import time

async def get_with_per(key: str, loader_fn, ttl=300, beta=1.0):
    """
    Probabilistic Early Revalidation (PER) algorithm.
    Fetch is triggered before expiry with increasing probability.
    beta controls aggressiveness (higher = earlier revalidation).
    """
    entry = await redis.get(f"{key}:per")  # stores {value, computed_at, ttl}
    if entry:
        data = json.loads(entry)
        value = data["value"]
        computed_at = data["computed_at"]
        stored_ttl = data["ttl"]
        elapsed = time.time() - computed_at
        remaining = stored_ttl - elapsed

        # Decide whether to recompute based on probability
        # P(recompute) increases as remaining TTL decreases
        gap = stored_ttl - remaining  # time since creation
        if remaining <= 0 or (-beta * math.log(random.random()) >= remaining):
            # Recompute (in background, return cached value now)
            asyncio.create_task(_refresh(key, loader_fn, ttl))
        return value

    # Cache miss — must fetch
    value = await loader_fn()
    entry = {"value": value, "computed_at": time.time(), "ttl": ttl}
    await redis.setex(f"{key}:per", ttl + 60, json.dumps(entry))
    return value

async def _refresh(key, loader_fn, ttl):
    value = await loader_fn()
    entry = {"value": value, "computed_at": time.time(), "ttl": ttl}
    await redis.setex(f"{key}:per", ttl + 60, json.dumps(entry))
```

### Solution 3: Bloom Filter for Negative Caching

Cache misses for non-existent keys are expensive. A Bloom filter provides O(1) space-efficient check for "definitely not in DB":

```python
from pybloom_live import BloomFilter

# Bloom filter: compact set membership check
# False positive rate: ~1% with 10 bits per element
user_bloom = BloomFilter(capacity=1_000_000, error_rate=0.01)

# On DB load, add all existing IDs to bloom filter
for user_id in db.execute("SELECT id FROM users"):
    user_bloom.add(user_id)

async def get_user_with_bloom(user_id: int):
    # Quickly reject lookups for non-existent users (avoids DB miss)
    if user_id not in user_bloom:
        return None  # Definitely doesn't exist — no DB query needed

    cached = await redis.get(f"user:{user_id}")
    if cached:
        return json.loads(cached)

    user = await db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
    if user:
        await redis.setex(f"user:{user_id}", 300, json.dumps(dict(user)))
    return dict(user) if user else None
```

---

## LRU Implementation (from scratch)

```python
from collections import OrderedDict
from threading import Lock

class LRUCache:
    """
    Thread-safe LRU cache backed by OrderedDict.
    OrderedDict in Python 3.7+ is a doubly linked list + dict.
    """

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._cache: OrderedDict[str, any] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str):
        with self._lock:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)  # mark as recently used
            return self._cache[key]

    def put(self, key: str, value) -> None:
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = value
            else:
                self._cache[key] = value
                if len(self._cache) > self.capacity:
                    self._cache.popitem(last=False)  # evict LRU (first item)

    @property
    def stats(self) -> dict:
        with self._lock:
            return {"size": len(self._cache), "capacity": self.capacity}
```

---

## Cache Invalidation Strategies

### 1. TTL-based (simplest)

```python
await redis.setex(f"user:{user_id}", 300, json.dumps(user))  # expires in 5 minutes
```

**Consistency window:** up to TTL seconds.
**Best for:** semi-static data (user profiles, product catalog).
**Avoid for:** financial data, inventory counts, anything requiring strong consistency.

### 2. Event-driven invalidation

```python
# On DB write, publish an invalidation event
async def update_user(user_id: int, updates: dict):
    user = await db.update_user(user_id, updates)

    # Invalidate cache immediately
    await redis.delete(f"user:{user_id}")

    # Publish event for other cache layers (CDN, other services)
    await pubsub.publish("cache.invalidate", {"entity": "user", "id": user_id})
    return user

# Other services listen and invalidate their local caches
async def on_cache_invalidate(message):
    entity = message["entity"]
    entity_id = message["id"]
    await local_cache.delete(f"{entity}:{entity_id}")
```

### 3. Version-based (cache tags)

```python
async def get_user_posts(user_id: int):
    # Key includes a version number stored separately
    version = await redis.get(f"user:{user_id}:version") or "0"
    cache_key = f"user:{user_id}:posts:v{version}"

    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    posts = await db.fetchall("SELECT * FROM posts WHERE user_id = $1", user_id)
    await redis.setex(cache_key, 3600, json.dumps(posts))
    return posts

async def invalidate_user_posts(user_id: int):
    # Increment version — old keys automatically become orphaned
    await redis.incr(f"user:{user_id}:version")
    # Old cache entries expire via TTL naturally
```

---

## Complexity Analysis

| Operation | LRU Cache | Redis Network Cache | DB query |
|-----------|-----------|---------------------|----------|
| Cache hit | O(1) time, L1/RAM latency | O(1) time, ~0.5ms | N/A |
| Cache miss + load | O(1) + O(query) | O(1) + O(query) | O(query) |
| Cache eviction | O(1) | O(1) | N/A |
| Invalidation (TTL) | O(1) | O(1) | N/A |
| Invalidation (scan) | O(N) | O(N) SCAN | N/A |

Space complexity: O(capacity) for LRU cache.

---

## Benchmark (p50, p99, CPU, Memory)

Setup: 10,000 req/s, Redis 7 (single node), PostgreSQL 15, Redis on same host (0.3ms RTT).

```
┌────────────────────┬────────┬────────┬──────────┬───────────────┐
│ Hit Rate / Config  │  p50   │  p99   │ DB Calls/s│ Redis Mem/GB  │
├────────────────────┼────────┼────────┼──────────┼───────────────┤
│ No cache           │  8ms   │ 22ms   │ 10,000   │ 0             │
│ 50% hit rate       │  4ms   │ 14ms   │  5,000   │ 0.5           │
│ 80% hit rate       │  2ms   │  7ms   │  2,000   │ 1.0           │
│ 95% hit rate       │ 0.8ms  │  2ms   │    500   │ 2.0           │
│ 99% hit rate       │ 0.5ms  │  1ms   │    100   │ 4.0           │
└────────────────────┴────────┴────────┴──────────┴───────────────┘

CPU: Redis uses ~1 CPU core at 100k ops/sec.
     PostgreSQL CPU drops proportionally with hit rate.
```

---

## Observability

```python
from prometheus_client import Counter, Histogram, Gauge

cache_ops = Counter('cache_operations_total', 'Cache ops', ['operation', 'result'])
# result: hit, miss, error, eviction

cache_latency = Histogram('cache_latency_seconds', 'Cache operation latency',
    ['operation'], buckets=[.0001, .0005, .001, .005, .010, .025, .100])

cache_memory = Gauge('cache_memory_bytes', 'Cache memory usage')

def track_cache(operation: str):
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
                if result is None:
                    cache_ops.labels(operation, 'miss').inc()
                else:
                    cache_ops.labels(operation, 'hit').inc()
                return result
            except Exception as e:
                cache_ops.labels(operation, 'error').inc()
                raise
            finally:
                cache_latency.labels(operation).observe(time.monotonic() - start)
        return wrapper
    return decorator

# Derived metrics to alert on:
# hit_rate = rate(cache_ops{result="hit"}[5m]) / rate(cache_ops_total[5m])
# ALERT: hit_rate < 0.80 for 5 minutes
# ALERT: cache_memory_bytes / cache_max_bytes > 0.90 (approaching eviction pressure)
```

### Redis built-in stats

```bash
redis-cli INFO stats | grep -E "keyspace_hits|keyspace_misses|evicted_keys|used_memory"

# keyspace_hits:   10450921
# keyspace_misses:  524051     → hit_rate = 10450921 / (10450921 + 524051) = 95.2%
# evicted_keys:     1203       → eviction happening — consider increasing maxmemory
# used_memory_human: 2.50G
```

---

## Multi-language Implementation

### Python — Redis with connection pool

```python
import redis.asyncio as aioredis
import json
from functools import wraps

redis_pool = aioredis.ConnectionPool.from_url(
    "redis://localhost:6379",
    max_connections=20,
    decode_responses=True
)
redis_client = aioredis.Redis(connection_pool=redis_pool)

def cached(key_fn, ttl=300):
    """Decorator for cache-aside pattern."""
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            key = key_fn(*args, **kwargs)
            hit = await redis_client.get(key)
            if hit:
                return json.loads(hit)
            result = await fn(*args, **kwargs)
            if result is not None:
                await redis_client.setex(key, ttl, json.dumps(result))
            return result
        return wrapper
    return decorator

@cached(key_fn=lambda user_id: f"user:{user_id}", ttl=300)
async def get_user(user_id: int) -> dict:
    return await db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
```

### Go — Redis with go-redis

```go
package cache

import (
    "context"
    "encoding/json"
    "time"

    "github.com/redis/go-redis/v9"
)

type Cache struct {
    client *redis.Client
}

func New(addr string) *Cache {
    return &Cache{
        client: redis.NewClient(&redis.Options{
            Addr:         addr,
            PoolSize:     20,
            MinIdleConns: 5,
            DialTimeout:  5 * time.Second,
            ReadTimeout:  3 * time.Second,
            WriteTimeout: 3 * time.Second,
        }),
    }
}

func (c *Cache) GetOrLoad(ctx context.Context, key string, loader func() (any, error), ttl time.Duration) (any, error) {
    val, err := c.client.Get(ctx, key).Result()
    if err == nil {
        var result any
        json.Unmarshal([]byte(val), &result)
        return result, nil  // cache hit
    }
    if err != redis.Nil {
        return nil, err  // redis error
    }

    // Cache miss — load from source
    value, err := loader()
    if err != nil {
        return nil, err
    }

    data, _ := json.Marshal(value)
    c.client.SetEx(ctx, key, string(data), ttl)
    return value, nil
}
```

### Node.js — ioredis

```javascript
const Redis = require('ioredis');

const redis = new Redis({
    host: 'localhost',
    port: 6379,
    maxRetriesPerRequest: 3,
    retryStrategy: (times) => Math.min(times * 50, 2000),
    lazyConnect: false,
});

class Cache {
    constructor(redis, defaultTTL = 300) {
        this.redis = redis;
        this.defaultTTL = defaultTTL;
    }

    async getOrLoad(key, loader, ttl = this.defaultTTL) {
        const cached = await this.redis.get(key);
        if (cached !== null) {
            return JSON.parse(cached);  // cache hit
        }

        const value = await loader();
        if (value !== null && value !== undefined) {
            await this.redis.setex(key, ttl, JSON.stringify(value));
        }
        return value;
    }

    async invalidate(key) {
        await this.redis.del(key);
    }

    async invalidatePattern(pattern) {
        // Use SCAN to avoid blocking Redis with KEYS
        const pipeline = this.redis.pipeline();
        let cursor = '0';
        do {
            const [nextCursor, keys] = await this.redis.scan(cursor, 'MATCH', pattern, 'COUNT', 100);
            cursor = nextCursor;
            keys.forEach(key => pipeline.del(key));
        } while (cursor !== '0');
        await pipeline.exec();
    }
}

const cache = new Cache(redis);
const getUser = (userId) =>
    cache.getOrLoad(`user:${userId}`, () => db.query('SELECT * FROM users WHERE id = $1', [userId]));
```

---

## Trade-offs

| Strategy | Consistency | Complexity | Write Cost | Best For |
|----------|-------------|------------|------------|----------|
| Cache-Aside | Eventual (TTL) | Low | None | Read-heavy, tolerable staleness |
| Read-Through | Eventual (TTL) | Medium | None | Same as above, cleaner code |
| Write-Through | Strong | Medium | 2× write | Write + read hot data |
| Write-Behind | Eventual | High | Near-zero | Write-heavy, tolerable loss risk |

**Cache size vs hit rate trade-off:**

```
Working set size estimate (for Zipf with s=1):
  Cache 10% of items → ~63% hit rate
  Cache 20% of items → ~74% hit rate
  Cache 50% of items → ~88% hit rate

Diminishing returns: doubling cache size increases hit rate by decreasing amounts.
```

---

## Failure Modes

**1. Cold start / cache stampede:**
After deploy, cache is empty. All requests hit DB simultaneously. DB CPU spikes, queries slow down, connections exhaust. Write-through or pre-warming at deploy time prevents this.

```python
async def warm_cache():
    """Pre-populate cache with hot data before accepting traffic."""
    hot_user_ids = await db.fetchall(
        "SELECT id FROM users ORDER BY last_active DESC LIMIT 10000"
    )
    for user_id in hot_user_ids:
        user = await db.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        await redis.setex(f"user:{user_id}", 3600, json.dumps(dict(user)))
```

**2. Inconsistency window:**
TTL-based caching means stale reads for TTL seconds after a write. For financial data (balances, inventory), stale reads are dangerous.

Mitigation: Event-driven invalidation. On write, immediately `DEL` the key.

**3. Memory pressure and eviction:**
When Redis hits `maxmemory`, it begins evicting keys. If your eviction policy is `allkeys-lru`, hot keys may be evicted. If `noeviction`, new writes fail.

```bash
# Set eviction policy
redis-cli CONFIG SET maxmemory-policy allkeys-lru
redis-cli CONFIG SET maxmemory 2gb
```

**4. Cache key collision:**
Two different data types sharing a key namespace causes data corruption.

```python
# BAD: user:1 could be either a User or a UserPreference
cache.set("user:1", user_data)
cache.set("user:1", user_prefs)  # overwrites!

# GOOD: namespace by entity type
cache.set("user:profile:1", user_data)
cache.set("user:prefs:1", user_prefs)
```

**5. Serialization mismatch:**
Storing JSON-serialized objects and deserializing into the wrong type causes silent data bugs.

---

## When NOT to Use

**Strongly consistent data:**
Bank balances, inventory counts, distributed locks — these require the DB as the single source of truth. Caching introduces inconsistency windows that cause incorrect reads.

**Highly write-heavy data:**
If a key is written 100× per second and read 10× per second, caching increases write overhead with near-zero hit rate. Cache read-heavy paths only.

**Large objects:**
Caching 100MB objects in Redis wastes memory. Consider Content-Addressable Storage (S3/GCS) with a CDN for large blobs.

**Personal/sensitive data at rest:**
Caching PII in Redis adds attack surface. Ensure Redis has auth, TLS, and network isolation. Consider not caching sensitive fields.

---

## Lab

The lab for this module is embedded in the benchmarks directory:
`../../benchmarks/06-cache-vs-no-cache/README.md`

It demonstrates the hit rate vs latency trade-off with a simulated Zipf workload.

---

## Key Takeaways

1. **The cache equation:** `effective_latency = hit_rate × cache_latency + (1 - hit_rate) × origin_latency`. Hit rate is the dominant variable.
2. **Zipf distribution** makes caches work: 20% of items get 80% of reads. Cache that 20%.
3. **LRU is O(1)** for both get and put using doubly linked list + hash map. Redis approximates it with 5-sample random eviction.
4. **Cache placement:** Cache-Aside for most cases. Write-Through for write+read hot data. Write-Behind for extreme write throughput.
5. **Thundering herd** on expiry is a real production failure mode. Use single-flight mutex or probabilistic early revalidation (PER).
6. **Invalidation is hard.** TTL is simple but eventually consistent. Event-driven is complex but correct.
7. **Bloom filters** eliminate DB queries for non-existent keys — essential for attack mitigation (random key probing).
8. **Observe:** track hit rate, eviction rate, memory usage. Alert on hit_rate < 80% and memory > 90%.
