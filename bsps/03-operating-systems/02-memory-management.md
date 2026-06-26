# Memory Management

## Problem

You call `malloc(32)`. You get back a pointer. You think you just "got 32 bytes of memory." You did not. You invoked one of the most sophisticated data structures in your entire process — an allocator juggling free lists segregated by size, per-thread arenas to avoid lock contention, metadata headers tucked invisibly beside your bytes, and a running negotiation with the kernel over address space. The 32 bytes are almost the least interesting thing that happened. And the moment you call it in a hot loop, in a multithreaded server, on a long-running process, every one of those hidden mechanisms becomes *your* performance and *your* footprint — whether you ever learned their names or not.

Here's the boundary that organizes this chapter, because people blur it. **Virtual memory (chapter 05) is the kernel handing out memory in 4 KB pages.** But your program doesn't allocate in pages — it allocates a 16-byte struct here, a 200-byte string there, a 3 MB buffer occasionally. Something must sit between "the kernel gives me pages" and "my code wants 16 bytes": something that grabs big chunks of pages and chops them into the small, varied pieces your code asks for, tracks which are free, and hands them back. That something is the **allocator** (malloc/free, the runtime heap), living in *user space* inside your process. Memory management is two layers: the kernel's page-level machinery underneath, and the allocator's byte-level machinery on top. **Part I** of this chapter is the allocator. **Part II** is the hard stuff people actually break on: how the *kernel* allocates pages (the buddy and slab allocators), why memory has a *physical location* (NUMA), how a *garbage collector* decides what's free without your help (and how it does it *while your program runs* — concurrent marking, write barriers, moving compaction), and the deepest puzzle of all, **when it is safe to free memory that another thread might still be reading** (lock-free reclamation — the unfinished half of chapter 01's ABA problem).

Because the failures here are *slow and cumulative*, not loud. A service runs fine for hours, then memory creeps up and never comes back — not a "forgot to free" leak but **fragmentation**, free memory shattered too fine to reuse. A request that should be 2 ms spikes to 50 ms — because a garbage collector chose that moment to walk the heap. A multicore service stops scaling — because every thread fights the allocator lock, or because memory is attached to the *wrong socket*. None of these show up in a unit test. All are memory management, and the hardest of them — concurrent GC and safe reclamation — are where even strong engineers get genuinely lost. We're not going to simplify those; we're going to take them head-on.

## Why It Matters (Latency, Throughput, Cost)

**The allocator is a lock, and locks don't scale.** A naïve single global heap means every `malloc`/`free` across every thread contends on one lock. On one core, invisible. On 32 cores running an allocation-heavy server, that lock is the bottleneck and throughput flatlines no matter how many cores you add — the textbook "we scaled the hardware and nothing got faster." This is *why* jemalloc/tcmalloc/mimalloc exist: their headline feature is **per-thread caches and arenas** so the common case touches no shared lock. Swapping the default glibc malloc for jemalloc has improved real services' multicore throughput by double digits with a one-line `LD_PRELOAD`.

**Fragmentation is memory you paid for and can't use.** Your RSS can be 4 GB while live objects are 2 GB — the other 2 GB is free space trapped between live allocations in chunks too small or awkwardly placed to satisfy new requests. You pay for 4 GB (or hit a 4 GB container limit and get OOM-killed, chapter 05) to do 2 GB of work. And allocators rarely return freed memory to the OS promptly — a heap that grew for a spike stays grown. This is why "just restart it nightly" is a real, deployed practice for fragmentation-prone services.

**GC moves cost from allocation-time to collection-time, and it shows up as tail latency.** Allocation in a managed runtime can be nearly free (a bump pointer), but the collector must periodically find and reclaim dead objects, historically by *pausing your program* — the dreaded GC pause turning a 2 ms request into 50 ms at random. The entire history of GC engineering is a war to make those pauses shorter and concurrent, and understanding *how* (Part II) is what lets you tune `GOGC`/`GOMEMLIMIT`/`MaxGCPauseMillis` from understanding instead of superstition.

**On a multi-socket box, where memory lives is as important as how much.** Memory physically attached to socket 0 is ~1.5–2× slower to access from socket 1 (NUMA). An allocator or runtime that hands a thread on socket 1 memory living on socket 0 silently taxes every access. This is why high-performance databases pin memory to nodes (`numactl`) — the allocation *policy* about *location* is a latency decision, and it's invisible until you measure cross-node traffic.

## Mental Model

Picture the allocator as a **shopkeeper between your program and the kernel's warehouse.**

