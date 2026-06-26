# Processes and Threads

## Problem

Let me start with a number that decides architectures. You want to handle 10,000 simultaneous clients. Here's what that costs three different ways:

```
10,000 concurrent clients, each needs its own "thread of execution":
  one OS thread each:     10,000 × ~8 MB stack   = ~80 GB RAM   → impossible
  one process each:       10,000 × ~10 MB         = ~100 GB RAM  → impossible
  one goroutine each:     10,000 × ~2 KB stack    = ~20 MB RAM   → trivial
```

Same goal. The first two answers don't fit on a server; the third fits in a rounding error. This isn't a small constant-factor difference — it's the difference between "we need a fleet" and "one box, who cares." And it is *entirely* a consequence of what a process is, what a thread is, and what a goroutine is at the level of the operating system. The famous "C10K problem" (and its successor C10M) — how do you serve ten thousand, then ten million, concurrent connections — is not solved by a faster CPU. It's solved by *understanding this table* well enough to not pick the impossible rows.

Here's the thing most application engineers never confront: **your concurrency model is not a library choice, it's an operating-system choice in disguise.** When you pick "thread per request" or "async event loop" or "goroutines" or "process per connection," you are picking a point on a tradeoff curve between isolation, memory cost, and switching cost that the kernel defines and you merely consume. PostgreSQL forks a whole OS *process* for every connection — which is exactly why connection pools exist and why "just open more connections" eventually melts your database server. Nginx uses a handful of processes and an event loop, which is why it serves a hundred thousand connections on a laptop. Go invented goroutines specifically to make the cheap row of that table the *default* row. None of these are arbitrary; each is a deliberate trade against the costs we're about to take apart.

So the real subject of this chapter isn't "a process has its own memory and threads share memory" — you can recite that. It's *why those facts produce the cost table above*, what actually happens in the microseconds of a context switch, and how to read a concurrency model and immediately know which row of that table you've signed up for. Get this and the entire "threads vs. async vs. goroutines" debate stops being religion and becomes arithmetic.

## Why It Matters (Latency, Throughput, Cost)

Here are the costs that drive every decision below. Internalize the orders of magnitude, not the exact figures:

```
                          create        memory each        "switch" cost
  ──────────────────────────────────────────────────────────────────────
  OS process (fork)       ~1–2 ms       ~5–10 MB           1–10 µs + TLB flush
  OS thread (pthread)     ~50 µs        1–8 MB stack       1–10 µs + TLB flush
  Goroutine / async task  ~200 ns       ~2 KB (grows)      ~100 ns, no kernel, no TLB
```

**Process creation is dominated by the address space, not the code.** `fork()` for a tiny process is cheap; `fork()` for PostgreSQL's ~50 MB backend is "expensive" because the kernel must set up page tables describing all that memory (copy-on-write saves the *data* copy but not the bookkeeping). This is why PostgreSQL's per-connection cost is real and why PgBouncer exists: a pool of, say, 20 reused backends in front of 2,000 application connections turns 2,000 forks-and-fat-processes into 20 long-lived ones. The connection pool isn't a nicety; it's the thing standing between you and a DB server that spends its RAM on process overhead instead of caching your data.

**Thread memory is dominated by the stack.** Each OS thread reserves a stack — default 8 MB on Linux (1 MB on Windows). That's *reserved address space*, only partially backed by physical pages until touched, but the reservation still caps how many threads fit. Ten thousand threads at 8 MB is 80 GB of address space you've committed to, which is why "thread per connection" has a hard ceiling in the low thousands. The fix isn't smaller threads — it's *not having one thread per connection.*

**Switching cost is where throughput quietly dies.** A context switch costs 1–10 µs of *direct* work (saving and restoring registers), but the *indirect* cost is worse and invisible to microbenchmarks: switching to a different process flushes the TLB (so the new process re-walks page tables on its first memory accesses) and pollutes the CPU caches (the new thread's data evicts the old thread's). At 10,000 threads each switching every millisecond, you're doing 10 million switches per second, and the cache/TLB damage can eat double-digit percentages of your CPU before any of your actual code runs. This is the hidden tax that makes "just add more threads" stop scaling and start *reversing*.

