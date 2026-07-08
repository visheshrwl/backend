---
title: Map Internals — Buckets, Load Factor, and Swiss Tables
description: How Go's map is built, why it grows incrementally, why iteration order is randomized, the concurrency rule that panics your service, and the Go 1.24 Swiss-table rewrite.
tags: ["go", "maps", "hashmap", "swiss-tables", "performance"]
status: published
access: gated
publishedAt: 2026-07-08
---

# Map Internals — Buckets, Load Factor, and Swiss Tables

## Learning objectives

You will understand how a Go map is laid out, when and how it grows, why iteration order is deliberately random, why concurrent access panics rather than corrupts, when a map is the wrong choice, and what changed in the Go 1.24 Swiss-table rewrite.

## Why this matters

Maps are everywhere in production Go — routing tables, caches, dedup sets, request metadata. Two things about them bite teams repeatedly: **concurrent writes crash the process** (by design), and **maps are a major source of GC pressure and memory bloat** because they never shrink. Knowing the internals tells you when to reach for a map, when to reach for a slice or a `sync.Map`, and how to keep a hot map from dominating your heap.

## The mechanics: the classic bucketed hash map

Through Go 1.23, a map is a `hmap` header pointing at an array of **buckets**, each holding up to **8 key/value pairs** (`bmap`):

```go
// simplified from src/runtime/map.go (pre-1.24)
type hmap struct {
    count     int            // len(m) — O(1)
    flags     uint8
    B         uint8          // there are 2^B buckets
    buckets   unsafe.Pointer // array of 2^B bmap
    oldbuckets unsafe.Pointer // non-nil during incremental growth
    // ...
}
type bmap struct {
    tophash [8]uint8 // top byte of each slot's hash (fast reject)
    // followed by 8 keys, then 8 values, then an overflow *bmap
}
```

Lookup of key `k`:

```
1. h = hash(k)
2. bucket = h & (2^B - 1)          # low bits pick the bucket
3. top = h >> 56                    # high byte is the "tophash"
4. scan the 8 tophash bytes; on a match, compare the full key
5. not found in bucket? follow the overflow pointer chain
```

```
hmap ──► buckets[2^B]
          ┌─────────────────────────────────────────────┐
 bucket 0 │ tophash[8] │ k0 k1 … k7 │ v0 v1 … v7 │ ovf ──┼──► overflow bmap
          ├─────────────────────────────────────────────┤
 bucket 1 │ tophash[8] │ …                          ovf  │
          └─────────────────────────────────────────────┘
```

The `tophash` array is the performance trick: instead of comparing full keys, the map first compares one byte per slot to reject non-matches cheaply, only doing a full key comparison on a byte match. Keys and values are stored in separate runs (all keys, then all values) rather than interleaved, to avoid padding waste from alignment.

### Growth and the load factor

When the map gets too full it grows. The trigger is a **load factor of 6.5** average entries per bucket, or too many overflow buckets. Growth **doubles** the bucket count (`B++`) and then **evacuates incrementally**: the `oldbuckets` pointer stays live, and each insert/delete migrates a couple of old buckets into the new array. This spreads the rehash cost across many operations instead of one giant O(n) stall — the same "no stop-the-world" philosophy as the GC.

Two consequences that reach production:

1. A map **never shrinks.** Delete every key and the bucket array stays allocated at its high-water mark. A map that once held 10M entries holds ~10M buckets' worth of memory forever. To reclaim it you must build a fresh map and drop the old one.
2. Growth means **pointers into map storage are never given to you** — `&m[k]` does not compile — because evacuation moves entries. This is why you cannot take the address of a map element, and why "modify a struct value in a map" requires read-modify-write (`v := m[k]; v.X++; m[k] = v`).

## Why iteration order is randomized

`for k := range m` visits keys in a **deliberately randomized** order — the runtime picks a random starting bucket and offset each time. This is not incidental; it is enforced so that engineers cannot accidentally depend on iteration order, which is an implementation detail that changes across growth and Go versions. If you need order, sort the keys explicitly:

```go
keys := make([]string, 0, len(m))
for k := range m { keys = append(keys, k) }
sort.Strings(keys)
for _, k := range keys { use(m[k]) }
```

Relying on map order is a classic bug that "works on my machine" and fails in CI or production. The randomization is the language protecting you from it.

## The concurrency rule that crashes services

Maps are **not safe for concurrent use** when at least one goroutine writes. The runtime actively **detects** concurrent map writes and **panics the process**:

```
fatal error: concurrent map writes
```

This is intentional and unrecoverable — it is a `fatal error`, not a `panic` you can `recover`. The rationale: a data race on a map could corrupt the bucket structure and cause silent, undebuggable failures, so the runtime chooses a loud crash over quiet corruption. Your options for concurrent maps:

- **`sync.RWMutex` around a plain map** — best for most cases; simple and fast when writes are not extreme.
- **`sync.Map`** — a specialized concurrent map optimized for two narrow cases: (a) keys written once and read many times, or (b) disjoint key sets per goroutine. It is *slower* than a mutex+map for general read/write workloads, so use it only when its access pattern matches.
- **Sharding** — N mutex-guarded maps keyed by `hash(k) % N` to spread lock contention, for very high write concurrency.

