# Virtual Memory

## Problem

Run this on your laptop right now: open two programs, attach a debugger to each, and ask both "what's the address of your main global variable?" There's a real chance they answer with the *same number* — `0x55…` something, identical. Two programs, same address, both running, neither crashing, neither reading the other's data. How? If memory is "a street of numbered houses" (the picture from the arrays chapter), how can two programs both live at house #93,824,992,236,885 simultaneously without one stomping the other?

The answer is the single most important illusion in computing, and the fact that you've probably never had to think about it is the proof of how well it works: **the addresses your program uses are fake.** Every address in your code is a *virtual* address — a name your process invented — and a piece of hardware translates it, on *every single memory access*, into a different *physical* address in the actual RAM chips. Your process believes it owns a vast, private, contiguous expanse of memory starting at zero. It owns no such thing. It owns a sparse scattering of 4 KB physical pages wherever the OS found room, stitched together by a translation table into the *appearance* of a clean private address space. The two programs share an address because that address is a lie they each tell privately, and the hardware resolves each lie to different physical RAM.

This isn't academic trivia — it's the load-bearing mechanism under a startling amount of what you do daily. Process isolation (chapter 01's "a bug in one PostgreSQL backend can't corrupt another")? That's virtual memory — each process's translation table simply has no entry pointing at another's pages. `mmap`-ing a file and reading it as if it were an array? Virtual memory. Copy-on-write `fork()`? Virtual memory. The page cache that makes your second read of a file 1000× faster than the first? Virtual memory. The mysterious truth that you can `malloc` more memory than you have RAM and it *works*? Virtual memory. The equally mysterious truth that your container got OOM-killed while `free` showed gigabytes available? Also virtual memory, and its overcommit gamble.

So this chapter builds that illusion from the ground up: how an address gets translated, why that translation is fast (the TLB) and what happens when it's slow, what a "page fault" actually is (and why one kind is fine and another kind is a disaster), and how a dozen features you use without thinking are all the same translation machinery wearing different hats. Once you can *see* the translation happening, a whole category of production mysteries — latency spikes, OOM kills, slow first-touches, huge-page tuning — stops being mysterious.

## Why It Matters (Latency, Throughput, Cost)

**The TLB is a tiny cache whose misses you pay for invisibly.** Translation isn't free — to turn a virtual address into a physical one, the CPU consults a page table, which itself lives in memory. Doing that on every access would be insane, so the CPU caches recent translations in the **TLB** (Translation Lookaside Buffer). The TLB is *small* — a few hundred to ~1500 entries, each covering one 4 KB page, so it "sees" only a handful of megabytes at once. Sweep through a data structure bigger than that with poor locality and you start *missing* the TLB: now each access pays for a **page-table walk** (several dependent memory reads) *before* it even fetches your data. On large, scattered workloads, TLB misses alone can be 10–30% of runtime — a cost that never appears in your code, your algorithm's Big-O, or a naïve profiler. It's why huge pages exist (more on that below) and why "why is my big hash table so slow?" sometimes has nothing to do with the hash table.

**A page fault is a 100,000× latency cliff hiding behind a normal-looking memory access.** When you read an address whose page isn't currently backed by physical memory, the access *traps into the kernel* — a **page fault**. A *minor* fault (the page is in RAM but not yet mapped into your table, e.g. first touch of freshly-allocated memory, or a page already in the page cache) costs ~1 µs. A *major* fault (the page must be fetched from disk — it was swapped out, or it's a file page not yet read) costs ~100 µs to many milliseconds. The same C statement `x = arr[i]` is either a 1 ns L1 hit or a 10 ms disk fault depending on the page's state, and nothing in the syntax tells you which. This is why a service that quietly started swapping doesn't get "a bit slower" — it falls off a cliff, because random memory accesses that were nanoseconds are now milliseconds.

**Overcommit lets you allocate memory you don't have — until the bill comes due as an OOM kill.** Because pages are only backed by physical RAM when first *written*, Linux by default lets processes `malloc`/`mmap` far more than physically exists (it bets you won't touch it all). Great for fork-heavy and sparsely-allocating workloads. The catch: when processes *do* collectively touch more than exists, there's no more RAM to hand out, and the kernel's **OOM killer** picks a process and terminates it. This is why your container can be OOM-killed while monitoring shows "free memory" — allocation succeeded (virtual), the kill happened on *touch* (physical), and the accounting is for committed pages, not your intuition. Understanding overcommit is understanding why container memory limits and OOM scores matter.