## Mental Model

Strip it to the studs. **A process is an address space plus a bundle of resources. A thread is a flow of execution through that address space.** Everything else follows from those two sentences.

```
PROCESS  (one private virtual address space + kernel bookkeeping)
┌──────────────────────────────────────────────────────────┐
│  CODE (text)    GLOBALS (data)    HEAP (malloc) ──grows──► │
│                                                            │
│  file descriptors, signal handlers, PID, permissions...    │  ← shared by all threads
│                                                            │
│  ┌─ Thread 1 ─┐   ┌─ Thread 2 ─┐   ┌─ Thread 3 ─┐         │
│  │ stack      │   │ stack      │   │ stack      │  ◄──────┼── each thread: own stack,
│  │ registers  │   │ registers  │   │ registers  │         │   own registers, own
│  │ TLS        │   │ TLS        │   │ TLS        │         │   thread-local storage
│  └────────────┘   └────────────┘   └────────────┘         │
└──────────────────────────────────────────────────────────┘
```

The whole personality of each abstraction falls out of *what's inside the box vs. what's shared*:

- **Threads share the heap, the code, and the file descriptors.** That's why threads communicate almost for free — two threads see the same memory, so "passing data" is just a pointer. It's *also* why a single corrupt pointer in one thread can scribble over another thread's data and crash the whole process: shared memory is shared blast radius. No isolation.
- **Processes share nothing by default** — separate address spaces, walled off by the hardware (chapter 05's virtual memory is the wall). That's why a bug in one PostgreSQL backend can't corrupt another backend, which is precisely what you want in a multi-tenant database. The cost of that safety is that processes can't just pass a pointer; they need explicit, slower channels (pipes, sockets, shared-memory segments) to talk.

So the fundamental dial is **isolation vs. communication cost**, and it's a genuine trade, not a "better/worse." Threads: cheap sharing, zero isolation. Processes: strong isolation, expensive sharing. Every concurrency architecture is choosing where on that dial to sit — and then, increasingly, *cheating* the memory and switching costs with a third option (user-space threads) that we'll build up to.

## Underlying Theory

We'll build from "what is a process, really" up to "why goroutines exist and what they cost." Each layer adds one piece of the machine.

### Layer 1 — What a process actually is to the kernel

To you, a process is "my program running." To the kernel, a process is a **data structure** — a `task_struct` in Linux, often called the Process Control Block — and that structure is the process. It holds the process ID, the pointer to its page tables (its private map from virtual to physical memory, see chapter 05), its open file descriptor table, its signal handlers, its scheduling state and priority, its parent, its permissions. The "program running" is just a CPU executing instructions while the kernel keeps this struct up to date.

Why does this matter? Because *everything expensive about a process is expensive because of what's in that struct.* Creating a process means building all of that. Switching to a process means loading the relevant parts of it into the CPU (and pointing the MMU at its page tables). Killing a process means tearing it down. When we say `fork()` is "O(virtual pages)," we mean the costly part is constructing the new page-table map — the bookkeeping that gives the child its own private view of memory. The process *is* its metadata; the running is the easy part.

### Layer 2 — Threads: many flows through one address space

Now the key realization: most of that `task_struct` — the page tables, the file descriptors, the heap — doesn't actually need to be *per-flow-of-execution.* If two flows are cooperating on the same task (handle this request, render this frame), they *want* to share the heap and the open files. The only things that genuinely must be private to each flow are: the **stack** (each flow has its own call chain) and the **registers** (each flow is at its own instruction, with its own local values), plus a slice of thread-local storage.

That's a thread. On Linux, a thread is *literally* a `task_struct` that shares its memory map and file descriptors with its siblings instead of having private copies — it's created by the same underlying `clone()` syscall as a process, just with "share the address space" flags flipped on. This is a beautiful unification: a process and a thread are the same kind of kernel object; a "process" is just a thread that shares nothing, a "thread" is one that shares almost everything.

The cost consequence: threads are cheaper than processes to create (no new address space to build) and cheaper to switch *between siblings* (same page tables → no TLB flush). But they still cost a kernel-managed stack (megabytes of reserved address space) and every switch still goes through the kernel scheduler. They're cheaper, not cheap.

### Layer 3 — The context switch, in slow motion

This is the operation that quietly governs your throughput ceiling, so let's watch it happen frame by frame. A context switch occurs when the kernel decides a different thread should run on this CPU — triggered by a timer interrupt (the running thread's time slice expired), a thread blocking on I/O (voluntarily yielding), a syscall returning, or a thread exiting.

