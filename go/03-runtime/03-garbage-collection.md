---
title: Garbage Collection — The Concurrent Tri-Color Collector
description: How Go's concurrent mark-sweep GC works, the write barrier, GOGC and GOMEMLIMIT, why Go is non-generational and non-compacting, and how to tune and diagnose GC in production.
tags: ["go", "gc", "garbage-collection", "memory", "performance", "gogc"]
status: published
access: gated
publishedAt: 2026-07-08
---

# Garbage Collection — The Concurrent Tri-Color Collector

## Learning objectives

You will understand how Go's garbage collector works (concurrent tri-color mark-sweep), what the write barrier does and why it exists, how `GOGC` and `GOMEMLIMIT` control it, why Go deliberately chose a non-generational, non-compacting design, and how to diagnose and tune GC behavior in a live service.

## Why this matters

The GC is the thing you stop thinking about until it shows up as latency. Every Go service has a GC, and under memory pressure or high allocation rates it manifests as p99 spikes, CPU spent in `runtime.gcBgMarkWorker`, or — worse — an OOM kill because you didn't set a memory limit. Understanding the collector lets you read GC traces, set the two knobs correctly, and — most importantly — reduce allocations so the GC has less to do, which is almost always the real fix.

## The mechanics: concurrent tri-color mark-sweep

Go's GC is a **tri-color, concurrent, mark-sweep** collector. Break that down:

- **Mark-sweep:** it finds live objects (mark), then reclaims everything unmarked (sweep). It does *not* move objects (non-compacting) and does not separate young from old (non-generational).
- **Concurrent:** marking runs *at the same time as your program*, on background worker goroutines, so the program mostly keeps running during a GC cycle. There are two brief **stop-the-world (STW)** pauses per cycle, each typically well under a millisecond.
- **Tri-color:** the marking algorithm colors objects white (candidate for collection), grey (reachable but children not yet scanned), or black (reachable, children scanned).

```
Tri-color invariant: no BLACK object points directly to a WHITE object.

  roots ──► [BLACK] ──► [BLACK] ──► [GREY] ──► [WHITE] ──► [WHITE]
                                       │  (grey = frontier being scanned)
  Marking: pop a grey object, blacken it, grey its white children.
  Done when no grey objects remain. Everything still WHITE is garbage → swept.
```

A GC cycle:

```
1. STW #1 (brief): enable the write barrier, scan stack roots setup.
2. Concurrent mark: background workers walk the object graph, greying then
   blackening reachable objects, WHILE the program runs. Mark assists: an
   allocating goroutine helps mark, proportional to how much it allocates
   (so heavy allocators pay their share and can't outrun the collector).
3. STW #2 (brief): mark termination — finish, disable the write barrier.
4. Concurrent sweep: reclaim white objects lazily as memory is requested.
```

## The write barrier — why it exists