```
   your code            the allocator (user space)              the kernel
   ─────────            ──────────────────────────              ──────────
   malloc(16) ───►  ┌─────────────────────────────┐
   malloc(200) ──►  │  free lists by size class:   │   when low, asks the kernel
   free(ptr) ────►  │   [16][16][16] ...           │   for a big slab of PAGES
   malloc(16) ───►  │   [32][32] ...               │ ◄── mmap / brk ──► [4KB][4KB]...
                    │  per-thread caches (no lock) │                    (chapter 05)
                    └─────────────────────────────┘
        small, varied, frequent                    big, page-aligned, rare
```

The shopkeeper's whole job is **impedance matching** between mismatched worlds. Your program wants small, oddly-sized, frequent allocations with instant turnaround. The kernel deals in big, page-aligned, syscall-expensive chunks. The allocator buys in bulk and retails in small pieces, keeping inventory (free lists) so most requests are satisfied *without bothering the kernel.* Two consequences fall straight out, and they're the heart of Part I:

1. **`free` returns memory to the *shopkeeper*, not the *warehouse*.** The allocator puts the chunk back on a free list to reuse — it does *not* immediately return pages to the kernel. That's why RSS stays high after you've freed everything: the allocator hoards inventory. By design (returning pages is expensive), and the root of "why didn't my RSS drop."
2. **How the free list is organized determines fragmentation.** Sort inventory by size and a 16-byte request is instantly matched; leave it a disorganized pile and you may have plenty of total free space but no piece the right size.

And the model you need for Part II's hard part — garbage collection — is different: instead of *you* telling the shopkeeper "I'm done with this," the GC is a shopkeeper who **periodically figures out for itself which items no customer can reach anymore** by tracing every pointer from a set of roots, and reclaims the rest. The entire difficulty of modern GC is doing that tracing *while customers are still moving things around* — which is exactly the concurrency problem of chapter 01, now inside the allocator.

---

## PART I — The Allocator

### Layer 1 — Where the heap comes from: brk and mmap

Before the allocator can retail, it wholesales from the kernel through two doors. **`brk`/`sbrk`** moves the "program break" — the top of the contiguous heap. Bumping it up gives the allocator more address space to carve. Cheap, but *stack-like*: you can only return memory by lowering the break, which needs the top of the heap to be free — and it usually isn't, pinned by some live allocation. **`mmap`** asks for an independent region of pages anywhere, which can be `munmap`'d back independently. Allocators use `brk` for the small-object heap and `mmap` for large allocations (so one big free actually returns memory).

This already explains a classic mystery: free a 500 MB buffer and RSS drops immediately, but free ten thousand small objects and RSS doesn't budge. The big one was `mmap`'d and `munmap`'d back; the small ones went onto `brk`-heap free lists, trapped behind whatever's above them — returned to the shopkeeper, never to the warehouse.

### Layer 2 — Size classes and free lists

The naïve allocator keeps one list of free chunks of all sizes and searches for one big enough (first-fit/best-fit) — slow, and it fragments badly (split a big chunk for a small request, leaving an awkward remainder, repeatedly, until the heap is confetti). Every serious allocator instead uses **size classes**: predefined sizes (8, 16, 32, 48, 64, …) each with its own free list.

```
request 30 bytes ─► round up to class 32 ─► pop a chunk off the 32-byte free list  (O(1))
free a 32-byte chunk ─► push it back onto the 32-byte free list                    (O(1))
```

The payoff is twofold. **Speed:** alloc/free are O(1) list ops — no searching, no splitting — because chunks in a class are interchangeable. **Fragmentation control:** same-size chunks are perfectly reusable for each other. The cost is **internal fragmentation** — 30 bytes consumes a 32-byte slot, wasting 2 — bounded waste traded for eliminating unbounded external fragmentation. This is the structural heart of jemalloc/tcmalloc/mimalloc, and (Layer 8) the kernel's slab allocator. It's the same trade as a hash table's load factor and a dynamic array's geometric growth: *waste a little bounded space to make the common op O(1).*

### Layer 3 — Arenas and per-thread caches: beating the lock

Size classes make a single-threaded allocator fast. On multicore the bottleneck moves to *contention*: if all threads share free lists, every op must lock them, serializing your program (chapter 01's contention, and Layer 7's cache-line ping-pong on the lock word). The fix that defines modern allocators: **don't share.** **Per-thread (or per-CPU) caches** give each thread its own stash per size class — a thread allocating and freeing its own short-lived objects hits a thread-local list, *zero locking*. **Arenas** split the heap into independent locked regions so even cache refills contend only with a few threads, not all. This is why jemalloc/tcmalloc transform multicore scaling: the default's global lock is the ceiling, and arenas lift it.