```
Thread A running ──► [switch] ──► Thread B running

  1. Trap into the kernel (interrupt or syscall)            ← mode switch
  2. Save A's registers (RSP, RIP, general regs) to A's kernel stack
  3. Save A's FPU/SIMD state if it was used (~100 cycles)
  4. Scheduler picks B (chapter 04: the CFS red-black tree)
  5. IF B is a different PROCESS than A:
        point the MMU at B's page tables  →  TLB FLUSH ☠
  6. Load B's saved registers
  7. Return to user space as B                              ← mode switch back
```

The *direct* cost — steps 2, 3, 6 — is maybe a microsecond. The killers are the implicit ones. Step 5's **TLB flush** means B starts with an empty translation cache and must re-walk multi-level page tables on its first memory touches (chapter 05 explains why that hurts). And nowhere on this list but everywhere in reality: B's working set isn't in the CPU caches — A's was. So B runs slow for thousands of cycles while it re-warms L1/L2 with its own data, having just evicted A's. (This is exactly the arrays-chapter cache story, now playing out across threads.) Switching between two *threads of the same process* skips the TLB flush (shared page tables) — which is one concrete reason threads can be cheaper than processes beyond just creation cost.

The lesson: a context switch isn't "save some registers." It's "throw away the CPU's warmed-up state and rebuild it." That's why a system thrashing between thousands of threads can spend more time switching than working — the dreaded state where adding load *decreases* throughput.

### Layer 4 — fork() and copy-on-write: the PostgreSQL model

When a process calls `fork()`, the child is supposed to get a complete *copy* of the parent's memory. Naïvely copying a 50 MB process's entire address space on every fork would be brutal. So the kernel cheats with **copy-on-write (COW)**: the child's page tables are set up to point at the *same physical pages* as the parent, all marked read-only. Both processes share the actual memory — until one of them *writes*. That write triggers a protection fault, the kernel transparently makes a private copy of just that one page (~1 µs), and the writer continues on its private copy. Pages that are only read are shared forever, for free.

This is why `fork()`'s cost is dominated by *setting up the page tables* (proportional to the virtual size), not by copying data — the data copy is deferred and often never happens. It's why PostgreSQL's fork-per-connection model is viable at all: a new backend shares the parent's code and untouched memory via COW, only paying for pages it actually dirties. And it's why PostgreSQL's *shared buffers* (the buffer pool) are deliberately allocated as **shared memory**, not COW memory — every backend must see the *same* cached database pages, so they're explicitly mapped shared, outside the COW mechanism. Understanding COW is understanding why "process per connection" isn't as insane as the raw 10 MB figure suggests — and also why it still doesn't scale to 10,000 connections, because the per-process page-table and switching overhead is real even when the data copy is free.

### Layer 5 — User-space threads: cheating the whole table

Here's the move that produced that magical 2 KB row. The expensive things about an OS thread are: (1) a big kernel-managed stack, and (2) every switch and every scheduling decision goes through the *kernel*. What if we kept thousands of "threads" entirely in *user space*, invisible to the kernel, and multiplexed them onto a small number of real OS threads ourselves?

That's the **M:N threading model** (M user-space tasks on N OS threads), and it's what goroutines, Java's virtual threads (Project Loom), and async/await runtimes all are underneath. The wins are exactly the costs we identified:

- **Tiny, growable stacks.** A goroutine starts with a ~2 KB stack that grows on demand, instead of reserving 8 MB up front. That single change turns 80 GB into 20 MB.
- **Switches that never enter the kernel.** When goroutine A blocks (say, on a network read), the Go runtime's scheduler — running in user space — just saves A's tiny context and runs goroutine B on the *same* OS thread. No trap into the kernel, no TLB flush, no scheduler syscall. ~100 ns instead of ~1–10 µs. The kernel never even knows a "switch" happened; from its view, one OS thread kept running.

