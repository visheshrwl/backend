# Memory Management

## Problem

You call `malloc(32)`. You get back a pointer. You think you just "got 32 bytes of memory." You did not. You invoked one of the most sophisticated data structures in your entire process — an allocator that is, right now, juggling free lists segregated by size, per-thread arenas to avoid lock contention, metadata headers tucked invisibly beside your bytes, and a running negotiation with the kernel over how much address space to hold. The 32 bytes are almost the least interesting thing that happened. And the moment you call it in a hot loop, in a multithreaded server, on a long-running process, every one of those hidden mechanisms becomes *your* performance and *your* memory footprint — whether you ever learned their names or not.

Here's the chapter-05 boundary, because it matters and people blur it. **Virtual memory (chapter 05) is the kernel handing out memory in 4 KB pages.** But your program doesn't allocate in pages — it allocates a 16-byte struct here, a 200-byte string there, a 3 MB buffer occasionally. Something has to sit between "the kernel gives me pages" and "my code wants 16 bytes": something that grabs big chunks of pages from the kernel and chops them into the small, varied pieces your program asks for, tracks which pieces are free, and hands them back out. That something is the **allocator** (malloc/free, and the runtime's heap), and it lives in *user space*, inside your process. Memory management is two layers: the kernel's page-level machinery underneath, and the allocator's byte-level machinery on top. This chapter is the top layer — and the top layer is where the bugs that ruin long-running services live.

Because here's what nobody warns you about: the allocator's failures are *slow and cumulative*, not loud and immediate. A service runs fine for hours, then its memory creeps up and up and never comes back down — not a leak in the "forgot to free" sense, but **fragmentation**, free memory shattered into pieces too small to reuse. A multithreaded service scales beautifully to 8 cores and then *stops* scaling — because every thread is fighting for the same allocator lock. A request that should be 2 ms occasionally spikes to 50 ms — because a garbage collector decided this was the moment to walk the entire heap. None of these show up in a unit test. All of them are memory management, and understanding the allocator is the difference between treating them as gremlins and treating them as physics.

## Why It Matters (Latency, Throughput, Cost)

**The allocator is a lock, and locks don't scale.** A naïve single global heap means every `malloc` and `free` across every thread contends on one lock. On one core, invisible. On 32 cores running an allocation-heavy server, that lock becomes the bottleneck and your throughput flatlines no matter how many cores you add — the textbook "we scaled the hardware and nothing got faster." This single problem is *why* modern allocators (jemalloc, tcmalloc, mimalloc) exist: their headline feature is **per-thread (or per-CPU) caches and arenas** so that the common case — a thread allocating and freeing its own objects — touches no shared lock at all. Choosing jemalloc over the default glibc malloc has, for real services, improved multicore throughput by double-digit percentages with a one-line `LD_PRELOAD` change. The allocator is infrastructure, and the default isn't always the right one.

**Fragmentation is memory you paid for and can't use.** Your process's RSS (resident memory) can be 4 GB while your program is only "using" 2 GB of live objects — the other 2 GB is free space trapped between live allocations, in chunks too small or too awkwardly placed to satisfy new requests. You're paying for 4 GB of RAM (or hitting your 4 GB container limit and getting OOM-killed, chapter 05) to do 2 GB of work. Worse, allocators rarely return freed memory to the OS promptly — a heap that grew to handle a traffic spike often *stays* grown. Fragmentation is the reason "just restart it every night" is a real, widely-deployed operational practice for services in fragmentation-prone languages: the restart is a fragmentation reset.

**Allocation in the hot path is death by a thousand cuts.** A single `malloc` is fast — tens of nanoseconds for the common cached case. But a request handler that allocates a hundred small temporary objects pays that cost a hundred times, plus the cache misses of touching a hundred scattered heap locations (arrays chapter), plus, in managed languages, the GC pressure of creating a hundred objects the collector must later trace and free. This is why high-performance systems obsess over **allocation-free hot paths**: object pools, arena/bump allocators, `sync.Pool` in Go, pre-sized buffers, reusing slices. Not because one allocation is slow, but because *eliminating allocation from the hot path* removes a whole category of latency, cache, and GC cost at once.

## Mental Model

Picture the allocator as a **shopkeeper standing between your program and the kernel's warehouse.**

```
   your code            the allocator (user space)              the kernel
   ─────────            ──────────────────────────              ──────────
   malloc(16) ───►  ┌─────────────────────────────┐
   malloc(200) ──►  │  free lists by size class:   │   when it runs low,
   free(ptr) ────►  │   [16][16][16] ...           │   asks the kernel for
   malloc(16) ───►  │   [32][32] ...               │   a big slab of PAGES
                    │   [256] ...                  │ ◄──── mmap / brk ────► [4KB][4KB]...
                    │  per-thread caches (no lock) │                       (chapter 05)
                    └─────────────────────────────┘
        small, varied, frequent                    big, page-aligned, rare
```

The shopkeeper's whole job is **impedance matching** between two mismatched worlds. Your program wants small, oddly-sized, frequent allocations with instant turnaround. The kernel deals only in big, page-aligned, relatively expensive chunks (a syscall each). The allocator buys in bulk from the kernel (a slab of pages) and retails in small pieces to you, keeping an inventory (free lists) of what it has lying around so it can satisfy most requests *without bothering the kernel at all.* A `malloc` that's satisfied from the allocator's existing inventory is a fast user-space operation; only when inventory runs out does it make the slow trip to the kernel for more pages.

Two consequences fall out of this picture immediately, and they're the heart of the chapter:

1. **`free` usually returns memory to the *shopkeeper*, not the *warehouse*.** When you free, the allocator typically just puts the chunk back on its free list to hand out again — it does *not* immediately give those pages back to the kernel. That's why your process's memory footprint can stay high after you've freed everything: the allocator is hoarding inventory for next time. This is by design (returning pages is expensive), and it's the root of "why didn't my RSS go down."
2. **The free list's organization determines fragmentation.** If the shopkeeper sorts inventory by size (size classes), a request for a 16-byte chunk is instantly matched from the 16-byte bin. If inventory is one disorganized pile, the shopkeeper may have plenty of total free space but no single piece the right size — fragmentation. *How the allocator organizes free memory is the whole game,* and it's what Layer 2 is about.

## Underlying Theory

### Layer 1 — Where the heap comes from: brk and mmap

Before the allocator can retail memory, it has to wholesale it from the kernel, and it has exactly two doors. The first is **`brk`/`sbrk`** — moving the "program break," the top of the contiguous heap region. Bumping the break upward by a megabyte gives the allocator a megabyte of fresh address space to carve up. It's cheap and simple but *strictly stack-like*: you can only easily return memory by lowering the break, which only works if the top of the heap is free — and it usually isn't, because some still-live allocation is sitting up there pinning it. The second door is **`mmap`** — asking the kernel for an independent region of pages anywhere in the address space, which can be `munmap`'d back independently. Allocators use `brk` for the main small-object heap (fast, contiguous) and `mmap` for large allocations (so a single big free can actually return the memory to the OS).

This is *already* the explanation for a classic mystery: free a giant 500 MB buffer and watch RSS drop immediately, but free ten thousand small objects and watch RSS not budge. The big one was `mmap`'d and got `munmap`'d back to the kernel; the small ones went onto `brk`-heap free lists, trapped behind whatever's above them, returned to the shopkeeper but never to the warehouse. Same `free()` call, opposite effect on your memory graph, entirely because of which door the memory came through.

### Layer 2 — Size classes and free lists: how to not fragment

The naïve allocator keeps one list of free chunks of all sizes; to satisfy a request it searches for a chunk big enough (first-fit, best-fit). This is slow (searching) and fragments badly (you split a big chunk to serve a small request, leaving an awkward remainder, over and over until the heap is confetti). Every serious modern allocator instead uses **size classes**: it pre-defines a set of fixed sizes (say 8, 16, 32, 48, 64, 80, ..., then larger geometric steps) and keeps a *separate free list per class.*

```
request 30 bytes ─► round up to size class 32 ─► pop a chunk off the 32-byte free list
                                                  (O(1), no search, no splitting)
free a 32-byte chunk ─► push it back onto the 32-byte free list (O(1))
```

The payoff is enormous and twofold. **Speed:** allocation and free become O(1) list operations — no searching, no splitting — because every chunk in a class is interchangeable. **Fragmentation control:** chunks of the same size are perfectly reusable for each other, so freeing a 32-byte object always produces something a future 32-byte request can use exactly. The cost is **internal fragmentation** — your 30-byte request consumes a 32-byte slot, wasting 2 bytes — a small, *bounded* waste traded for eliminating the unbounded *external* fragmentation of the naïve approach. This size-class idea is the structural heart of jemalloc, tcmalloc, mimalloc, and the kernel's own **slab allocator** (which manages kernel objects like `task_struct`s and inodes the same way). It's also, not coincidentally, the same idea as a hash table's load-factor trade and a dynamic array's geometric growth: *waste a little bounded space to make the common operation O(1).*

### Layer 3 — Arenas and per-thread caches: beating the lock

Size classes make a single-threaded allocator fast. But on a multicore server, the bottleneck moves from *computation* to *contention* — if all threads share one set of free lists, every `malloc`/`free` must lock them, and that lock serializes your whole program (chapter 04's contention story). The fix that defines modern allocators: **don't share.**

- **Per-thread (or per-CPU) caches:** each thread keeps its own small stash of free chunks per size class. A thread allocating and freeing its own short-lived objects hits its *private* cache — **zero locking, zero contention**, just a thread-local list pop. This is the fast path, and for most server workloads it's the overwhelming majority of allocations.
- **Arenas:** the heap is split into several independent arenas, each with its own lock, and threads are assigned to arenas. Even when a thread must touch shared structure (its cache is empty and needs refilling), it contends only with the few other threads on its arena, not all threads globally. jemalloc's name is literally "je" (Jason Evans) + "malloc," and arena-based design is its signature.

This is why swapping glibc malloc for jemalloc or tcmalloc can transform a service's multicore scaling: the default allocator's locking becomes the ceiling, and the arena/thread-cache design lifts it. When you read "we improved throughput 30% by switching to jemalloc," *this layer* is the reason. It's also why the allocator is one of the few pieces of "infrastructure" you can swap underneath an unchanged program and see a real win.

### Layer 4 — Fragmentation: the slow-motion failure

We've touched fragmentation; now name its two species precisely, because they have different cures. **Internal fragmentation** is waste *inside* an allocated chunk — the 2 bytes lost when 30 bytes rounds up to a 32-byte size class. It's bounded (at most the gap between adjacent size classes) and it's the price of O(1) allocation; you accept it. **External fragmentation** is free memory *between* live chunks, in pieces unusable for current requests — total free space is plenty, but it's shattered. This is the dangerous one: unbounded, cumulative, and the cause of "RSS climbs forever."

The deep reason external fragmentation builds up: **objects with different lifetimes get interleaved in memory.** Allocate a long-lived object, then a short-lived one right after it, free the short-lived one — now there's a hole, but it's pinned next to a long-lived object that won't move, so it can only be reused by something that fits exactly. Do this millions of times with varied sizes and lifetimes and the heap Swiss-cheeses. Size classes greatly mitigate this (same-size holes are perfectly reusable), which is why modern allocators fragment far less than old ones — but they can't eliminate it, because a size class that's grown to 1000 chunks and then mostly freed still holds those pages until the *whole run/slab* is empty. This is precisely why **compacting garbage collectors** (next layer) exist: the only true cure for external fragmentation is to *move live objects together*, which a manual allocator can't do (it would invalidate your pointers) but a managed runtime can.

### Layer 5 — The managed-language twist: GC as an allocator with a clock

In C/C++/Rust, *you* call `free`. In Java, Go, Python, JavaScript, C#, you don't — a **garbage collector** decides when memory is free by periodically determining which objects are still reachable. This changes the allocation story in two profound ways.

First, **allocation can be absurdly cheap** — often a *bump pointer*. Many GC'd runtimes allocate new objects by simply incrementing a pointer into a contiguous nursery region (allocate = `ptr += size`, nearly free), because they don't need to find a fitting hole — they'll clean up later in bulk. This is faster than even a thread-cache malloc. The catch is the "later."

Second, **the cost moves from allocation-time to collection-time, and shows up as latency spikes.** The GC must periodically trace the object graph from roots (chapter 04's stop-the-world pauses), and historically it did this by *pausing your program entirely* — the dreaded GC pause that turns a 2 ms request into a 50 ms one at random. The entire history of GC engineering (Go's concurrent collector, Java's G1/ZAP/Shenandoah/ZGC) is a war to make these pauses shorter and more concurrent, trading throughput and complexity for predictable tail latency. And generational GC is itself an allocator insight: *most objects die young* (the "generational hypothesis"), so collect the young nursery often and cheaply, promote survivors to an old region collected rarely. A compacting GC also *cures external fragmentation for free* by moving survivors together — the thing manual allocators fundamentally cannot do.

The practical upshot for an engineer: in managed languages, "memory management" means managing *allocation rate* and *object lifetime*, because those drive GC frequency and pause length. Reducing allocations in the hot path (pooling, reuse, value types) isn't just saving allocation cost — it's *reducing how often and how long the GC stops your world.* This is the same "allocation-free hot path" goal as in C, reached for a different reason.

### Layer 6 — The kernel's last resort: overcommit and the OOM killer

One more layer, where user-space memory management hits the kernel's policy and chapter 05 reconnects. The allocator hands you address space optimistically; the kernel backs it with physical pages lazily (demand paging) and *overcommits* — promising more than exists. When the bet fails and physical (or cgroup) memory is exhausted, the **OOM killer** terminates a process. From the memory-management angle, the lessons are operational: an allocator that hoards freed pages inflates your RSS and pushes you toward the cgroup limit faster; fragmentation inflates it further; and in a container, the memory limit is far below host RAM, so the allocator's hoarding and fragmentation matter *much more* than on a big bare-metal box. This is why container-deployed services often tune the allocator aggressively — jemalloc's `background_thread` and decay settings, `malloc_trim`, Go's `GOGC` and `GOMEMLIMIT` — to return memory to the OS proactively and stay under the limit. Memory management stops being "does it work" and becomes "does it stay below the ceiling forever."

## A Ladder From L1 to Principal

- **L1 / new grad:** `malloc`/`free` (or the GC) give and reclaim memory; don't leak. You know the heap is where dynamic memory lives.
- **L3–L4 / solid engineer:** You understand the allocator sits above the kernel's pages, that free doesn't always return memory to the OS, and that allocation in hot loops costs (cache + GC pressure). You reuse buffers and pool objects on hot paths.
- **Senior:** You reason about fragmentation (internal vs. external), size classes and arenas, and you know when to swap in jemalloc/tcmalloc. In managed languages you tune allocation rate to control GC frequency and tail latency.
- **Staff:** You diagnose RSS-climb and fragmentation in production, tune allocator decay/return-to-OS behavior and GC parameters against container limits, and design allocation-free hot paths as an architectural property.
- **Principal:** You treat the allocator and GC as first-class system components — choosing them, configuring them fleet-wide, and designing data structures and object lifetimes so the memory system stays bounded and predictable under load. You connect allocation patterns to cache behavior, multicore scaling, tail latency, and cost-per-request as one continuous story.

One idea climbing: *something must chop the kernel's pages into your program's bytes, track what's free, and do it fast and contention-free without leaking space over time — and every "mysterious memory problem" is that something hitting one of its built-in limits.*

## Complexity Analysis

| Operation | Cost | What's happening |
|-----------|------|------------------|
| `malloc` (thread-cache hit) | ~tens of ns, O(1) | Pop a chunk from a thread-local size-class free list; no lock, no kernel |
| `malloc` (cache miss, refill arena) | hundreds of ns | Lock an arena, refill the thread cache from it |
| `malloc` (heap exhausted) | µs (syscall) | `brk`/`mmap` to get more pages from the kernel |
| `free` (small object) | ~tens of ns, O(1) | Push back onto a free list — returns to allocator, **not** the OS |
| `free`/`munmap` (large, mmap'd) | µs (syscall) | Returns pages to the kernel; RSS drops |
| Bump-pointer alloc (GC nursery) | ~few ns | `ptr += size`; cleanup deferred to collection |
| GC minor collection | µs–ms pause | Trace + reclaim young generation; frequency ∝ allocation rate |
| GC major / compaction | ms+ pause | Trace whole heap; compaction also cures external fragmentation |

The number that matters most isn't in any single row — it's that the fast path (thread-cache hit, bump alloc) is ~10–50× cheaper than the slow path (arena lock, syscall, GC), and *your allocation pattern decides how often you take each.*

## War Stories (the shape of the bug in the wild)

- **The service that wouldn't scale past 8 cores.** Throughput flatlined as cores were added; profiling showed everyone blocked on the malloc lock. A one-line switch to jemalloc (per-thread caches, arenas) removed the contention and the service scaled linearly again. The bottleneck was the allocator, not the application.
- **The nightly restart that "fixed" memory.** A long-running service's RSS climbed for hours and never recovered, so ops restarted it every night. The cause was external fragmentation plus the allocator hoarding freed pages — not a leak. Tuning the allocator's return-to-OS behavior (decay/`malloc_trim`) and reducing lifetime-mixed allocations broke the cycle; the nightly restart became unnecessary.
- **The 50 ms tail in a 2 ms service.** A Go service had a clean p50 and a horrible p99. The spikes were GC pauses driven by a hot path allocating thousands of tiny temporary objects per request. Pooling buffers (`sync.Pool`) and cutting per-request allocations slashed GC frequency and flattened the tail. Same fix as a C "allocation-free hot path," different reason.
- **OOM-killed in a container, fine on the laptop.** Code that ran for days on a 64 GB workstation got OOM-killed hourly in a 2 GB container. The allocator's page hoarding and fragmentation, invisible against 64 GB, blew past the cgroup limit. Setting `GOMEMLIMIT` / tuning jemalloc decay to return memory proactively kept it under the ceiling.

## Key Takeaways

1. **The allocator is a user-space data structure between your bytes and the kernel's pages.** It buys pages in bulk (`brk`/`mmap`) and retails small chunks, keeping free-list inventory so most allocations never touch the kernel. Memory management is two layers — this one and chapter 05's pages.
2. **`free` returns memory to the allocator, not usually the OS.** That's why RSS stays high after freeing — the allocator hoards inventory. Large `mmap`'d allocations are the exception (they `munmap` back and drop RSS).
3. **Size classes make allocation O(1) and tame fragmentation** by keeping per-size free lists — interchangeable chunks, no searching, no splitting — at the price of bounded internal fragmentation. It's the structural heart of jemalloc/tcmalloc and the kernel slab allocator.
4. **Per-thread caches and arenas beat the allocator lock**, which is why modern allocators scale on multicore where naïve global-lock malloc doesn't. Swapping in jemalloc/tcmalloc is a real, low-effort throughput win.
5. **External fragmentation is the slow-motion killer** — free memory shattered between live, interleaved-lifetime objects — and it's unbounded and cumulative. Only a *compacting* (moving) collector truly cures it.
6. **GC moves the cost from allocation-time to collection-time**, making allocation nearly free (bump pointer) but introducing pauses; managing allocation *rate* and object *lifetime* is how you control GC frequency and tail latency. Generational GC exploits "most objects die young."
7. **In containers, the allocator's hoarding and fragmentation hit the cgroup limit hard** — far below host RAM — so tuning return-to-OS behavior (`GOMEMLIMIT`, jemalloc decay, `malloc_trim`) is what keeps a service under its ceiling and un-OOM-killed.

## Related Modules

- `05-virtual-memory.md` — the page-level machinery (demand paging, overcommit, OOM killer) the allocator builds on; this chapter is the byte-level layer above it
- `01-processes-and-threads.md` — per-process heaps and stacks, and why allocator lock contention interacts with thread count
- `04-scheduling.md` — lock contention on the allocator and stop-the-world GC pauses as scheduling/latency phenomena
- `../02-data-structures-and-algorithms/01-arrays-and-memory-layout.md` — why scattered heap allocations cause cache misses; size classes mirror dynamic-array geometric growth
- `../02-data-structures-and-algorithms/02-hash-tables.md` — the same "waste bounded space to get O(1)" trade as load factor and size classes
- `../07-core-backend-engineering/` — allocation-free hot paths, object pooling, and buffer reuse as applied patterns
