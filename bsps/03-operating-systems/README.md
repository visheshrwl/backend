# Module 03 — Operating Systems

> The operating system is the layer you never call directly and can't escape. Every connection your service holds is an OS abstraction; every slow request is, somewhere underneath, a syscall, a page fault, a context switch, or a throttled cgroup. This module makes that invisible layer visible — not as OS-course trivia, but as the machinery that decides your latency, throughput, and cloud bill. The thread through every chapter: *the abstraction is cheap until you cross a boundary, and all the cost lives at the boundaries.*

## How these chapters are built

Each chapter takes you from L1 (new grad) to principal-engineer depth on one topic, built from first principles. They share a deliberate anatomy:

- **Problem** — the specific misunderstanding this chapter destroys, usually with a number you can reproduce.
- **Why It Matters** — the latency, throughput, and cost consequences in real systems (databases, containers, the cloud bill).
- **Mental Model** — the intuition, built visually, that the rest of the chapter hangs on.
- **Underlying Theory** — layered from the simplest idea to systems-internals depth, each layer fixing a flaw in the last.
- **A Ladder From L1 to Principal** — the same topic at five altitudes.
- **Complexity Analysis**, **War Stories**, **Key Takeaways**, **Related Modules**.

## Contents

1. **[Processes and Threads](01-processes-and-threads.md)** — why one unit of concurrency costs 10 MB (process), megabytes (thread), or 2 KB (goroutine); what really happens in a context switch; copy-on-write `fork()`; and how to read a concurrency model off the OS cost table. *The C10K problem, from first principles.*
2. **[Memory Management](02-memory-management.md)** — the allocator as a shopkeeper between your bytes and the kernel's pages; size classes, arenas, and per-thread caches; why `free` doesn't return memory to the OS; fragmentation as a slow-motion failure; and GC as an allocator with a clock.
3. **[I/O and Syscalls](03-io-and-syscalls.md)** — the user/kernel wall and the cost of crossing it; why buffering exists; the page cache and the buffered-write-vs-`fsync` durability gap; blocking vs. `epoll` (O(ready) vs O(N), how C10K was solved); zero-copy; and io_uring.
4. **[Scheduling](04-scheduling.md)** — who runs when there's more work than cores; CFS as a fair accountant (vruntime); preemption vs. yielding; cache/NUMA affinity; and **cgroup CPU throttling** — *the* container bug behind high p99 at low utilization.
5. **[Virtual Memory](05-virtual-memory.md)** — every address is a lie the hardware resolves per-page; page tables, the TLB, and huge pages; the page fault as the kernel taking over a memory access (minor vs. major, the latency cliff); demand paging, overcommit, the OOM killer; and how mmap, COW, the page cache, and swap are all one mechanism.

## Reading Order

Read in numerical order, but know the dependency spine: **05 (virtual memory)** is the foundation — page tables, the TLB, and faults — that 01 (the TLB flush in a context switch), 02 (allocation atop demand paging), and 03 (the page cache) all build on. If you only read one as background, read 05.

## Cross-Module Links

Concepts here are applied in:
- `../02-data-structures-and-algorithms/01-arrays-and-memory-layout.md` — the cache/TLB locality story these chapters keep cashing in
- `../05-network-programming/` — epoll/kqueue event loops and socket I/O in depth
- `../06-databases/` — `fsync`/WAL durability, the buffer pool, and process-per-connection
- `../07-core-backend-engineering/` — threading vs. async vs. event loop, applied
- `../09-performance-engineering/` — profiling faults, syscalls, throttling, and scheduler-induced tail latency
- `../10-production-systems/` — operational application under real load