### Layer 4 — Fragmentation, named precisely

**Internal fragmentation** is waste *inside* a chunk (the 2 bytes from rounding 30→32) — bounded, the price of O(1). **External fragmentation** is free memory *between* live chunks, in pieces unusable for current requests — total free is plenty but shattered. The dangerous one: unbounded, cumulative, the cause of "RSS climbs forever." Its deep cause: **objects with different lifetimes get interleaved.** Allocate long-lived, then short-lived next to it, free the short-lived — now there's a hole pinned next to something immovable, reusable only by an exact fit. Do it millions of times and the heap Swiss-cheeses. Size classes mitigate it (same-size holes are reusable), which is why modern allocators fragment far less — but can't eliminate it, because a size class grown to 1000 chunks then mostly freed still holds those pages. The only true cure for external fragmentation is to *move live objects together* — which a manual allocator can't (it'd invalidate your pointers) but a **compacting GC can** (Layer 12).

### Layer 5 — GC as an allocator with a clock (the bridge to Part II)

In C/C++/Rust *you* call `free`. In Java/Go/Python/JS/C# a **garbage collector** decides when memory is free by periodically finding which objects are still reachable. Two consequences. First, **allocation gets absurdly cheap** — often a *bump pointer*: allocate by incrementing a pointer into a contiguous nursery (`ptr += size`), because the runtime will clean up in bulk later. Faster than a thread-cache malloc. Second, **the cost moves to collection-time and shows up as latency spikes** — historically a full stop-the-world pause. The generational insight (Layer 12) and the concurrent-collection machinery (Layers 11–12) are the entire response to that. We're now leaving the easy part.

### Layer 6 — The kernel's last word: overcommit and the OOM killer