Here is the subtle part. Marking runs *concurrently* with your program, so the object graph is **changing while the GC walks it**. Consider: the GC has blackened object A (won't rescan it). Your program then stores a pointer to a white object W into A, and deletes the only other reference to W. Now A (black) points to W (white), and W will be wrongly collected — a **use-after-free**, the cardinal GC bug.

The **write barrier** prevents this. It is a small piece of code the compiler inserts around pointer writes *while the GC is active*: when the program writes a pointer, the barrier shades the involved object grey so it won't be missed. Go uses a **hybrid write barrier** (since Go 1.8, combining Dijkstra-style and Yuasa-style) that maintains the invariant while requiring only a lightweight barrier and eliminating the need to rescan stacks. The cost: every pointer write during a GC cycle runs a few extra instructions. This is why Go code that writes lots of pointers during heavy GC can be slightly slower — the barrier is on.

## Why non-generational and non-compacting

This surprises engineers coming from Java, whose GCs are generational and compacting. Go chose neither, on purpose:

- **Non-compacting** (objects never move): so pointers are stable, interior pointers work, and `unsafe`/CGO/stack-sharing are simpler. The cost is potential heap fragmentation, which Go's size-class allocator (tcmalloc-style, segregated free lists) mitigates well.
- **Non-generational** (no young/old separation): the "generational hypothesis" (most objects die young) is somewhat *defeated in advance* by Go's escape analysis — many short-lived objects never reach the heap at all; they stay on the stack. So a generational GC would buy less than it does in Java, at the cost of more complexity and write-barrier overhead. The Go team judged the trade not worth it. (An experimental generational-ish design and, in Go 1.25, an experimental "Green Tea" GC continue to explore this space.)

The engineering takeaway: **Go's answer to GC pressure is to not allocate in the first place** (stack allocation via escape analysis, pooling, preallocation), rather than to make collection of young objects cheaper. That is why the previous chapters obsess over allocations.

## The two knobs: GOGC and GOMEMLIMIT

You control the GC with exactly two settings. Know both cold:

**`GOGC`** (default 100) sets the **heap growth target**: the GC triggers the next cycle when the heap has grown by `GOGC`% since the last cycle's live set. `GOGC=100` means "collect when the heap doubles relative to live data." Trade-off:

```
GOGC=100 (default): balance CPU vs memory
GOGC=200:  fewer GCs, more CPU headroom, ~more memory used
GOGC=50:   more frequent GCs, less memory, more CPU spent collecting
GOGC=off:  disable GC entirely (only for short-lived batch jobs)
```

Raising `GOGC` trades memory for CPU (fewer collections); lowering it trades CPU for memory. It is a *throughput vs footprint* dial.

**`GOMEMLIMIT`** (added in **Go 1.19**) sets a **soft memory limit**. The GC becomes more aggressive as the heap approaches the limit, running more often to stay under it. This is the fix for the classic Go-in-a-container failure mode: with only `GOGC`, a spike in live data could push the heap past the container's memory limit and get the process **OOM-killed** before the next scheduled GC. `GOMEMLIMIT` lets the runtime trade CPU to *avoid* the OOM. The recommended production configuration:

```
GOMEMLIMIT = ~90-95% of the container memory limit   # leave headroom for non-heap
GOGC       = 100 (default), OR set high/off and rely on GOMEMLIMIT as the backstop
```

The modern pattern in memory-constrained deployments is "set `GOMEMLIMIT` to your real limit and let it govern," because it responds to *actual* memory pressure rather than a fixed growth ratio. It is a *soft* limit — the runtime will exceed it rather than stall forever if it truly cannot free memory, so it prevents most but not pathological OOMs.

## Production engineering: diagnosing GC

```
GODEBUG=gctrace=1 ./server
# one line per GC cycle:
gc 42 @7.213s 2%: 0.018+3.2+0.021 ms clock, 0.14+1.1/3.0/0.9+0.17 ms cpu,
   118->121->59 MB, 120 MB goal, 8 P
```

Decode the important fields: `2%` is the fraction of total CPU spent in GC (watch this — high single digits or more means GC is a real cost); the `ms clock` triplet is STW-mark + concurrent-mark + STW-termination (the two STW numbers are your pause times — should be sub-ms); `118->121->59 MB` is heap-before → heap-at-mark-end → live-after; `120 MB goal` is the trigger target computed from `GOGC`/`GOMEMLIMIT`.

Signals and their meanings:

- **GC CPU % high** (say >10%): allocation rate is too high. Fix by reducing allocations, not by tuning knobs. Profile with `go tool pprof` on the heap profile.
- **STW pauses > 1 ms**: rare on modern Go; usually means enormous heaps or too many goroutines to scan. Investigate stack scan cost.
- **Heap climbs, live set flat**: a leak (often goroutine or map — see prior chapters), not a GC problem.
- **OOM kills**: set `GOMEMLIMIT`. This is the first move for any container that gets OOM-killed.

The heap profile is the workhorse: `go tool pprof -http=:8080 http://svc/debug/pprof/heap` shows exactly which call sites allocate the most, which is where you cut GC work at the source.

## Common mistakes

- Tuning `GOGC` to fix latency when the real problem is a high allocation rate — reduce allocations first.
- Not setting `GOMEMLIMIT` in containers, then getting OOM-killed under load spikes.
- Confusing a memory *leak* (live set grows) with GC *pressure* (churn of short-lived objects). They have opposite fixes.
- Setting `GOGC=off` in a long-running service — the heap grows unbounded.
- Assuming the GC compacts/defrags memory — it doesn't; a fragmented heap stays fragmented.

## Best practices

- **Reduce allocations** as the primary GC strategy: stack allocation (escape analysis), `sync.Pool`, preallocation, avoiding `interface{}`/`fmt` on hot paths. Fewer allocations = less GC, full stop.
- Set `GOMEMLIMIT` to ~90–95% of the container limit in memory-constrained deployments.
- Leave `GOGC=100` unless a benchmark shows raising it (more memory available → less GC CPU) or lowering it (tight memory) helps.
- Export and alert on GC CPU fraction and heap size; keep a heap profile endpoint (`/debug/pprof/heap`) available.

## Performance analysis

The dominant lever is allocation rate, and it is enormous. A service allocating 2 GB/s of short-lived objects will spend real CPU in GC and see latency jitter; the *same logic* rewritten to reuse buffers (`sync.Pool`, `s[:0]` reset) and keep values on the stack can drop to near-zero steady-state allocation and effectively disappear from GC traces. In practice, moving a hot handler from "allocates per request" to "zero-alloc" often cuts GC CPU from several percent to a fraction of a percent and removes a whole class of p99 spikes — a bigger win than any knob. Measure allocation rate with `gctrace` and `-benchmem`; that number, not the GC settings, is what you optimize.

## Production case study

A JSON-heavy API in a 512 MB container was periodically OOM-killed under traffic spikes. Two independent fixes applied. First, immediate: setting `GOMEMLIMIT=460MiB` made the GC ramp up as the heap approached the limit, trading a few percent CPU to keep the process alive — the OOM kills stopped. Second, structural: the heap profile showed most allocations came from decoding and re-encoding JSON per request into fresh buffers and boxing values into `interface{}`. Introducing `sync.Pool`ed buffers, decoding into typed structs instead of `map[string]any`, and reusing encoder buffers cut steady-state allocation by ~80%, which in turn dropped GC frequency and CPU so far that the memory limit was rarely approached at all. The order is the lesson: **`GOMEMLIMIT` to stop the bleeding, reduce allocations to cure the disease.**

## Exercises

1. Run a service with `GODEBUG=gctrace=1` under load and interpret one line completely: pause times, GC CPU %, and the heap triple.
2. Write a program that allocates heavily in a loop; measure GC CPU. Introduce a `sync.Pool` and measure again. Quantify the reduction.
3. In a memory-limited container (or with `GOMEMLIMIT` set low), induce a live-set spike and observe the GC ramp up to avoid OOM. Then remove `GOMEMLIMIT` and watch it get killed.
4. Take a heap profile of an allocating program and identify the top three allocating call sites. Eliminate one via stack allocation or preallocation and confirm with a follow-up profile.

## Summary

- Go's GC is **concurrent tri-color mark-sweep**: marking runs alongside your program with two brief (sub-ms) STW pauses; it is **non-generational** and **non-compacting** by deliberate choice.
- The **write barrier** (hybrid, since 1.8) preserves correctness during concurrent marking by shading objects on pointer writes; it costs a few instructions per pointer write while GC is active.
- Two knobs: **`GOGC`** (heap growth target, throughput vs footprint) and **`GOMEMLIMIT`** (soft memory cap, the fix for container OOM kills). Set `GOMEMLIMIT` to ~90–95% of the container limit.
- Go's real GC strategy is **allocate less** (escape analysis, pooling, preallocation), because it lacks a generational shortcut. Reducing allocation rate beats tuning knobs.
- Diagnose with `GODEBUG=gctrace=1` and the heap profile; distinguish GC *pressure* (churn) from a *leak* (growing live set).

Next → [Escape analysis: reading and controlling where memory lives](/backend-guide/go/03-runtime/04-escape-analysis)
