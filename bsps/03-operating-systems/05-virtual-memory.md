# Virtual Memory

## Problem

Run this on your laptop right now: open two programs, attach a debugger to each, and ask both "what's the address of your main global variable?" There's a real chance they answer with the *same number* — `0x55…` something, identical. Two programs, same address, both running, neither crashing, neither reading the other's data. How? If memory is "a street of numbered houses" (the arrays chapter), how can two programs both live at house #93,824,992,236,885 without one stomping the other?

The answer is the most important illusion in computing, and the fact that you've never had to think about it is proof of how well it works: **the addresses your program uses are fake.** Every address in your code is a *virtual* address — a name your process invented — and hardware translates it, on *every memory access*, into a *physical* address in the actual RAM chips. Your process believes it owns a vast, private, contiguous expanse of memory from zero. It owns no such thing. It owns a sparse scattering of 4 KB physical pages wherever the OS found room, stitched into the *appearance* of a clean private address space. The two programs share an address because that address is a lie they each tell privately, resolved to different physical RAM.

This isn't trivia — it's the load-bearing mechanism under a startling amount of what you do. Process isolation (one backend can't corrupt another)? Virtual memory. `mmap`-ing a file as an array? Virtual memory. Copy-on-write `fork()`? The page cache that makes the second read 1000× faster? Allocating more memory than you have RAM and having it *work*? The OOM kill while `free` showed gigabytes available? All virtual memory. **Part I** of this chapter builds the illusion: translation, the TLB, the page fault, demand paging, and the realization that mmap/COW/page-cache/swap/isolation are all *one mechanism*. **Part II** takes the genuinely hard internals people break on and rarely learn — **TLB shootdowns** (the multicore nightmare where changing one mapping forces synchronous interrupts across every core), how page **reclaim** actually chooses victims (the LRU approximations, and the new MGLRU), **reverse mapping**, the gritty truth of **transparent huge pages** and the compaction stalls they cause, the **copy-on-write exploits** (Dirty COW) that turned this elegant trick into a kernel-level security hole, and the modern **memory-pressure and cgroup** machinery (PSI, swap, `memory.high`). These are where the 3am incidents come from, and we're taking them at full difficulty.

## Why It Matters (Latency, Throughput, Cost)

**The TLB is a tiny cache whose misses you pay invisibly.** Translation isn't free — turning virtual→physical means consulting a page table that itself lives in memory. Doing that per access would be insane, so the CPU caches recent translations in the **TLB** (Translation Lookaside Buffer), a few hundred to ~1500 entries each covering one 4 KB page — so it "sees" only a few megabytes at once. Sweep a structure bigger than that with poor locality and you *miss* the TLB: each access now pays a **page-table walk** (several dependent memory reads) *before* fetching your data. TLB misses alone can be 10–30% of runtime on large scattered workloads — invisible to your code, your Big-O, and naïve profilers.

**A page fault is a 100,000× latency cliff behind a normal-looking access.** Reading an address whose page isn't backed by physical memory *traps into the kernel* — a **page fault**. A *minor* fault (page in RAM but not yet mapped, e.g. first touch, or a page-cache hit) costs ~1 µs. A *major* fault (page must come from disk — swapped out, or a file page not yet read) costs ~100 µs to many ms. The same `x = arr[i]` is a 1 ns L1 hit or a 10 ms disk fault depending on the page's state, and nothing in the syntax tells you which. This is why a service that started swapping doesn't get "a bit slower" — it falls off a cliff.

**Overcommit lets you allocate memory you don't have — until the bill comes due as an OOM kill.** Because pages are backed by RAM only when first *written* (demand paging), Linux lets processes allocate far more than physically exists. Great for fork-heavy and sparse workloads. The catch: when processes *do* collectively touch more than exists, the kernel's **OOM killer** terminates one. This is why a container is OOM-killed while monitoring shows "free memory" — allocation succeeded (virtual), the kill happened on *touch* (physical), and the cgroup ceiling is far below host RAM.

**The hard internals decide your tail and your blast radius.** A TLB shootdown storm can serialize a many-threaded process on every `munmap`. A bad reclaim decision swaps out your hot working set. THP compaction stalls spike your p99. A COW bug is a privilege-escalation exploit. And whether you see memory pressure *coming* (PSI) or only *after* the OOM kill is the difference between graceful degradation and an outage. These are the genuinely hard parts, and they're exactly the ones that get hand-waved.

## Mental Model

Hold two pictures at once: what your program sees, and what's real.

```
WHAT YOUR PROCESS BELIEVES:            WHAT'S ACTUALLY TRUE:
one huge, private, contiguous          a sparse scatter of 4 KB physical pages,
address space from 0 → 2^48            wherever the OS found room, plus a
                                       translation table faking the contiguity

  virtual                                physical RAM (frames)
  ┌───────────┐ 0x0000                   ┌──────┐ frame 7   ← virt page 2 lives here
  │  code      │ page 0  ──────┐         ├──────┤ frame 19  ← virt page 0
  │  data      │ page 1  ───┐  │         ├──────┤ frame 3   ← someone else's
  │  heap ───► │ page 2  ─┐ │  └────────►├──────┤ frame 41  ← virt page 1
  │  stack ◄── │ page N   │ └───────────►├──────┤
  └───────────┘          └─────────────►└──────┘
       │  every access: MMU translates virtual page → physical frame
       ▼  via the page table, cached in the TLB
```

The core operation: **a virtual address splits into a page number and an offset.** The page number is looked up to find a physical frame; the offset is added unchanged. Translation happens at *page granularity* — which is why everything in virtual memory (faults, sharing, protection, swapping) happens in 4 KB units.

The second idea that makes it tractable: **mappings are lazy and meaningful.** A page table entry needn't point at real RAM. It can say "not present" (touch → fault → kernel decides), "present read-only" (the COW trick), "on disk in swap," or "this is a file, fault it in." The page table isn't just a translation map — it's a set of *instructions to the kernel* about what to do when you touch each page. From that one indirection grow demand paging, COW, mmap, swap, and the page cache. And the *hard* truth Part II adds: this map is **shared and cached across many cores**, so changing it isn't a local edit — it's a distributed coherence problem (TLB shootdowns), with the same flavor as cache coherence in chapter 01, but for *translations* instead of *data*.

---

## PART I — Building the Illusion

### Layer 1 — The page table: translation as a lookup

The simplest translation is a big array: one entry per virtual page → physical frame. The problem is size: a 48-bit space with 4 KB pages has 2³⁶ ≈ 68 billion pages; a flat table would need hundreds of GB *per process* — absurd, since processes use a sparse fraction of their space. The fix is a **multi-level page table** — a tree. A small top table points to second-level tables, which point to third-level, etc. (x86-64 uses 4 levels, newer chips 5). Sparsity wins: unused regions have no lower tables (a null high-level entry prunes a whole subtree). A process using a few MB needs a handful of small tables, not a map of 68 billion pages.

```
virtual address (48 bits) = [ L4 idx | L3 idx | L2 idx | L1 idx | offset (12 bits) ]
   CR3 register ─► L4 table ─► L3 table ─► L2 table ─► L1 table ─► physical frame + flags
```

The cost: translating one address now walks *four* tables — **four dependent memory reads** before you reach your data (they can't be parallelized; each tells you where the next lives). A raw page-table walk is brutally slow — which is why the next layer exists.

### Layer 2 — The TLB: caching translations

If every access required a four-level walk, computers would be unusable. So the CPU keeps a cache *just for translations*: the **TLB**, mapping virtual page → physical frame for recent pages, answering in ~1 cycle. The vast majority of accesses hit it, translation is effectively free, and the page table is never touched. The walk happens only on a TLB *miss*, then the result is cached.

This gives you a *second* locality beyond the arrays chapter's data-cache locality: **keep your translations in the TLB.** Sequential access wins twice (data in cache *and* one TLB entry covering 4 KB of consecutive accesses); scattered access loses twice (cache misses *and* TLB misses triggering full walks). It's why pointer-chasing a huge structure is even worse than the cache story alone — two caches missed, one miss costing four dependent reads. And **a context switch to a different process invalidates the TLB** (different translations) — chapter 01's "TLB flush," why the first microseconds after a process switch run slow. (Tagged TLBs — PCID/ASID — soften this by tagging entries with an address-space ID so a switch needn't flush everything; the *hard* counterpart, what happens on *multicore* mapping changes, is Layer 7.)

### Layer 3 — Huge pages: extending TLB reach

The TLB has a fixed, small entry count, each normally covering 4 KB — so its total *reach* is a few MB. A multi-GB heap blows past that and lives in chronic TLB misses. **Huge pages** (2 MB, or 1 GB) make one entry cover 2 MB — *512× more per entry* — so the same TLB now reaches gigabytes and the chronic misses vanish. Trade-offs: coarser (a barely-used 2 MB page wastes 2 MB) and harder to allocate when memory is fragmented. Explicit huge pages are reserved up front; **Transparent Huge Pages (THP)** promote regions automatically — and THP is famously double-edged (background defrag stalls), which is why Redis/Mongo often recommend disabling it. The *why* behind those stalls is Part II, Layer 11.

### Layer 4 — The page fault: where the kernel takes over

The hinge of the whole system. A PTE can be marked **"not present."** When the CPU translates an address and finds one, it *traps into the kernel*: a **page fault.** The faulting instruction freezes, the kernel's handler decides what should be there and makes it so, then resumes the instruction as if nothing happened. From your program's view, the access just took a while. The cost depends on *why*:

- **Minor fault (~1 µs):** no disk needed. Freshly-allocated memory first-touched (grab a zeroed frame, map it), or a file page already in the page cache (just map it), or a COW copy.
- **Major fault (~100 µs–10 ms):** the page must come from disk (swapped out, or a file page not yet read). Storage latency, and the thread *blocks* the whole time. The latency cliff.

The minor-vs-major distinction is one of the most useful things on a dashboard: a climbing *major*-fault rate is the unambiguous signature of memory pressure / swapping, explaining tail spikes that CPU and disk-throughput graphs miss.

### Layer 5 — Demand paging and overcommit

The not-present trick enables the system's defining laziness: **nothing is backed by RAM until touched.** `malloc(1 GB)` just extends your virtual space and marks pages not-present; frames attach one at a time, by minor fault, as you *write*. Allocate a giant array, never touch half — that half costs no RAM. This **demand paging** is why allocation is fast and cheap regardless of size (the cost deferred to first touch — also why benchmark first-passes are slow, the "mysterious first run" from arrays). It makes **overcommit** natural: hand out more virtual memory than physical, betting most is never simultaneously resident. Usually a great bet (fork+COW, sparse structures); but a bet, and when reality calls it the **OOM killer** scores processes (~by footprint, `oom_score_adj`) and kills one. Resolution of the "OOM with free memory" paradox: allocation succeeded virtually long ago; the reckoning came at touch-time; in containers the cgroup ceiling is far below host RAM.

### Layer 6 — The same machine, wearing every hat

The payoff: a catalog of "different" features are all the page-table-plus-fault mechanism. **Process isolation** — each process's page table simply can't *name* another's frames (the wall is the absence of an entry). **Copy-on-write `fork()`** — shared frames marked read-only; a write faults, copies one page, remaps private. **`mmap`** — map a file's bytes into your space; access becomes ordinary memory, the kernel faults pages from the file. **The page cache** — file pages stay resident as frames and map into anyone who reads the file (the second read is a minor fault, not disk — the ~1000× speedup). **Swap** — evict cold pages to disk, mark not-present; touching them majors them back. Five features, one mechanism. Internalizing "it's all page tables with not-present entries and a fault handler" lets you *derive* their behavior — including the failure modes Part II is about.

---

## PART II — The Hard Internals

### Layer 7 — TLB shootdowns: the multicore translation-coherence nightmare

Here's a problem the single-core picture hides completely. The TLB caches translations — but each core has its *own* TLB. Now suppose a thread on core 0 changes a mapping: it `munmap`s a region, or marks a COW page read-only, or `madvise(MADV_DONTNEED)`s a page away. Core 0 updates the page table and flushes *its own* TLB. But threads of the *same process* are running on cores 1, 2, 3… and *their* TLBs may still hold the **now-stale translation** — they'd keep using a mapping that no longer exists, reading freed or wrong physical memory. Unlike data cache coherence (MESI, chapter 01), which the *hardware* maintains automatically, **TLB coherence is the kernel's job, in software** — there is no hardware protocol that invalidates other cores' TLBs for you.

So the kernel must do a **TLB shootdown**: core 0 sends an **inter-processor interrupt (IPI)** to every other core that might have the stale entry, each of those cores takes the interrupt, flushes the relevant TLB entry (or its whole TLB), and acknowledges — and **core 0 *waits* for all the acknowledgments** before it can safely proceed (e.g., free the physical page). It's a synchronous, cross-core, broadcast-and-wait operation.

```
TLB shootdown (core 0 unmaps a page used by a multithreaded process):
  core 0: update page table, flush own TLB
  core 0: send IPI ──► cores 1,2,3,...,N : "flush this translation!"
  cores 1..N: take interrupt, flush TLB entry, ACK
  core 0: WAIT for all ACKs ────────────────► only now is it safe to free the page
          (every other core was interrupted; core 0 stalled the whole time)
```

The performance consequences are severe and surprising:

- **It scales with core count, the wrong way.** A 64-thread process doing frequent `munmap`/`mprotect`/`madvise` triggers an IPI to *dozens of cores per operation*, each interrupting useful work, with the initiator stalled waiting. This is a genuine, measured scalability wall: memory-mapping-heavy multithreaded workloads (some allocators, some GCs, some JITs that `mprotect` code pages) can spend a shocking fraction of CPU in shootdowns, and it gets *worse* as you add cores. "We scaled to more cores and `munmap`/free got slower" is often this.
- **It's why bulk operations batch.** Allocators and the kernel try hard to *batch* unmappings and *defer* TLB flushes (flush a range once rather than per-page; coalesce shootdowns) precisely because each shootdown is a cross-core IPI storm. It's why `madvise(MADV_FREE)` exists (lazily reclaim pages *without* an immediate shootdown — defer the cost), and why some high-performance systems avoid unmapping memory at all (pool and reuse it) to dodge shootdowns entirely.
- **Mitigations are active research.** Tagged TLBs (PCID/ASID) reduce *full* flushes on context switch but don't eliminate shootdowns for *mapping changes*; there are ongoing kernel efforts (and hardware proposals like ARM's broadcast TLB invalidation, `TLBI`, which does TLB invalidation in hardware without IPIs) to make this cheaper.