The allocator hands you address space optimistically; the kernel backs it lazily (demand paging) and *overcommits* — promising more than exists (chapter 05). When the bet fails and physical (or cgroup) memory is exhausted, the **OOM killer** scores processes (roughly by footprint, tunable via `oom_score_adj`) and kills one. The memory-management lessons are operational: an allocator hoarding freed pages inflates RSS toward the cgroup limit; fragmentation inflates it more; and in a container the limit is far below host RAM, so hoarding and fragmentation matter *much* more. Hence container-tuned allocators (`background_thread`/decay in jemalloc, `malloc_trim`, Go's `GOGC`/`GOMEMLIMIT`) that return memory proactively. (Modern kernels also expose **PSI** — Pressure Stall Information, `/proc/pressure/memory` — which reports the fraction of time tasks stalled waiting on memory, a far earlier and better signal than waiting for the OOM kill; chapter 05 returns to it.)

---

## PART II — The Hard Part

### Layer 7 — Where pages themselves come from: the buddy allocator

We said the allocator gets pages from the kernel. But how does the *kernel* manage physical pages? It has the same problem one level down — hand out contiguous runs of physical pages, fight fragmentation, do it fast under concurrency — and its answer is the **buddy allocator**, worth understanding because its fragmentation behavior causes real production failures.

The buddy system manages memory in power-of-two blocks (1, 2, 4, 8, … pages). To satisfy a request, it finds the smallest power-of-two block that fits; if only a bigger block is free, it **splits** it in half repeatedly, each half called the other's "buddy," until it reaches the right size. When a block is freed, the allocator checks whether its buddy is *also* free and, if so, **coalesces** them back into the larger block — recursively. The buddy of a block is found by a single bit-flip of its address, so split/merge are O(1).

```
need 1 page, only an 8-page block free:
  [ 8 pages ]  → split → [4][4] → split → [2][2] → split → [1][1]  give one, track the rest
free that page later: is its buddy free? yes → merge → [2] → buddy free? → [4] → ...
```

The elegance is the cheap coalescing — buddies recombine automatically, fighting fragmentation. The weakness is **external fragmentation of physical memory**: over time, memory can become littered with scattered single free pages whose buddies are *not* free, so they can't coalesce into the larger contiguous blocks that some allocations *require*. And here's where it bites you in production: **huge pages need 2 MB of physically contiguous memory** (chapter 05), and a long-running, fragmented system may have gigabytes free yet be unable to find a single contiguous 2 MB run — so huge-page allocation fails, or the kernel's **memory compaction** (which migrates pages to create contiguous regions) kicks in and causes latency stalls. The infamous Transparent Huge Pages latency spikes are partly the buddy allocator's fragmentation forcing compaction. The kernel mitigates with **migration types** (grouping movable vs. unmovable allocations so reclaimable memory stays coalescable), but the lesson stands: fragmentation is fractal — it exists at the malloc level *and* the physical-page level, and the second kind is what makes huge pages flaky on old, busy machines.

### Layer 8 — The slab allocator: caching live objects, not just free space

The buddy allocator deals in pages, but the kernel constantly allocates *small fixed-size objects* — `task_struct`s (chapter 01), inodes, file descriptors, network buffers — thousands of times a second. Carving these from the buddy allocator would be wasteful (internal fragmentation) and slow. So Linux layers the **slab allocator** (and its successors SLUB/SLOB) on top: for each *type* of frequently-used object, it keeps caches ("slabs") of pre-carved, ready-to-go object slots.

The deep idea that makes slab special isn't just size-classing — it's **caching the *initialized* state of objects.** Constructing a kernel object (setting up its locks, lists, fields) costs work; when you free it, the slab allocator can keep it in a *partially-constructed* state so the next allocation of that type skips reinitialization. Combined with per-CPU slab caches (no lock on the fast path — same trick as userspace thread caches) and object coloring (offsetting objects within slabs so different objects of the same cache land on *different* cache lines, reducing conflict misses), the slab allocator makes "give me a fresh `task_struct`" nearly free. You can watch it live in `/proc/slabinfo` — and a leaking kernel object type shows up there as a slab cache growing without bound, one of the few ways to diagnose a *kernel* memory leak. The pattern to carry: *size classes + per-CPU caches + reuse of constructed state* is the universal recipe for fast fixed-size allocation, in the kernel exactly as in jemalloc.

### Layer 9 — Coalescing and boundary tags: how malloc fights fragmentation

Back in userspace, how does a general-purpose `malloc` (one that *doesn't* use pure size classes, or for its large-object path) merge adjacent free chunks so freed memory doesn't stay shattered? The classic technique is **boundary tags** (Knuth), and it's a small jewel of data-structure design. Each chunk stores its size and free/used status in a header *and* a footer (the boundary tags). When you free a chunk, you want to merge it with adjacent free neighbors — but how do you find the chunk *before* you in memory without scanning from the start? The footer of the previous chunk sits *immediately before your header*: read it, learn the previous chunk's size and status in O(1), and if it's free, coalesce by extending backwards. The header of the next chunk is at your end + size: O(1) forward too.

```
... [hdr|  chunk A  |ftr][hdr|  chunk B (freeing) |ftr][hdr| chunk C |ftr] ...
                      ▲                                  ▲
   read A's footer (just before B's header) ──► A free? merge.   read C's header ──► C free? merge.
   O(1) both directions, no scanning — that's the boundary-tag trick.
```

This is why glibc's malloc can do **immediate coalescing** — every free checks both neighbors and merges, keeping free chunks as large as possible. The trade-offs are real and they're why allocators differ: immediate coalescing fights fragmentation but costs work on every free and can cause "coalescing thrash" if you repeatedly free-and-reallocate the same size (merge, split, merge, split). Some allocators defer coalescing, batch it, or skip it for small size classes entirely (where same-size reuse makes it pointless). The boundary tag is also why every malloc'd chunk has hidden overhead bytes — and why `malloc(16)` actually consumes more than 16, and why heap overflow exploits target these headers (corrupt a size field and you can trick the allocator into returning overlapping chunks — the classic heap-exploitation primitive).

### Layer 10 — NUMA: memory has a location

On a single-socket machine, all memory is equidistant from the CPU (UMA — uniform memory access). On multi-socket servers — most database and large-service hardware — each CPU socket has its *own* bank of RAM physically attached to it, and accessing another socket's RAM means crossing the inter-socket interconnect (QPI/UPI/Infinity Fabric). That's **NUMA** (Non-Uniform Memory Access): local memory ~100 ns, remote memory ~150–200 ns, plus the interconnect has *bandwidth* limits that become a bottleneck under heavy cross-node traffic.

The crucial and counterintuitive fact: **a page's physical location is decided by the kernel's default "first-touch" policy — the page is allocated on the NUMA node of whichever CPU *first writes* to it, not whichever CPU allocated it.** This produces a classic, devastating bug: a program where one initialization thread `malloc`s and zeroes a giant array (first-touching every page → all pages land on *that thread's* node), then spawns worker threads across all sockets to process it. Now every worker on a *remote* socket pays the cross-node penalty on every access, and the one node holding all the memory becomes a bandwidth chokepoint. The array was "allocated correctly"; it just all lives on the wrong node for most of its users.

```
first-touch trap:
  init thread on node 0:  malloc + memset huge_array   → ALL pages allocated on node 0
  worker threads on node 1: read huge_array            → every access is REMOTE (slow + congested)
```

The fixes are policy: **first-touch by the thread that will use the data** (have each worker initialize its own slice, so pages land local), explicit **`numactl --interleave`** (spread pages round-robin across nodes so no single node is the bottleneck — good for shared data with no clear owner), `mbind`/`set_mempolicy` for fine control, or pinning threads *and* memory to the same node (`numactl --cpunodebind --membind`). Databases (Postgres, MySQL, Oracle), JVMs with large heaps, and in-memory caches all care intensely about this. NUMA is the reason "we added a second CPU socket and it didn't scale as expected, or got slower" — the memory placement, not the cores, was the problem. It's the allocator's *location* policy mattering as much as its *size* policy.

### Layer 11 — Garbage collection I: reachability, tri-color, and why concurrency is hard

Now the deep one. A GC's job: find every object still **reachable** and reclaim the rest. "Reachable" means: start from the **roots** (global variables, every thread's stack and registers — the things the program can directly name) and follow every pointer transitively. Anything you can reach is live; anything you can't is garbage, *by definition* — the program has no way to ever name it again. This is a graph traversal (chapter 04 of module 02): the heap is a directed graph of objects-pointing-to-objects, and GC is reachability from the roots.

The clean way to describe a *concurrent* collector's progress is the **tri-color abstraction** (Dijkstra). Every object is one of three colors:

- **White** — not yet proven reachable (candidate garbage). Everything starts white.
- **Grey** — proven reachable, but its outgoing pointers haven't been scanned yet (a frontier/worklist node — exactly BFS/DFS's frontier).
- **Black** — proven reachable AND all its pointers scanned (fully processed).

```
GC marking = graph traversal:
  start: roots → grey.   loop: pick a grey object, scan its pointers
         (white children it points to → grey), then it → black.
  done when no grey left.   Every still-WHITE object is unreachable → free it.
```

Simple — *if the program holds still.* But stop-the-world pauses (freezing every thread for the whole mark) are the latency villain. We want to mark **concurrently**, while the program (the "mutator") keeps running and *changing pointers underneath us.* And that's where it gets genuinely hard, because concurrent mutation can make a live object look dead. The fatal scenario, the **lost-object problem**:

> The collector has finished scanning object A and colored it **black** (done — won't look at it again). Meanwhile the mutator takes a pointer to a **white** object C (which was only reachable via some grey object B), stores that pointer *into black A*, and then deletes the path from B to C. Now C is reachable (via A), but A is black (won't be re-scanned) and C is white (will be collected). **The GC is about to free a live object** — a use-after-free, the worst possible bug, caused by the program legally moving a pointer while the GC wasn't looking.

The invariant that must never break is: **no black object points to a white object** (with no grey object protecting it). The collector defends this with **barriers** — tiny bits of code the compiler injects around pointer operations, the GC's equivalent of the memory barriers in chapter 01:

- A **write barrier** intercepts pointer *stores*. Two famous flavors. **Dijkstra (insertion) barrier:** when the mutator writes a pointer to white C into black A, the barrier *greys C* (re-protecting it). **Yuasa (deletion) barrier:** when the mutator *overwrites/deletes* a pointer (the B→C edge), the barrier greys the old target C before it's lost (a "snapshot-at-the-beginning" approach — anything reachable when marking started stays alive this cycle). Either way, the barrier restores the invariant at the exact moment the mutator could break it.
- Go's collector uses a hybrid write barrier; Java's collectors use write barriers (and card marking, next layer); this machinery is *why* concurrent GC is correct.

The takeaway you can carry into tuning: concurrent GC isn't free even when it doesn't pause — **every pointer write in your program pays a small write-barrier tax** so the collector can run alongside you. That's a real, if small, throughput cost (a few percent), and it's the trade you accept to convert long stop-the-world pauses into short ones. When you read "Go optimized its write barrier" or "ZGC has a load barrier," this is the cost they're shaving.

### Layer 12 — Garbage collection II: generations, card tables, and moving objects while the program runs

Two more hard pieces complete the modern GC picture.

**Generational collection and the find-the-pointers problem.** The **generational hypothesis** — empirically, *most objects die young* (temporaries, request-scoped data) — suggests collecting the young "nursery" frequently and cheaply, promoting survivors to an "old" generation collected rarely. Huge win: a minor collection scans only the small nursery, not the whole heap. But there's a snag that's subtler than it looks: to collect the young generation, you must know all pointers *into* it — including pointers from **old** objects to young ones (an old cache holding a fresh entry). Scanning the entire old generation to find them would destroy the whole "only look at the nursery" benefit.

The solution is a **write barrier** again, feeding a **card table** (or remembered set): divide the old generation into small "cards" (e.g., 512 bytes each) with a one-byte dirty flag per card. The write barrier, on *every* pointer store into an old object, marks that object's card dirty. At minor-collection time, the GC scans only the *dirty* cards of the old generation for young-pointing references, not the whole thing. So the generational hypothesis pays off, financed by a write barrier whose cost is a single byte-store per pointer write. This is *why* young-gen collections can be sub-millisecond on a multi-gigabyte heap: you never touch most of it.

**Concurrent compaction — the genuinely mind-bending part.** Recall (Layer 4) the only cure for external fragmentation is to *move live objects together* — compaction. But how do you move an object while the program is actively reading and writing it through pointers that hold its *old* address? Move it and every existing pointer is now dangling. Doing this *concurrently*, without a long stop-the-world pause to fix up every pointer, is the frontier of GC engineering, and the two production answers are worth knowing because they're beautiful:

- **Shenandoah (Red Hat)** gives every object an extra **forwarding pointer** (a Brooks pointer) at its head. To move an object, copy it and set the original's forwarding pointer to the copy; all access goes *through* the forwarding pointer, so it transparently redirects to the new location. A **read barrier** follows the forwarding pointer on every access. Pointers get updated to the new address lazily, concurrently.
- **ZGC (Oracle)** uses **colored pointers** + a **load barrier**: it stashes metadata bits *inside* the 64-bit pointer itself (marking/relocation state), and a load barrier on every pointer *read* checks those bits — if the object has been relocated, the barrier transparently fixes the pointer to the new address ("self-healing") right then. ZGC achieves **sub-millisecond pauses on multi-terabyte heaps**, which a decade ago would have sounded impossible.

The price of all this — forwarding pointers, load/read barriers on *every* object access — is throughput: you're paying a small tax on reads and writes constantly so that pauses can be tiny and bounded regardless of heap size. That is the fundamental, unavoidable GC trade, stated once and for all: **you can have low pause times or maximum throughput, and the barriers are how you buy the former with the latter.** Choosing a collector (Go's low-latency concurrent GC, Java's G1 for balance, ZGC/Shenandoah for tail latency, Parallel/throughput collectors for batch) is choosing your point on that curve — and now you know the mechanism behind the choice.

### Layer 13 — Lock-free memory reclamation: the unfinished half of ABA

Chapter 01 left a hard problem open. In a lock-free data structure (no mutex), when one thread *removes* a node from, say, a lock-free queue, **when is it safe to actually `free` that node?** Another thread might, at this very instant, be holding a pointer to it mid-traversal (it read the pointer a moment ago, before you removed it). Free it now and that thread reads freed memory — use-after-free. Worse, the freed memory gets *reallocated* as a new node, and now CAS sees the same address and succeeds incorrectly — that's the **ABA problem** from chapter 01, and you can now see it's *fundamentally a reclamation problem*: ABA happens precisely because a freed-and-reused address fools a pointer comparison. Solve safe reclamation and you solve ABA. There's no GC to lean on (this is C/C++/Rust, or the runtime's own internals), so this is genuinely one of the hardest corners of systems programming, and three schemes solve it:

- **Hazard pointers** (Michael). Before dereferencing a shared pointer, a thread *publishes* it to a per-thread "hazard" slot ("I am about to use this node — don't free it"). When a thread wants to free a node, it first scans all threads' hazard slots; if any thread has published that node, the free is *deferred* (the node goes on a retry list). Bounded memory, lock-free, but every access has the cost of publishing and a memory barrier.
- **Epoch-based reclamation** (EBR). A global epoch counter advances periodically; each thread announces the epoch it's working in when it enters a critical section. A node removed in epoch *e* can only be freed once *every* thread has advanced past *e* (so no one can still hold a pre-removal pointer). Lower per-operation cost than hazard pointers (just an epoch announcement), but a single stalled thread can stall reclamation and let memory grow — the classic EBR weakness. Crossbeam (Rust) uses this.
- **RCU (Read-Copy-Update)** — the Linux kernel's crown jewel, and the most elegant of all. The premise: **readers never block, never write, never even use atomics on the fast path** — they just read. Writers don't modify in place; they *copy* the data, modify the copy, and atomically swap the pointer to publish it. The old version is freed only after a **grace period** — a wait until every CPU has passed through a "quiescent state" (a context switch, or a moment with no RCU readers), which *guarantees* no reader can still hold the old pointer. The genius is that on the read side RCU is almost *free* (no locks, no atomics, no cache-line contention — readers on different cores don't interfere at all), making it perfect for read-mostly data that's read constantly and updated rarely: routing tables, the kernel's directory-entry cache, module lists, security policies. RCU is *the* reason the Linux kernel scales reads across hundreds of cores.

```
RCU:  readers ──► just read (no locks, no atomics, near-zero cost, never block)
      writer  ──► copy → modify copy → atomic publish pointer → wait grace period → free old
                   the grace period guarantees every reader has moved on before the free
```

The unifying insight across this whole layer (and the link back to Layer 11): **garbage collection and lock-free reclamation are the same problem** — "when can I safely free memory that something else might still reference?" — solved at different levels. A tracing GC answers it by *proving unreachability from roots*; RCU/hazard-pointers/epochs answer it by *proving no live reader holds the pointer*. Either way, the deepest question in memory management isn't "how do I allocate" — it's "**how do I know when it's safe to free**," and every sophisticated system is, underneath, an answer to that one question.

---

## A Ladder From L1 to Principal

- **L1 / new grad:** `malloc`/`free` (or the GC) give and reclaim memory; don't leak; the heap holds dynamic memory.
- **L3–L4 / solid engineer:** The allocator sits above the kernel's pages; `free` doesn't always return memory to the OS; hot-loop allocation costs (cache + GC pressure). You pool buffers and reuse objects on hot paths.
- **Senior:** You reason about fragmentation (internal vs. external), size classes and arenas, when to swap in jemalloc/tcmalloc, and NUMA first-touch. You tune allocation *rate* to control GC frequency and tail latency.
- **Staff:** You understand GC mechanism — tri-color, write barriers, generational/card tables, concurrent vs. STW — and tune collectors and allocator return-to-OS behavior against container limits. You diagnose RSS-climb, fragmentation, NUMA imbalance, and slab leaks in production.
- **Principal:** You treat allocator, GC, and memory *placement* as first-class design surfaces — choosing collectors on the pause/throughput curve, designing object lifetimes and NUMA layout for the workload, and reaching for lock-free + RCU-style reclamation where read scaling demands it. You see GC and lock-free reclamation as one question — *when is it safe to free?* — and design accordingly.

One idea climbing: *something must chop pages into bytes, fight fragmentation at every level (malloc, buddy, slab), place memory where it'll be used (NUMA), and — the deepest part — determine when memory is safe to reclaim, whether by tracing reachability (GC) or proving no reader remains (RCU). Every "mysterious memory problem" is one of these mechanisms hitting a limit.*

## Complexity Analysis

| Operation | Cost | What's happening |
|-----------|------|------------------|
| `malloc` (thread-cache hit) | ~tens of ns, O(1) | Pop from a thread-local size-class list; no lock, no kernel |
| `malloc` (arena refill / heap grow) | hundreds of ns – µs | Lock an arena, or `brk`/`mmap` syscall |
| `free` (small) | ~tens of ns, O(1) | Push onto a free list — back to allocator, **not** the OS |
| Coalescing free (boundary tags) | O(1) | Check both neighbors via header/footer; merge if free |
| Buddy alloc/free | O(log pages) | Split/merge power-of-two blocks; buddy via bit-flip |
| Bump-pointer alloc (GC nursery) | ~few ns | `ptr += size`; cleanup deferred |
| Write barrier (per pointer store) | ~1–few ns each | The concurrent/generational GC tax on every pointer write |
| Load/read barrier (ZGC/Shenandoah) | ~per pointer read | Self-healing relocation check — buys sub-ms pauses |
| Minor GC (generational) | µs–ms | Scan nursery + dirty cards only; frequency ∝ alloc rate |
| Concurrent mark/compact | mostly concurrent, µs pauses | Barriers keep it correct while the program runs |
| RCU read | ~free | No lock, no atomic, never blocks |
| RCU reclaim / hazard-pointer free | deferred (grace period / scan) | Safe only once no reader can hold the pointer |
| Remote NUMA access | ~1.5–2× local | Cross-socket interconnect; bandwidth-limited under load |

The number behind every tuning decision: the fast path (thread-cache hit, bump alloc, RCU read) is ~10–50× cheaper than the slow path (arena lock, syscall, GC pause, remote NUMA), and your access *patterns* decide how often you take each.

## War Stories (the shape of the bug in the wild)

- **The service that wouldn't scale past 8 cores.** Throughput flatlined; everyone was blocked on the malloc lock (and its cache-line ping-pong, chapter 01 Layer 7). A one-line switch to jemalloc (per-thread caches, arenas) restored linear scaling.
- **The array that lived on the wrong NUMA node.** A numerical service initialized a huge array in one thread (first-touch put it all on node 0), then processed it with workers across both sockets. Workers on node 1 paid remote-access cost on every read and saturated the interconnect. Fix: each worker first-touched its own slice (`numactl`/parallel init), pages went local, throughput jumped.
- **The 50 ms tail in a 2 ms service.** A Go service had clean p50, horrible p99 — GC pauses from a hot path allocating thousands of tiny temporaries per request. Pooling buffers (`sync.Pool`) cut allocation rate, GC frequency dropped, the tail flattened. Same "allocation-free hot path" goal as C, reached to spare the *collector*, not the allocator.
- **The huge pages that wouldn't allocate.** A long-running box had gigabytes free but couldn't back THP — physical memory was so fragmented (buddy allocator, Layer 7) that no contiguous 2 MB run existed, so the kernel ran compaction and caused latency stalls. Disabling THP (per Redis/Mongo guidance) removed the stalls.
- **The lock-free queue that corrupted under load.** A hand-rolled lock-free structure freed nodes immediately on removal; under contention a reader holding a stale pointer hit use-after-free, and reused addresses triggered ABA. The fix was proper reclamation (hazard pointers) — and the deeper lesson that lock-free without a reclamation scheme is broken by construction.

## Key Takeaways

1. **The allocator is a user-space data structure between your bytes and the kernel's pages** — size classes for O(1) bounded-waste allocation, per-thread caches/arenas to beat the lock. `free` returns memory to the allocator, not usually the OS (why RSS stays high); large `mmap`'d allocations are the exception.
2. **Fragmentation is fractal:** internal (bounded, the price of size classes), external in malloc (interleaved lifetimes, cured only by moving objects), *and* external in physical pages (the buddy allocator — why huge pages get flaky on busy machines). Boundary tags give O(1) coalescing to fight it.
3. **The slab allocator caches *constructed* fixed-size kernel objects** — size classes + per-CPU caches + reuse of initialized state — the same fast-fixed-size recipe as jemalloc, visible in `/proc/slabinfo`.
4. **Memory has a location (NUMA), set by first-touch.** The classic bug is one thread initializing data that many remote threads then use; fix by first-touching where you'll use it, or `numactl` interleave/bind. Placement is a latency decision as much as size.
5. **GC is reachability from roots (tri-color), and doing it *concurrently* is hard** because the program moves pointers underneath the collector (the lost-object problem). **Write barriers** maintain the no-black-points-to-white invariant — meaning every pointer write in your program pays a small tax so the GC can run alongside it.
6. **Generational GC exploits "most objects die young,"** financed by a write barrier + **card table** so minor collections scan only the nursery and dirty old cards — why sub-ms young-gen collections on huge heaps are possible.
7. **Concurrent compaction moves live objects while the program runs** via forwarding pointers (Shenandoah) or colored pointers + load barriers (ZGC), achieving sub-ms pauses on terabyte heaps. The eternal trade: **low pause vs. high throughput, with barriers buying the former with the latter.**
8. **The deepest question is "when is it safe to free?"** Tracing GC answers it by proving unreachability; **RCU / hazard pointers / epochs** answer it by proving no live reader holds the pointer — and that's the same problem as ABA. **RCU** makes readers nearly free (no locks/atomics, never block), which is how the Linux kernel scales read-mostly data across hundreds of cores.

## Related Modules

- `05-virtual-memory.md` — demand paging, overcommit, the OOM killer, PSI, and physical-page management (the buddy allocator) this chapter builds on; huge-page contiguity
- `01-processes-and-threads.md` — the allocator lock and cache-line contention; the ABA problem this chapter's RCU/hazard-pointers complete; signal handlers can't allocate (async-signal-safety)
- `04-scheduling.md` — GC stop-the-world and RCU grace periods as scheduling/latency events; per-CPU structures mirror the allocator's per-thread caches
- `../02-data-structures-and-algorithms/01-arrays-and-memory-layout.md` — why scattered allocations cause cache misses; size classes mirror dynamic-array growth
- `../02-data-structures-and-algorithms/04-graphs-and-network-algorithms.md` — GC marking *is* graph reachability/BFS-DFS from roots
- `../07-core-backend-engineering/` — allocation-free hot paths, object pooling, and GC tuning as applied patterns