## Mental Model

Hold two pictures at once. The one your program sees, and the one that's real.

```
WHAT YOUR PROCESS BELIEVES:            WHAT'S ACTUALLY TRUE:
one huge, private, contiguous          a sparse scatter of 4 KB physical pages,
address space from 0 → 2^48            wherever the OS found room, plus a
                                       translation table faking the contiguity

  virtual                                physical RAM (frames)
  ┌───────────┐ 0x0000                   ┌──────┐ frame 7   ← virt page 2 lives here
  │  (unmapped)                          ├──────┤ frame 19  ← virt page 0 lives here
  │  code      │ page 0  ──────┐         ├──────┤ frame 3   ← someone else's
  │  data      │ page 1  ───┐  │         ├──────┤ frame 41  ← virt page 1 lives here
  │  heap ───► │ page 2  ─┐ │  └────────►├──────┤ ...
  │            │          │ └───────────►├──────┤
  │  ...gap... │          └─────────────►├──────┤
  │  stack ◄── │ page N                  └──────┘
  └───────────┘
       │  every access: MMU translates virtual page → physical frame
       ▼  via the page table, cached in the TLB
```

The core operation, the one thing to burn in: **a virtual address splits into a page number and an offset.** The page number is looked up in the page table to find a physical frame; the offset is added unchanged. `virtual 0x1234` with 4 KB pages = page `0x1` + offset `0x234`; if page 1 maps to frame 41, the physical address is `frame_41_base + 0x234`. Translation happens at *page granularity* — the offset within a page is identity — which is why everything in virtual memory happens in 4 KB units: faults, sharing, protection, swapping, all per-page.

And the second idea that makes the whole system tractable: **mappings are lazy.** A page table entry doesn't have to point at real RAM. It can say "not present" (touch it → page fault → kernel decides what to do), "present and read-only" (the COW trick), "on disk in swap," or "this is a file, fault it in from the filesystem." The page table isn't just a translation map — it's a set of *instructions to the kernel* about what to do when you touch each page. That single indirection is the seed from which demand paging, COW, mmap, swap, and the page cache all grow.

## Underlying Theory

We'll build the illusion in layers: the translation itself, the hardware that makes it fast, the fault that makes it flexible, and then the parade of features that are all secretly this same machine.

### Layer 1 — The page table: translation as a lookup

The simplest possible translation is a big array: one entry per virtual page, holding the physical frame it maps to. The problem is size. A 48-bit address space with 4 KB pages has 2³⁶ ≈ 68 billion pages; a flat table of that would need hundreds of gigabytes *per process* — absurd, especially since processes use a tiny, sparse fraction of their address space.

The fix is a **multi-level page table** — a tree. Instead of one giant array, you have a small top-level table whose entries point to second-level tables, whose entries point to third-level tables, and so on (x86-64 uses 4 levels; newer chips offer 5). The win is sparsity: regions of the address space you never use simply have no lower-level tables — a null pointer at a high level prunes an entire subtree of would-be entries. A process using a few megabytes needs only a handful of small tables, not a map of all 68 billion potential pages.

```
virtual address (48 bits) = [ L4 idx | L3 idx | L2 idx | L1 idx | offset (12 bits) ]
                                │        │        │        │
   CR3 register ─► L4 table ────┘        │        │        │
                     entry ─► L3 table ──┘        │        │
                                entry ─► L2 table─┘        │
                                           entry ─► L1 table─► physical frame + flags
```