The deep lesson, and the reason this belongs at the top of the hard pile: **the TLB is a cache, and like every cache it has a coherence problem — but unlike data caches, its coherence is maintained by the kernel in software via synchronous cross-core interrupts.** Changing a memory mapping is not a cheap local edit; it's a distributed operation whose cost grows with how many cores share the address space. Most engineers never learn this and are baffled when their parallel, mmap-churning workload hits a wall that profiling attributes to "kernel time" — it's shootdowns.

### Layer 8 — The full translation pipeline: page-walk caches and what a "TLB miss" really costs

We said a TLB miss costs "four dependent memory reads." That would be devastatingly slow if it were literally true on every miss, so the hardware has *another* layer of caching most people don't know exists: **paging-structure caches** (also called page-walk caches or MMU caches). The CPU caches not just final translations (the TLB) but the *intermediate* page-table levels — the L4, L3, L2 entries it walked. Because nearby virtual addresses share upper-level page-table entries (the top levels change only every gigabyte or terabyte of address space), a TLB miss for an address near a recently-walked one finds the upper levels already cached and only needs to read the *last* level or two from memory — not all four. So the real cost of a TLB miss ranges from "a few cycles" (upper levels cached, only the leaf missed) to "the full four dependent memory reads" (a cold walk to a far-away region), and *that variance* is why memory-access patterns matter even for translation: locality helps the page-walk caches just as it helps the TLB and the data caches.