The catch — and there's always a catch — is that user-space scheduling only works if blocking operations *cooperate.* If a goroutine makes a genuinely blocking syscall, it would block the underlying OS thread and stall every other goroutine parked on it. So these runtimes wrap blocking I/O: the Go runtime hands your "blocking" network read to an event-notification system (epoll/kqueue, chapter 03) and parks the goroutine, freeing the OS thread to run others. This is the deep reason async runtimes exist — they're the machinery that makes user-space threads' cheap switching compatible with I/O that would otherwise block the kernel thread underneath.

### Layer 6 — Reading a concurrency model off the costs

Now you can decode every concurrency architecture as a position on the cost table, and predict its scaling ceiling:

- **Thread-per-request** (classic Java/Tomcat, Apache prefork): one OS thread per in-flight request. Simple to reason about (blocking code, linear control flow), but capped at low thousands of concurrent requests by stack memory and switching overhead. Fine when concurrency is modest; a wall when it isn't.
- **Process-per-connection** (PostgreSQL, Apache prefork): maximum isolation, highest per-unit cost. Right when isolation is the product requirement (a crashing backend mustn't corrupt others) and connection count is bounded — *which is exactly why a pool in front is mandatory.*
- **Event loop / async** (Nginx, Node.js, Redis): a *single* (or few) OS thread(s) running an event loop, never blocking, juggling thousands of connections by reacting to readiness events. Collapses the memory and switching costs to near zero, at the price of "no blocking allowed, ever" — one slow synchronous operation stalls everything. (Redis being single-threaded is this taken to its logical, glorious extreme.)
- **M:N green threads** (Go, Loom): the synthesis — write simple blocking-style code, but each "thread" is a 2 KB user-space task the runtime multiplexes over a few OS threads with epoll underneath. You get the event loop's efficiency with the thread model's readability. This is why Go became the default language for high-connection network services.

The point of the chapter, in one line: when someone shows you a concurrency design, ask "which row of the cost table is one unit of concurrency here, and what's the switching path?" — and you'll know its ceiling before you benchmark it.

## A Ladder From L1 to Principal

- **L1 / new grad:** A process has its own memory; threads within a process share memory; threads are cheaper than processes. You can write multithreaded code and know shared state needs locks.
- **L3–L4 / solid engineer:** You know *why* — the stack/address-space costs, that a context switch goes through the kernel, that goroutines/async are cheaper because they don't. You pick thread pools over unbounded threads and understand why PostgreSQL needs a connection pool.
- **Senior:** You reason about the context switch's hidden costs (TLB flush, cache pollution), copy-on-write semantics of `fork()`, and choose a concurrency model (threads vs. async vs. M:N) deliberately against the workload's concurrency level and isolation needs.
- **Staff:** You diagnose switch-storm/thrashing in production, tune thread-pool sizes against core count and I/O ratio, reason about isolation as a fault-containment property, and know when process isolation is worth its cost (multi-tenancy, security boundaries).
- **Principal:** You design the system's concurrency architecture as a first-class decision — where the isolation boundaries are, what the per-unit cost of concurrency is, how it interacts with the scheduler and memory system — and you can predict scaling ceilings from the model rather than discovering them in an incident. The cost table is how you think.

It's all one idea climbing: *a unit of concurrency costs memory to hold and time to switch to, and the entire art is making those two numbers small without giving up the isolation you actually need.*

## Complexity Analysis

| Operation | Cost | What dominates it |
|-----------|------|-------------------|
| `fork()` | O(virtual pages) | Building the child's page tables (COW defers the data copy) |
| `pthread_create()` | ~50 µs, O(1) | Kernel thread setup + stack reservation |
| goroutine / async task creation | ~200 ns, O(1) | A user-space allocation; no kernel involvement |
| Context switch (thread, same process) | 1–10 µs | Register save/restore; **no** TLB flush (shared page tables) |
| Context switch (process) | 1–10 µs + penalty | Register save/restore **+ TLB flush + cache pollution** |
| Context switch (goroutine/async) | ~100 ns | User-space register swap; no kernel, no TLB, no syscall |
| COW page fault (first write after fork) | ~1 µs | Allocate + copy one 4 KB page |

The table the asymptotics hide: every "O(1)" switch above has a constant that ranges over **two orders of magnitude** depending on whether it crosses the kernel and flushes the TLB. That constant is the whole ballgame for high-concurrency services.

## War Stories (the shape of the bug in the wild)

- **The connection pool that wasn't.** An app opened a fresh PostgreSQL connection per request under load; at a few thousand concurrent requests the DB server ran out of RAM to fork backends and ground to a halt — not from query load, but from *process overhead*. A PgBouncer pool of 25 backends fixed it instantly. The bottleneck was the OS, not the database.
- **The thread pool sized to infinity.** A service used an unbounded thread pool "so requests never wait." Under a traffic spike it spawned ~40,000 threads, and throughput *collapsed* — the box was spending its cores context-switching and thrashing caches, not serving requests. Capping the pool near the core count restored it. More threads than cores past a point makes you slower, not faster.
- **The async function with a blocking call.** A Node.js service called a synchronous, CPU-heavy crypto function inside its event loop. Because the event loop is one thread, that one blocking call froze *every* in-flight request for tens of milliseconds each time. The event-loop model's superpower (no per-connection cost) is also its trap (no blocking, ever).
- **The fork bomb that wasn't malicious.** A batch job `fork()`ed a worker per input file; on a directory with 100k files it exhausted the process table. COW made each fork cheap on data but the sheer count of `task_struct`s and PIDs hit a hard kernel limit. Bounded worker pools exist for a reason.

## Key Takeaways

1. **Your concurrency model is an OS-cost decision in disguise.** One unit of concurrency is a process (~10 MB, strong isolation), an OS thread (~MBs of stack, kernel-switched), or a user-space task (~2 KB, user-switched) — and that choice sets your scaling ceiling before you write a line of logic.
2. **A process is its metadata.** The expense of creating, switching, and destroying processes comes from the `task_struct` and page tables, not the running code. `fork()` is O(virtual pages) because it builds a page-table map.
3. **A thread is a process that shares almost everything** — same kernel object, just with the address space and file descriptors shared. Hence cheap communication (shared heap) and zero isolation (shared blast radius).
4. **A context switch throws away the CPU's warm state.** Direct cost is ~1 µs; the real cost is the TLB flush (on cross-process switches) and cache pollution. Thrash enough and adding load reduces throughput.
5. **Copy-on-write makes `fork()` survivable** — children share physical pages read-only until a write forces a private copy. It's why process-per-connection works at modest scale, and why PostgreSQL's buffer pool is explicitly *shared* memory, not COW.
6. **User-space (M:N) threads cheat the whole cost table** — tiny growable stacks and switches that never enter the kernel — which is why goroutines/virtual-threads/async exist. The price is that blocking I/O must be wrapped (epoll underneath) so it doesn't stall the OS thread.
7. **Pick the model by concurrency level and isolation need:** thread-per-request for modest concurrency, process-per-connection when isolation is the requirement (with a pool), event-loop/async or M:N green threads when you need tens of thousands of cheap concurrent flows.

## Related Modules

- `05-virtual-memory.md` — page tables and the TLB (what a context switch flushes), copy-on-write mechanics, and the address-space isolation that walls processes off
- `04-scheduling.md` — the CFS scheduler that *decides* which thread runs next at every context switch, time slices, and cgroup CPU throttling
- `03-io-and-syscalls.md` — the syscall boundary every kernel-level switch crosses, and the epoll/io_uring machinery that lets async runtimes park blocked tasks
- `02-memory-management.md` — per-process heaps, stacks, and the allocator behavior behind that "~10 MB per process"
- `../02-data-structures-and-algorithms/01-arrays-and-memory-layout.md` — the cache and TLB costs a context switch pays, from first principles
- `../07-core-backend-engineering/04-threading-vs-async-vs-event-loop.md` — this chapter's cost table applied to real concurrency-architecture choices