The cost is equally clear: translating *one* address now means walking *four* tables — **four dependent memory reads** before you reach your actual data. Each read depends on the previous (you can't fetch the L3 table until the L4 entry tells you where it is), so they can't be parallelized. A raw page-table walk is brutally slow. Which is exactly why the next layer exists.

### Layer 2 — The TLB: caching translations so the common case is free

If every memory access required a four-level page-table walk, computers would be unusable. So the CPU keeps a dedicated cache *just for translations*: the **TLB**. It maps virtual page → physical frame for recently-used pages, sitting right next to the core, answering in ~1 cycle. The overwhelming majority of accesses hit the TLB, the translation is effectively free, and the page table is never touched. The page-table walk only happens on a TLB *miss* — and then the result is cached in the TLB for next time.

This reframes performance in a way that connects straight back to the arrays chapter. There, "locality" meant "keep your data in cache." Here there's a *second*, parallel locality: **keep your translations in the TLB.** Sequential access wins twice — your data is in cache *and* your translations are in the TLB (consecutive addresses share a page, so one TLB entry covers 4 KB of accesses). Scattered access loses twice — cache misses *and* TLB misses, the latter triggering full page-table walks. This is why pointer-chasing through a huge structure is even worse than the cache story alone suggests: you're missing two caches, and one of the misses costs four dependent memory reads.

Two more facts with real consequences. First, **a context switch to a different process invalidates the TLB** (the new process has different translations) — this is the "TLB flush" from chapter 01, and it's why the first hundred microseconds after a process switch run slow while the TLB refills. (Modern CPUs soften this with "tagged" TLB entries — PCID/ASID — that tag each entry with an address-space ID so a switch needn't flush everything.) Second, the lever you actually reach for: **huge pages.**

### Layer 3 — Huge pages: making each TLB entry cover more

The TLB has a fixed, small number of entries, and each normally covers one 4 KB page — so the total memory the TLB can "see" (its *reach*) is tiny, maybe a few megabytes. A database or JVM with a multi-gigabyte heap blows past that reach instantly and lives in a state of chronic TLB misses, paying page-table walks across its working set.

**Huge pages** attack this directly: use 2 MB pages (or 1 GB pages) instead of 4 KB. Now a single TLB entry covers 2 MB — *512× more memory per entry* — so the same small TLB suddenly has gigabytes of reach, and the chronic misses largely vanish. The trade-offs: huge pages are coarser, so they can waste memory (a barely-used 2 MB page still consumes 2 MB) and are harder to allocate when memory is fragmented. Linux offers explicit huge pages (reserved up front) and Transparent Huge Pages (THP, the kernel promotes regions automatically) — and THP is famously a double-edged sword, sometimes causing latency stalls from background defragmentation, which is why databases like MongoDB and Redis often *recommend disabling THP* and managing huge pages explicitly. The point: when you see page-table-walk cost dominating a flame graph on a big-heap service, huge pages are the lever, and now you know *why* they help (TLB reach), not just that they do.

### Layer 4 — The page fault: where the kernel takes over

Here's the hinge of the entire system. A page table entry can be marked **"not present."** When the CPU translates an address and finds a not-present entry, it can't proceed — so it *traps into the kernel*: a **page fault**. The faulting instruction freezes, control jumps to the kernel's fault handler, the kernel decides what should be there and makes it so, then resumes your instruction as if nothing happened. From your program's perspective, `x = arr[i]` just took a while. Underneath, the kernel did real work.

What the kernel does — and how much it costs — depends on *why* the page wasn't present:

- **Minor fault (~1 µs):** the page is available without disk I/O. Maybe it's freshly-allocated memory being touched for the first time (the kernel grabs a zeroed frame and maps it). Maybe it's a file page that's already sitting in the page cache from an earlier read (just map it in). Cheap-ish, but not free — and at scale, *lots* of minor faults add up.
- **Major fault (~100 µs to 10 ms):** the page must come from disk. It was swapped out under memory pressure, or it's a file page not yet read. Now you pay storage latency, and the faulting thread *blocks* the whole time. This is the latency cliff. A handful of major faults per request is fine; a service that started swapping turns major faults into the common case and its latency detonates.

This is why the minor-vs-major distinction is one of the most useful things on a performance dashboard. `vmstat`, `/proc/<pid>/stat`, and `perf` all expose major fault counts; a climbing major-fault rate is the unambiguous signature of memory pressure / swapping, and it explains tail-latency spikes that CPU and disk-throughput graphs miss.

### Layer 5 — Demand paging and overcommit: laziness as policy