The review reflex: any map touched by more than one goroutine must have a visible synchronization story, or it is a latent `fatal error` under load.

## The Go 1.24 Swiss-table rewrite

As of **Go 1.24 (Feb 2025)**, the map implementation was replaced with one based on **Swiss Tables** (the design popularized by Abseil's `flat_hash_map`). The observable semantics are identical — same API, same randomized iteration, same concurrency rule — but the internals changed and typical maps got faster (notably large-map lookups) with lower memory overhead. Key differences:

- Storage is organized into **groups of 8 slots** with a **control word** (one byte of hash metadata per slot) that can be scanned with SIMD-style bit tricks, so the probe examines 8 slots' metadata in parallel rather than one tophash at a time.
- It uses open addressing within groups rather than per-bucket overflow chains, improving cache behavior.

What you do differently in practice: **nothing** — but you should know that map performance characteristics improved in 1.24 and that old blog posts describing `bmap`/overflow buckets describe the pre-1.24 layout. When you profile a map-heavy service, the numbers you measure on 1.24+ reflect the Swiss-table implementation.

## Common mistakes

- **Concurrent access without a lock** → `fatal error: concurrent map writes`. The most severe and most common map bug.
- **Depending on iteration order** — it is randomized on purpose.
- **Expecting memory to be freed after deletes** — maps never shrink; rebuild to reclaim.
- **`&m[k]`** — does not compile; map values are not addressable. Read-modify-write struct values.
- **Reaching for `sync.Map` by default** — it is slower than `RWMutex`+map for general workloads. Match it to its intended pattern or don't use it.
- **Using a map where a slice would do** — for small, fixed, or densely-integer-keyed sets, a slice is faster and lighter (see below).

## Best practices

- Preallocate with `make(map[K]V, hint)` when you know the rough size — it sets `B` up front and avoids incremental growth churn.
- Guard shared maps with `sync.RWMutex`; reach for `sync.Map` only for read-mostly or disjoint-key patterns; shard for extreme write concurrency.
- To reclaim memory from a shrunk map, assign a fresh map and let the old one be collected.
- For small integer-keyed lookups, benchmark a slice — linear scan of a handful of elements beats hashing and is cache-friendly.

## Performance analysis

```
$ go test -bench=Lookup -benchmem
BenchmarkMapLookup_1M-8       50000000    24.1 ns/op    0 allocs/op
BenchmarkSliceScan_8-8       300000000     3.9 ns/op    0 allocs/op   # 8-elem slice
BenchmarkSyncMapLookup-8     20000000     58.0 ns/op    0 allocs/op
```

Two lessons. First, for tiny collections a linear slice scan (~4 ns) beats a map lookup (~24 ns) because it avoids hashing and stays in one cache line — "use a map" is not automatic. Second, `sync.Map` read is ~2.5× a plain map read here; it earns its keep only when it removes lock contention that a mutex would otherwise cause, which this single-goroutine benchmark cannot show. Always benchmark `sync.Map` against `RWMutex`+map *under your real concurrency*, not in isolation.

## Production case study

A metrics service kept a `map[string]*counter` of active time series, adding and removing series as targets came and went. Two problems emerged. First, an occasional `fatal error: concurrent map writes` crash under scrape bursts — the map was read by the HTTP handler while a background reconciler wrote it, with no lock. The fix was an `RWMutex`. Second, memory grew monotonically even as the number of live series stayed flat, because churned-out keys were deleted but the map never shrank; the bucket array sat at the all-time-high series count. The fix was to periodically rebuild the map from the live set when the delete ratio crossed a threshold. Both bugs are direct consequences of the two map facts every engineer should hold: **concurrent writes crash**, and **maps never shrink.**

## Exercises

1. Write a map with 1M entries, delete all of them, and use `runtime.ReadMemStats` to show the memory is not reclaimed. Then rebuild and show it is.
2. Trigger `fatal error: concurrent map writes` with two goroutines writing the same map. Fix it with `RWMutex`, then benchmark against `sync.Map` under 90% reads and under 50% reads — find the crossover.
3. Print `range` order of a small map across ten program runs and confirm it changes. Write the deterministic sorted-key version.
4. For a `map[uint8]T` (≤256 keys), benchmark it against a `[256]T` array. Explain the result in terms of hashing and cache lines.

## Summary

- A Go map is a hashed, bucketed structure: pre-1.24, arrays of 8-slot `bmap` buckets with `tophash` fast-reject and overflow chains; **Go 1.24+ uses Swiss tables** (group control words, open addressing) — same semantics, better speed and memory.
- It grows by **doubling** at load factor 6.5 and **evacuates incrementally**; it **never shrinks** — rebuild to reclaim memory.
- Iteration order is **randomized on purpose**; sort keys if you need order.
- Concurrent writes are a **`fatal error`**, not a recoverable panic. Guard shared maps with `RWMutex`, use `sync.Map` only for its narrow patterns, and shard for extreme write load.
- Map values are not addressable (`&m[k]` fails); read-modify-write struct values. For tiny collections, a slice can beat a map.

Next → [Interface representation: itab, eface, and dynamic dispatch](/backend-guide/go/02-interfaces-and-methods/01-interface-representation)