This also reframes huge pages (Layer 3) more precisely. A 2 MB huge page isn't just "one TLB entry covering more memory" — it's also a *shorter walk*: because a huge page is mapped at a higher level of the page table (the L2 entry points directly to a 2 MB frame instead of to another table), a TLB miss on a huge page walks *one fewer level*. So huge pages help translation *twice*: bigger TLB reach (fewer misses) *and* cheaper misses (shorter walks). The three-layer reality to carry: every memory access is translated by a pipeline — **TLB (fastest) → page-walk caches (intermediate levels) → full page table in memory (slowest)** — and your locality determines how deep into that pipeline each access falls, exactly mirroring the L1/L2/L3/RAM data hierarchy from the arrays chapter, but for *addresses* instead of *values*.

### Layer 9 — Page reclaim: how the kernel chooses what to evict (LRU, and MGLRU)

When memory fills, the kernel must *reclaim* pages — write dirty ones back, drop clean ones — to make room. Which pages? The ideal is **LRU** (evict the least-recently-used, betting it won't be needed soon), but *true* LRU is impossible: it would require updating a timestamp or moving a list node on *every single memory access*, which is absurdly expensive. So the kernel *approximates* LRU, and how it does so determines whether it evicts cold junk (good) or your hot working set (catastrophic — instant thrashing).

The classic Linux approach uses **two LRU lists per memory zone: active and inactive.** New pages start on the inactive list. The trick that approximates "recently used" without per-access tracking is the **accessed bit**: each PTE has a hardware bit the CPU sets when the page is touched. The kernel periodically scans, and a page on the inactive list whose accessed bit is set (it was used recently) gets *promoted* to the active list and its bit cleared; a page whose bit stays clear (untouched) drifts toward eviction. This is the **second-chance / clock algorithm**: pages get a "second chance" if they were accessed since the last scan, approximating LRU with just a periodic bit-check instead of per-access bookkeeping. Reclaim pulls victims from the *inactive* list; the active list holds the hot working set, protected.

A subtle, crucial refinement: **refault detection.** If the kernel evicts a page and then that page faults back in *almost immediately*, eviction was a mistake — the page was actually hot, and the kernel is *thrashing* (evicting pages it immediately needs again). Modern kernels track eviction "distance" and detect refaults to recognize thrashing and adjust (and to feed PSI, Layer 13). This is the difference between "reclaim is working" and "the system is melting because it keeps evicting its own working set."

The newest chapter, in kernels 6.1+: **MGLRU (Multi-Gen LRU).** The two-list active/inactive scheme is crude — only two "ages" of page. MGLRU generalizes it to *multiple generations*: pages are aged through several generations based on access recency, giving the kernel a finer-grained, more accurate picture of which pages are truly cold, and it scans more cheaply (page-table-driven rather than scanning every page). The result is markedly better reclaim decisions under pressure — fewer wrong evictions, less thrashing, better tail latency and throughput on memory-constrained systems (which is most cloud/container deployments). If you run a recent kernel under memory pressure, MGLRU is quietly making better choices about what to throw away. The lesson: *true LRU is unaffordable, so the entire game is approximating it well* — accessed bits, second-chance, refault detection, multiple generations — and the quality of that approximation directly determines whether memory pressure degrades you gracefully or collapses you into a thrash.

### Layer 10 — Reverse mapping: finding every PTE that points to a page

Reclaim (Layer 9) and page migration (NUMA balancing, COW, compaction) all need to do something that sounds simple and is actually hard: to evict or move a *physical* page, the kernel must find and update *every page table entry that points to it* — and a single physical page can be mapped by *many* processes at once (shared libraries, the page cache, COW-shared pages after fork). The forward direction is easy (a PTE tells you its physical frame); the *reverse* — given a physical page, which PTEs across which processes map it? — requires a dedicated data structure: **reverse mapping (rmap).**

For file-backed pages, rmap works through the file's address-space object and an interval tree of the VMAs (memory regions) that map it. For anonymous pages (heap/stack, COW-shared after fork), Linux uses **anon_vma** chains linking a page back to all the VMAs that might map it. With rmap, to reclaim a physical page the kernel can walk every PTE referencing it, unmap each, flush the relevant TLBs (Layer 7 — *this* is often what triggers shootdowns during reclaim!), and only then free or migrate the page. Why this matters to you: rmap is the machinery that makes page *migration* possible at all — NUMA balancing moving a page to another node (chapter 02), compaction relocating pages to defragment, COW breaking a shared page — *all* require finding every mapper of a physical page, and rmap is how. It's also historically a source of subtle complexity and overhead (the anon_vma chains can get large in fork-heavy workloads), and it's the hidden reason some memory operations are more expensive than they look: "move this page" secretly means "find and update all N mappings of it, with TLB shootdowns for each." The forward map (virtual→physical) is what your program uses; the reverse map (physical→all virtuals) is what the *kernel* needs to manage memory, and it's quietly essential to everything in this Part.

### Layer 11 — Transparent huge pages and khugepaged: the latency story in full

Layer 3 said THP "can cause stalls." Here's the actual mechanism, because it's a real production foot-gun. THP gives huge pages *transparently*, two ways. First, on a page fault for an eligible region, the kernel may *immediately* try to allocate a 2 MB huge page instead of a 4 KB one. Second, a background kernel thread, **khugepaged**, periodically *scans* process memory looking for 512 contiguous 4 KB pages it can *collapse* into one 2 MB huge page (and conversely splits huge pages when needed). The promise: huge-page TLB benefits (Layers 3, 8) with zero application changes.

The problem is *where the 2 MB of physically contiguous memory comes from.* Recall the buddy allocator (chapter 02) and physical fragmentation: on a long-running, busy system, finding a free, contiguous, aligned 2 MB run can require **direct compaction** — the kernel synchronously migrating other pages around (using rmap, Layer 10, with TLB shootdowns, Layer 7) to manufacture a contiguous region *right at the moment of the page fault*, while your thread waits. That synchronous compaction is the THP latency spike: an innocent memory access triggers a fault that triggers compaction that migrates dozens of pages with cross-core shootdowns — hundreds of microseconds to milliseconds of stall, at unpredictable times. And khugepaged itself, scanning and collapsing in the background, periodically takes locks and does work that can briefly perturb the application.

This is why databases and latency-sensitive services (Redis, MongoDB, many JVMs) explicitly recommend setting THP to `madvise` (only use huge pages where the application *asked* via `madvise(MADV_HUGEPAGE)`) or disabling it (`never`) — they'd rather forgo the TLB benefit than eat unpredictable compaction stalls on the request path. The knobs (`/sys/kernel/mm/transparent_hugepage/enabled` and `/defrag`) let you choose *always* (aggressive, stall-prone), *madvise* (opt-in, the usual sweet spot), or *never*. The deeper lesson ties three chapters together: THP's stalls are the buddy allocator's physical fragmentation (chapter 02) forcing memory compaction (this chapter) with TLB shootdowns (Layer 7) — a perfect example of how the "hard internals" interlock, and why a feature that's pure win on paper (bigger TLB reach) can be a tail-latency disaster in practice. The right default is *opt-in*, not *automatic*.

### Layer 12 — Copy-on-write's sharp edges: from elegant trick to kernel exploit

COW (Layer 6) is beautiful, but its implementation — a page shared read-only until a write faults and triggers a private copy — is a delicate *race* between the fault handler, the page-table state, and other operations on the same memory, and that race has been one of the most fertile sources of serious Linux vulnerabilities. The most famous is **Dirty COW (CVE-2016-5195, 2016)**, a privilege-escalation bug that lived in the kernel for *nine years* and affected essentially every Linux device on Earth (servers, Android phones, embedded).

The essence: COW handling a write to a read-only shared page involves multiple steps (detect the write fault, allocate a copy, update the PTE to point at the private copy). Dirty COW exploited a race between a thread repeatedly writing to a COW mapping of a read-only file (via `/proc/self/mem`, which bypasses some checks) and another thread repeatedly calling `madvise(MADV_DONTNEED)` to discard the private copy. With the right timing, the kernel could be tricked into writing the modification *back to the original, read-only, shared page* — i.e., into a file the attacker had no permission to write, like `/etc/passwd` or a setuid binary. A race in the COW fault path became arbitrary write-to-read-only-file, became root. The fix reworked how COW tracks whether a page has been privately copied (the PTE "dirty" and a new "soft-dirty"/pin-aware tracking). A *successor* class of bugs ("Dirty Pipe," 2022, and the COW-vs-GUP/pinning issues) showed the COW path is *still* subtle: when a page is COW-shared *and* simultaneously pinned for DMA or long-term access (e.g., by a driver doing `get_user_pages`), deciding who sees the post-fork writes is genuinely hard, and getting it wrong is either a data-corruption bug or a security hole. Linux eventually changed `fork()`'s COW semantics for pinned pages to resolve this (the "COW after fork" rework around 2020–2022).

The lesson is twofold. Practically: COW is not free of edge cases — it interacts dangerously with anything that takes a *long-lived reference* to a page's physical address (DMA, RDMA, io_uring fixed buffers, `get_user_pages`), because COW assumes it can transparently swap the physical page under you, and a pinned reference breaks that assumption. Philosophically: *the most elegant optimizations have the sharpest edges*, because their elegance comes from deferring and sharing work in ways that create subtle temporal windows — and a deferred, shared, racy window between privilege boundaries is exactly what an attacker looks for. The page-fault path's beauty (lazy copies, shared frames) and its danger (races at the moment of divergence) are the same thing.

### Layer 13 — Memory pressure, swap, and cgroup control: seeing it coming, and bounding the blast

Finally, the modern machinery for *managing* pressure rather than just suffering it. Three pieces every container-era engineer needs.

**Swap, properly understood.** Swap isn't "extra slow RAM you should disable" (a common myth). It's a *release valve*: under pressure, the kernel can evict cold *anonymous* pages (heap that hasn't been touched in ages) to swap, freeing RAM for hot pages and the page cache. The `vm.swappiness` knob (0–100, default ~60) tunes the kernel's *preference* between reclaiming page-cache (file) pages versus swapping anonymous pages — low swappiness favors dropping file cache, high favors swapping anon. Disabling swap entirely doesn't prevent pressure; it just removes the gentle option, so the kernel goes straight from "fine" to "OOM kill" with no graceful middle (which is why some argue for a *little* swap even on big-RAM boxes — a soft landing). Modern variants: **zswap/zram** keep swapped pages *compressed in RAM* (trading CPU for the avoidance of disk I/O), a popular middle ground — swap that's ~10× faster than disk because it never leaves memory.

**PSI — Pressure Stall Information.** The old signals for memory pressure were terrible: by the time you saw swapping or an OOM kill, it was too late. **PSI** (`/proc/pressure/{cpu,memory,io}`, kernel 4.20+) directly measures *the fraction of time tasks were stalled waiting* on each resource — for memory, the percentage of wall time something was blocked on page faults / reclaim. This is a *leading* indicator: PSI memory pressure climbing means the system is *starting* to struggle *before* it thrashes or OOMs, giving you (or an orchestrator) time to react — shed load, scale out, or kill gracefully. Systems like Facebook's **oomd** (and systemd-oomd) use PSI to make *proactive*, graceful kill decisions *before* the kernel's last-resort OOM killer fires with its blunt heuristics. PSI is the single best signal for "is this box under memory pressure," and most people still watch the wrong metrics (free memory, which is meaningless given caching and overcommit).

**cgroup memory control (v2).** Containers don't just *get* a memory limit — cgroup v2 offers a *graduated* set of controls that are far better than a single hard cap: **`memory.max`** (the hard limit — exceed it and the cgroup's processes are OOM-killed), **`memory.high`** (a *soft* limit — exceeding it doesn't kill, it *throttles* the cgroup by aggressively reclaiming its memory and slowing its allocations, applying back-pressure so it has a chance to recover *before* hitting `memory.max`), and **`memory.low`/`memory.min`** (protection — memory the cgroup is *guaranteed* to keep under pressure, so a critical service isn't reclaimed to death by a greedy neighbor). The right container memory configuration uses `memory.high` for graceful throttling plus `memory.max` as the hard backstop, and watches the cgroup's *own* PSI — not the host's free memory — to know when *it* is under pressure. This is the memory analogue of chapter 04's CPU `requests`/`limits`: a soft, throttling pressure mechanism plus a hard cap, and using only the hard cap (the Kubernetes default) means you get the OOM-kill cliff with no graceful degradation. The capstone insight of the whole chapter: virtual memory's lies (overcommit, demand paging) make "how much memory am I using" almost meaningless, so the modern discipline is to measure *pressure* (PSI), provide *graceful back-pressure* (`memory.high`), and *protect* what matters (`memory.low`) — managing the illusion rather than pretending it's real RAM.

---

## A Ladder From L1 to Principal

- **L1 / new grad:** Programs use virtual addresses mapped to physical RAM; the OS handles translation; a page is the unit; `malloc` gives memory.
- **L3–L4 / solid engineer:** You understand page tables, the TLB, page faults (minor vs. major orders of magnitude apart), and demand paging (memory is lazy; first-touch is slow). You connect swapping/major faults to latency cliffs.
- **Senior:** You reason about TLB reach and huge pages, mmap/COW/page-cache as one mechanism, and watch major-fault rate. You know overcommit and the OOM killer, and why container limits bite below host RAM.
- **Staff:** You understand TLB shootdowns as a multicore scaling wall, how reclaim approximates LRU (and that bad reclaim thrashes), why THP causes compaction stalls, and you tune swap/swappiness, THP mode, and watch PSI rather than free memory.
- **Principal:** You treat the virtual memory system as a design surface — access patterns that respect TLB/page-walk-cache locality, avoiding shootdown-heavy mapping churn, configuring graduated cgroup memory controls (`memory.high`/`max`/`low`) with PSI-driven proactive management, and reasoning about COW's interaction with pinning/DMA. You predict the latency cliffs before traffic finds them, and you know "it's all page tables and faults — and their coherence, reclaim, and pressure are the hard parts."

One idea climbing: *every address is a lie the hardware resolves per-page; a not-present entry lets the kernel make memory mean anything (copies, files, caches, disk); and the genuinely hard parts are that this map is shared across cores (shootdowns), must be reclaimed by approximating LRU, must be reverse-mappable to be managed, and must be measured by pressure rather than trusted as "free RAM."*

## Complexity Analysis

| Operation | Cost | What's happening |
|-----------|------|------------------|
| TLB hit | ~1 cycle | Cached translation; the common case, ~free |
| TLB miss, upper levels cached | ~few–tens of cycles | Page-walk caches supply L4–L2; read only the leaf |
| TLB miss, cold walk | ~4 dependent memory reads | Full multi-level traversal |
| Minor page fault | ~1 µs | Page available without disk (first-touch, cache hit, COW) |
| Major page fault | ~100 µs – 10 ms | Disk fetch (swap-in / file read); thread blocks |
| TLB shootdown | IPI to N cores + wait | Software TLB coherence; **scales badly with core count** |
| Page reclaim (per page) | ~µs + possible writeback | Approximate-LRU victim selection; rmap + TLB flush |
| Huge page (2 MB) | 1 TLB entry, shorter walk | 512× reach *and* one fewer page-table level |
| THP fault needing compaction | ~100 µs – ms stall | Synchronous page migration to form a contiguous 2 MB run |

The whole story is in the gap between row 1 (free) and the rest, plus the realization that *changing* the map (shootdowns) and *reclaiming* it (LRU + rmap) are their own costs the single-core view hides entirely.

## War Stories (the shape of the bug in the wild)

- **The latency cliff that was swap.** A service's p99 jumped from 5 ms to 800 ms with no code change and flat CPU. The missed signal: the **major page fault** rate spiked — the working set had been pushed into swap, turning ns accesses into ms disk faults. Fix was memory, found only by watching major faults (and, better, PSI).
- **The munmap wall on 64 cores.** A multithreaded service doing heavy `mmap`/`munmap` churn scaled fine to 16 cores, then *regressed* past 32. Profiling blamed "kernel time"; the real cause was **TLB shootdown** IPI storms — every unmap interrupting dozens of cores, the initiator stalling for ACKs (Layer 7). Fix: pool and reuse mappings instead of unmapping; use `MADV_FREE`.
- **The THP compaction spikes.** A Redis instance had periodic, unexplained latency spikes correlated with nothing in the workload. THP was synchronously compacting memory to form 2 MB pages on the fault path (Layers 7, 10, 11). Setting THP to `madvise`/`never` (per Redis docs) flattened the tail.
- **The OOM cliff with no warning.** A container with swap disabled and only `memory.max` set went from healthy to OOM-killed instantly under a load spike — no graceful degradation (Layer 13). Adding `memory.high` (throttle before kill) and watching the cgroup's PSI gave early back-pressure and a soft landing.
- **The nine-year root exploit.** Dirty COW: a race in the copy-on-write fault path let an unprivileged user write to read-only files and escalate to root (Layer 12). A reminder that the most elegant memory trick had the sharpest security edge, and that COW + long-lived page references (pinning/DMA) is a genuinely hard correctness problem.

## Key Takeaways

1. **Every address is virtual,** translated per-page by the MMU on every access — the illusion behind isolation, contiguous-looking address spaces, and allocating more than you have. A virtual address is a page number + offset, so *everything* happens in page units.
2. **Translation is a pipeline — TLB → page-walk caches → full page table** — and locality decides how deep each access falls, mirroring the data-cache hierarchy but for addresses. **Huge pages help twice:** more TLB reach *and* shorter walks. The TLB flush on context switch is why post-switch code runs cold.
3. **A page fault is the kernel taking over an access.** Minor (~1 µs) routine; **major (~ms, disk) is the latency cliff** and the signature of swapping — watch the major-fault rate (and PSI).
4. **TLB coherence is the kernel's job, in software:** changing a mapping forces a **TLB shootdown** — synchronous cross-core IPIs that the initiator waits on — which **scales badly with core count** and makes mmap-churning multithreaded workloads hit a wall. Pool mappings; batch/defer flushes (`MADV_FREE`).
5. **True LRU is unaffordable, so reclaim approximates it** — active/inactive lists, the accessed-bit second-chance/clock, refault detection, and now **MGLRU's multiple generations.** Bad approximation = thrashing (evicting your own hot working set). **rmap** (reverse mapping) is what lets the kernel find every PTE for a page to reclaim or migrate it.
6. **THP's stalls = physical fragmentation (buddy allocator) forcing synchronous compaction (page migration + shootdowns) on the fault path.** The right default is opt-in (`madvise`), not automatic (`always`) — a feature that's pure win on paper and a tail-latency disaster in practice.
7. **Copy-on-write has sharp edges** — Dirty COW was a nine-year root exploit from a race in the fault path, and COW interacts dangerously with *pinned* pages (DMA/RDMA/io_uring/`get_user_pages`) because it assumes it can swap the physical page under you. Elegant deferred-and-shared optimizations create exactly the temporal windows attackers exploit.
8. **Manage the illusion, don't trust "free RAM."** Overcommit and demand paging make usage figures meaningless; the modern discipline is **measure pressure (PSI)**, provide **graceful back-pressure (`memory.high`)**, **protect critical memory (`memory.low`)**, and keep **swap** (even compressed, zram/zswap) as a soft landing instead of an OOM cliff.

## Related Modules

- `01-processes-and-threads.md` — the TLB flush on context switch, copy-on-write `fork()` and its pinning hazards, address-space isolation, and the cache-coherence analogy (MESI for data, shootdowns for translations)
- `02-memory-management.md` — the buddy allocator (physical fragmentation behind THP stalls), demand paging, overcommit, the OOM killer, NUMA migration via rmap, and PSI
- `03-io-and-syscalls.md` — the page cache, mmap (and its SIGBUS/pinning hazards), dirty-page writeback, and the major faults a cold read triggers
- `04-scheduling.md` — NUMA balancing migrating pages, the TLB flush cost of context switches, and how memory pressure compounds scheduling latency
- `../02-data-structures-and-algorithms/01-arrays-and-memory-layout.md` — the cache/TLB locality argument and "slow first run = demand paging," from first principles