Layer 4's not-present trick enables the system's defining laziness: **nothing is backed by physical RAM until you actually touch it.** When you `malloc` 1 GB, the kernel doesn't find 1 GB of RAM — it just extends your virtual address space and marks the pages not-present. Physical frames get attached one at a time, by minor fault, as you *write* to them. Allocate a giant array and never touch half of it, and that half never costs a byte of RAM. This is **demand paging**, and it's why allocation is fast and cheap regardless of size — the cost is deferred to first touch (which is also why benchmark first-passes are slow: they're paying the demand-paging faults the warm pass already settled — the exact "mysterious first run" from the arrays chapter, now fully explained).

Demand paging makes **overcommit** natural: since most allocated memory is never simultaneously resident, the kernel hands out more virtual memory than physical RAM exists, betting on the gap. Usually a great bet — fork-heavy workloads (COW means the child "has" a full copy it mostly never writes), sparse data structures, lazily-touched buffers. But it's still a *bet*, and when reality calls it — processes collectively touch more than RAM holds — the kernel is out of frames with no graceful option. It invokes the **OOM killer**, scores each process (roughly by memory footprint, adjustable via `oom_score_adj`), and kills one. This is the resolution of the "OOM-killed with free memory showing" paradox: the allocation succeeded virtually long ago; the reckoning came at touch-time; and in containers, the cgroup memory limit (chapter 02) makes the ceiling much lower than the host's free RAM suggests.

### Layer 6 — The same machine, wearing every hat

Now the payoff: a whole catalog of features you use independently are *all the page-table-plus-fault mechanism* in disguise. Seeing them as one thing is the senior-to-staff jump.

- **Process isolation** — each process has its own page table, so its virtual addresses simply *cannot name* another process's frames. The wall between PostgreSQL backends is the absence of an entry. Isolation isn't enforced by checking; it's enforced by the map not existing.
- **Copy-on-write `fork()`** (chapter 01) — child and parent page tables point at the same frames, marked read-only; a write faults, the kernel copies one page and remaps it private. COW *is* page faults plus shared frames.
- **`mmap` (memory-mapped files)** — map a file's bytes into your address space; reads/writes become ordinary memory accesses, and the kernel faults pages in from the file on demand and writes dirty ones back. This is how databases (LMDB, parts of SQLite, MongoDB's old MMAPv1) read data files "as memory," and how shared libraries are loaded once and mapped into every process. No `read()` syscalls in the hot path — just faults.
- **The page cache** — file pages, once read, stay resident as physical frames and are mapped into anyone who reads that file. Your second read of a file is a minor fault (or no fault) instead of disk I/O — a ~1000× speedup that's entirely virtual-memory bookkeeping. The page cache is *why* "the OS will cache it for you" is true.
- **Swap** — under memory pressure, the kernel evicts cold pages to disk and marks them not-present; touching them later triggers a major fault that pages them back in. Swap is demand paging pointed at disk instead of fresh frames.

Five "different" features, one mechanism. When you internalize that mmap, COW, the page cache, swap, and isolation are all *page tables with not-present entries and a kernel fault handler*, you stop memorizing them separately and start *deriving* their behavior — including their failure modes.

## A Ladder From L1 to Principal

- **L1 / new grad:** Programs use virtual addresses that map to physical RAM; the OS and hardware handle translation; a "page" is the unit. You know `malloc` gives you memory and the OS manages it.
- **L3–L4 / solid engineer:** You understand page tables and the TLB, what a page fault is, and that minor vs. major faults differ by orders of magnitude. You know demand paging means memory is lazy and first-touch is slow.
- **Senior:** You reason about TLB reach and reach for huge pages on big-heap services; you connect swapping/major faults to tail-latency cliffs; you understand mmap, COW, and the page cache as the same machinery and use them deliberately.
- **Staff:** You diagnose memory-pressure incidents from major-fault rates, tune THP/huge pages and swappiness, reason about overcommit and the OOM killer in container limits, and design data access (mmap vs. read, sequential vs. random) around TLB and page-cache behavior.
- **Principal:** You treat the virtual memory system as a design surface — choosing storage/access patterns that respect TLB reach and page-cache locality, setting isolation and overcommit policy across a fleet, and predicting where the latency cliffs are before traffic finds them. "It's all page tables and faults" is a tool you compute with.

One idea, climbing: *every address is a lie the hardware resolves per-page, and a not-present entry is a hook that lets the kernel make memory mean whatever it needs to — copies, files, caches, disk.*

## Complexity Analysis

| Operation | Cost | What's happening |
|-----------|------|------------------|
| TLB hit (translation) | ~1 cycle | Cached virtual→physical; the common case, effectively free |
| TLB miss → page-table walk | ~4 dependent memory reads | Multi-level table traversal; ~tens–hundreds of ns |
| Minor page fault | ~1 µs | Page available without disk (first-touch, page-cache hit, COW copy) |
| Major page fault | ~100 µs – 10 ms | Page fetched from disk (swap-in or file read); thread blocks |
| `malloc` of N bytes | O(1)-ish, no RAM yet | Extends virtual space; physical frames attached lazily on touch |
| First write after `fork` (COW) | ~1 µs | Minor fault → copy one 4 KB page → remap private |
| Huge page (2 MB) translation | 1 TLB entry covers 512× | Fewer TLB entries needed → far fewer walks on big heaps |

The whole performance story is in the gap between row 1 (free) and the rest. Virtual memory is fast because the TLB makes translation free *almost* always; it falls off cliffs exactly when it can't — TLB misses, then minor faults, then the major-fault precipice.

## War Stories (the shape of the bug in the wild)

- **The latency cliff that was swap.** A service's p99 went from 5 ms to 800 ms with no code change and no CPU increase. The signal everyone missed: the **major page fault rate** had spiked. Memory pressure had pushed the working set into swap, turning nanosecond accesses into millisecond disk faults. The fix was memory, not code — but you only find it if you're watching major faults.
- **The big hash table that TLB-thrashed.** A multi-gigabyte in-memory index was inexplicably slow despite great algorithmic complexity and warm caches. `perf` showed the time was in *page-table walks* — the working set vastly exceeded TLB reach, so nearly every random lookup missed the TLB. Enabling huge pages (2 MB) restored TLB reach and cut latency substantially. The algorithm was never the problem; translation was.
- **OOM-killed with memory to spare.** A container kept getting killed while host `free` showed gigabytes available. Overcommit had let the process allocate freely (virtual), the cgroup memory limit was far below host RAM, and the kill fired when *touched* pages crossed the cgroup ceiling. The "free memory" everyone pointed at belonged to the host, not the cgroup.
- **THP tail-latency stalls.** A Redis instance showed periodic latency spikes that correlated with nothing in the workload. Transparent Huge Pages' background defragmentation (`khugepaged`) was stalling the process to coalesce pages. Disabling THP — exactly as Redis's own docs recommend — flattened the tail. Sometimes the huge-page lever is the one you pull *out*.

## Key Takeaways

1. **Every address your program uses is virtual** — translated to physical RAM per-page by the MMU on every access. This illusion is what gives you process isolation, contiguous-looking address spaces, and the ability to allocate more than you have.
2. **A virtual address is a page number plus an offset**, and translation happens at 4 KB page granularity — which is why *everything* in virtual memory (faults, sharing, protection, swap) happens in page units.
3. **Multi-level page tables make translation sparse but slow** (4 dependent reads per walk); **the TLB makes the common case free** by caching translations. Performance has *two* localities now: keep data in cache *and* translations in the TLB.
4. **Huge pages exist to extend TLB reach** — one 2 MB entry covers 512× the memory — which is the lever for big-heap services drowning in page-table walks (but THP can backfire; sometimes you disable it).
5. **A page fault is the kernel taking over a memory access.** Minor faults (~1 µs, no disk) are routine; **major faults (~ms, disk) are a latency cliff** and the unambiguous signature of swapping/memory pressure. Watch the major-fault rate.
6. **Memory is lazy (demand paging):** allocation is cheap and backed by nothing until first *touch*, which enables **overcommit** — and overcommit is a bet the **OOM killer** settles when touched pages exceed physical (or cgroup) RAM. This is why you can be OOM-killed with "free" memory showing.
7. **mmap, copy-on-write, the page cache, swap, and process isolation are all the same mechanism** — page tables with not-present entries plus a kernel fault handler. Seeing them as one thing lets you derive their behavior instead of memorizing five features.

## Related Modules

- `01-processes-and-threads.md` — the TLB flush on context switch, copy-on-write `fork()`, and address-space isolation are all this chapter's machinery
- `02-memory-management.md` — the allocator (malloc/jemalloc) that sits *above* demand paging, fragmentation, the OOM killer, and cgroup memory limits in detail
- `03-io-and-syscalls.md` — the page cache as the heart of file I/O, mmap vs. read(), and how dirty pages get written back
- `04-scheduling.md` — context switches (which flush the TLB) and how memory pressure interacts with scheduling latency
- `../02-data-structures-and-algorithms/01-arrays-and-memory-layout.md` — the cache-and-TLB locality argument and the "slow first run = demand paging" story, from first principles
