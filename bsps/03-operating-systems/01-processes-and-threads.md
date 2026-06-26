# Processes and Threads

## Problem

Let me start with a number that decides architectures. You want to handle 10,000 simultaneous clients. Here's what that costs three different ways:

```
10,000 concurrent clients, each needs its own "thread of execution":
  one OS thread each:     10,000 × ~8 MB stack   = ~80 GB RAM   → impossible
  one process each:       10,000 × ~10 MB         = ~100 GB RAM  → impossible
  one goroutine each:     10,000 × ~2 KB stack    = ~20 MB RAM   → trivial
```

Same goal. The first two answers don't fit on a server; the third fits in a rounding error. This isn't a small constant-factor difference — it's the difference between "we need a fleet" and "one box, who cares." And it is *entirely* a consequence of what a process is, what a thread is, and what a goroutine is at the level of the operating system. The famous "C10K problem" — how do you serve ten thousand concurrent connections — is not solved by a faster CPU. It's solved by *understanding this table* well enough to not pick the impossible rows.

That's the first half of this chapter, and it's the *easy* half. Here's the hard half, the one that actually ends careers' worth of sleep: **the moment two threads share memory, you have entered a world where your code can be correct on every test, correct on your laptop, correct in staging for six months — and then produce a wrong answer in production that you cannot reproduce, because the bug only appears when two specific instructions on two specific cores interleave in one specific way out of billions.** Threads sharing memory is what makes them cheap to communicate. It is *also* what makes concurrent programming the single hardest thing in mainstream software engineering, and the reason is not that the rules are complicated. It's that the rules are *counterintuitive at the level of physics* — the hardware does not execute your program the way you wrote it, memory does not behave the way you think, and the abstractions that hide this (mutexes, atomics, channels) leak in ways that punish anyone who doesn't understand what's underneath.

So this chapter has two arcs. **Part I** is the cost model: why one unit of concurrency is 10 MB or 2 KB, what a context switch really does, why goroutines exist. That gives you the *performance* intuition to pick a concurrency architecture. **Part II** is the correctness model: cache coherence, memory ordering, what a mutex actually is underneath, deadlock, lock-free programming, and why "I added a lock" is not the same as "I understand what I'm protecting." That gives you the *safety* intuition to make concurrent code that's actually correct. Most material covers one and waves at the other. The waving is exactly where people break, so we're not going to wave.

## Why It Matters (Latency, Throughput, Cost)

Here are the costs that drive Part I. Internalize the orders of magnitude, not the exact figures:

```
                          create        memory each        "switch" cost
  ──────────────────────────────────────────────────────────────────────
  OS process (fork)       ~1–2 ms       ~5–10 MB           1–10 µs + TLB flush
  OS thread (pthread)     ~50 µs        1–8 MB stack       1–10 µs + TLB flush
  Goroutine / async task  ~200 ns       ~2 KB (grows)      ~100 ns, no kernel, no TLB
```

**Process creation is dominated by the address space, not the code.** `fork()` for a tiny process is cheap; `fork()` for PostgreSQL's ~50 MB backend is "expensive" because the kernel must set up page tables describing all that memory (copy-on-write saves the *data* copy but not the bookkeeping). This is why PostgreSQL's per-connection cost is real and why PgBouncer exists: a pool of 20 reused backends in front of 2,000 application connections turns 2,000 forks-and-fat-processes into 20 long-lived ones. The connection pool isn't a nicety; it's the thing between you and a DB server that spends its RAM on process overhead instead of caching your data.

**Thread memory is dominated by the stack.** Each OS thread reserves a stack — default 8 MB on Linux. That's *reserved address space*, only partially backed by physical pages until touched (chapter 05's demand paging), but the reservation caps how many threads fit. Ten thousand threads at 8 MB is 80 GB of committed address space, which is why "thread per connection" has a hard ceiling in the low thousands.

**Switching cost is where throughput quietly dies.** A context switch costs 1–10 µs of *direct* work, but the *indirect* cost is worse and invisible to microbenchmarks: switching to a different process flushes the TLB (chapter 05), and switching to any other thread pollutes the CPU caches. At 10,000 threads each switching every millisecond, you're doing 10 million switches per second, and the cache/TLB damage can eat double-digit percentages of CPU before any of your code runs.

**And the cost Part II is about — contention — is the one that doesn't show up until you scale.** A program with shared mutable state can be perfectly fast on 4 cores and *slower* on 32, because every write to a shared cache line forces a hardware coherence transaction between cores (we'll see exactly why). This is the cost that makes engineers say "we added cores and it got slower," and unlike the others, you cannot fix it by buying a bigger box — it's baked into the data structure. The correctness *and* the scalability of concurrent code both live in the same place: what happens, physically, when two cores touch the same memory.

## Mental Model

Strip it to the studs. **A process is an address space plus a bundle of resources. A thread is a flow of execution through that address space.** Everything else follows.

```
PROCESS  (one private virtual address space + kernel bookkeeping)
┌──────────────────────────────────────────────────────────┐
│  CODE (text)    GLOBALS (data)    HEAP (malloc) ──grows──► │
│                                                            │
│  file descriptors, signal handlers, PID, permissions...    │  ← shared by all threads
│                                                            │
│  ┌─ Thread 1 ─┐   ┌─ Thread 2 ─┐   ┌─ Thread 3 ─┐         │
│  │ stack      │   │ stack      │   │ stack      │  ◄──────┼── each thread: own stack,
│  │ registers  │   │ registers  │   │ registers  │         │   own registers, own TLS
│  └────────────┘   └────────────┘   └────────────┘         │
└──────────────────────────────────────────────────────────┘
```

The whole personality of each abstraction falls out of *what's shared vs. private*:

- **Threads share the heap, code, and file descriptors.** That's why threads communicate almost for free — two threads see the same memory, so "passing data" is just a pointer. It's *also* why a corrupt pointer in one thread can scribble over another's data and crash the whole process: shared memory is shared blast radius. No isolation. And — the theme of Part II — it's why two threads writing the same variable is a physics problem, not just a logic problem.
- **Processes share nothing by default** — separate address spaces, walled off by hardware (chapter 05's page tables are the wall). A bug in one PostgreSQL backend can't corrupt another. The cost of that safety is that processes can't pass a pointer; they need explicit, slower channels (pipes, sockets, shared memory) to talk.

So the fundamental dial is **isolation vs. communication cost**. Threads: cheap sharing, zero isolation, *and a minefield of correctness hazards*. Processes: strong isolation, expensive sharing, *and most of those hazards simply don't exist* (separate memory means no data races across processes). That last point is underrated and worth holding onto: a big reason the process-per-connection and "share nothing" architectures (PostgreSQL, Nginx workers, the actor model, Redis's single thread) are so robust is that *they sidestep Part II entirely.* The hardest bugs in this chapter are the tax you pay for shared mutable state, and some of the best designs are the ones that refuse to pay it.

---

## PART I — The Cost of Concurrency

### Layer 1 — What a process actually is to the kernel

To you, a process is "my program running." To the kernel, a process is a **data structure** — a `task_struct` in Linux — and that structure *is* the process. It holds the PID, the pointer to its page tables (its private virtual→physical map, chapter 05), its open file descriptor table, its signal handlers, its scheduling state and priority, its parent, its permissions. The "program running" is just a CPU executing instructions while the kernel maintains this struct.

Why does this matter? Because *everything expensive about a process is expensive because of what's in that struct.* Creating one builds all of it; switching to one loads the relevant parts into the CPU and points the MMU at its page tables; killing one tears it down. When we say `fork()` is "O(virtual pages)," the costly part is constructing the page-table map. The process *is* its metadata; the running is the easy part.

### Layer 2 — Threads: many flows through one address space

Most of that `task_struct` — page tables, file descriptors, heap — doesn't need to be *per-flow-of-execution.* Cooperating flows *want* to share the heap and open files. The only things that must be private to each flow are the **stack** (each has its own call chain), the **registers** (each is at its own instruction with its own locals), and a slice of thread-local storage.

That's a thread. On Linux a thread is *literally* a `task_struct` that shares its memory map and file descriptors with its siblings — created by the same `clone()` syscall as a process, just with "share the address space" flags flipped on. This is a beautiful unification: process and thread are the same kernel object; a "process" is a thread that shares nothing, a "thread" one that shares almost everything. Threads are cheaper to create (no new address space) and cheaper to switch between siblings (same page tables → no TLB flush) — but they still cost a kernel-managed stack and every switch goes through the kernel scheduler. Cheaper, not cheap.

### Layer 3 — The context switch, in slow motion

This operation quietly governs your throughput ceiling. A switch happens when the kernel runs a different thread on a CPU — triggered by a timer interrupt (slice expired), a thread blocking on I/O (voluntary yield), a syscall returning, or a thread exiting.

```
Thread A running ──► [switch] ──► Thread B running
  1. Trap into the kernel (interrupt or syscall)            ← mode switch
  2. Save A's registers (RSP, RIP, general regs)
  3. Save A's FPU/SIMD state if dirty (~100 cycles)
  4. Scheduler picks B (chapter 04: the EEVDF/CFS tree)
  5. IF B is a different PROCESS:  point MMU at B's page tables  →  TLB FLUSH ☠
  6. Load B's registers
  7. Return to user space as B                              ← mode switch back
```

The *direct* cost (steps 2,3,6) is ~1 µs. The killers are implicit. Step 5's **TLB flush** means B starts with an empty translation cache and re-walks page tables on its first memory touches (chapter 05). And B's working set isn't in the CPU caches — A's was — so B runs slow for thousands of cycles while it re-warms L1/L2, having just evicted A's. A context switch isn't "save some registers." It's "throw away the CPU's warmed-up state and rebuild it." That's why a system thrashing between thousands of threads can spend more time switching than working — the state where adding load *decreases* throughput.

### Layer 4 — fork() and copy-on-write

`fork()` is supposed to give the child a complete copy of the parent's memory. Copying a 50 MB process on every fork would be brutal, so the kernel cheats with **copy-on-write (COW)**: the child's page tables point at the *same physical pages*, all marked read-only. Both share the actual memory — until one *writes*. That write traps (a protection fault), the kernel copies just that one page (~1 µs), and the writer continues on its private copy. Read-only pages are shared forever, free.

So `fork()`'s cost is setting up page tables (proportional to virtual size), not copying data — the copy is deferred and often never happens. This is why PostgreSQL's fork-per-connection is viable, and why its *shared buffers* are deliberately **shared memory**, not COW — every backend must see the *same* cached pages. (COW has a famous dark side, "Dirty COW" and the more recent COW-vs-pinning bugs, where the race between the COW fault and other memory operations became an exploitable hole — a reminder that even this elegant trick has sharp edges; chapter 05 returns to it.)

### Layer 5 — User-space threads: cheating the whole table

The expensive things about an OS thread are (1) a big kernel-managed stack and (2) every switch and scheduling decision goes through the *kernel*. What if we kept thousands of "threads" in *user space*, invisible to the kernel, multiplexed onto a few real OS threads ourselves? That's the **M:N model** (M user tasks on N OS threads) — goroutines, Java virtual threads (Loom), async/await runtimes.

- **Tiny growable stacks.** A goroutine starts at ~2 KB and grows on demand instead of reserving 8 MB. That single change turns 80 GB into 20 MB.
- **Switches that never enter the kernel.** When goroutine A blocks on a network read, the Go runtime's *user-space* scheduler saves A's tiny context and runs B on the *same* OS thread. No trap, no TLB flush, no scheduler syscall — ~100 ns. The kernel never knows a switch happened.

The catch: user-space scheduling only works if blocking *cooperates.* A genuinely blocking syscall would block the underlying OS thread and stall every goroutine parked on it. So these runtimes wrap blocking I/O — handing your "blocking" read to epoll (chapter 03) and parking the goroutine, freeing the OS thread. *This is the deep reason async runtimes exist*: they're the machinery that makes cheap user-space switching compatible with I/O that would otherwise block the kernel thread underneath.

### Layer 6 — Reading a concurrency model off the costs

Now you can decode any architecture as a position on the table and predict its ceiling:

- **Thread-per-request** (classic Tomcat): one OS thread per in-flight request. Simple (blocking, linear code), capped at low thousands by stack and switching. Fine for modest concurrency; a wall otherwise.
- **Process-per-connection** (PostgreSQL): maximum isolation, highest per-unit cost, *no shared-memory hazards*. Right when isolation is the requirement and connection count is bounded — hence a mandatory pool in front.
- **Event loop** (Nginx, Node.js, Redis): one (or few) OS thread(s) running a loop, never blocking, juggling thousands of connections by reacting to readiness. Near-zero memory and switch cost, at the price of "no blocking, ever" — one slow synchronous call stalls everything. Redis single-threaded is this at its glorious extreme — *and note it dodges all of Part II by never sharing mutable state across threads.*
- **M:N green threads** (Go, Loom): write simple blocking-style code, but each "thread" is a 2 KB user task multiplexed over a few OS threads with epoll underneath. The event loop's efficiency with the thread model's readability — which is why Go owns high-connection network services.

When someone shows you a concurrency design, ask "which row is one unit of concurrency, and what's the switching path?" and you'll know its ceiling before you benchmark. But you also need the *other* question, the one Part I can't answer: **what mutable state do these units share, and what protects it?** That's where the real bugs are.

---

## PART II — The Hard Part: Shared Mutable State

Here is the uncomfortable foundation of everything that follows: **the CPU does not run your program. It runs a program that produces the same results as yours *would have on a single core*, and it is allowed to do almost anything to get there faster** — reorder instructions, execute them out of order, cache values in registers, buffer writes, speculate down branches — as long as *that one core* can't tell the difference. The instant a *second* core observes your memory, the illusion shatters, because the guarantees the hardware makes are "this core sees its own actions consistently," not "all cores see all actions in one agreed order." Concurrency bugs are, almost without exception, the gap between the program you wrote and the program the hardware actually executed becoming *visible to another thread.* We're going to build this up from the physical layer, because every higher abstraction (atomics, mutexes, channels) is just a disciplined way of forcing that gap closed at exactly the points where it matters.

### Layer 7 — Cache coherence (MESI): why shared writes are physically expensive

Recall from the arrays chapter that each core has its own private L1/L2 cache, and memory moves in 64-byte cache lines. Now ask the obvious question: if core 0 and core 1 both have a copy of the same line in their private caches, and core 0 writes to it, how does core 1 not read stale data? The answer is a hardware protocol — **MESI** — that runs on every memory access, and understanding it explains an entire class of "why doesn't this scale?" mysteries.

Every cache line, in every core's cache, is in one of four states:

- **Modified** — this core has the only copy, and it's dirty (changed, not yet written back to memory). Nobody else has it.
- **Exclusive** — this core has the only copy, and it's clean (matches memory).
- **Shared** — multiple cores have a clean copy. Reading is fine; nobody may write without coordinating.
- **Invalid** — this core's copy is stale/absent; it must fetch a fresh one to use it.

The protocol's rule is simple and brutal: **to *write* a line, a core must first own it Exclusively, which means invalidating every other core's copy.** So when core 0 writes a variable that core 1 also has cached, core 0 must broadcast an invalidation, wait for core 1 to drop its copy (mark it Invalid), and only then write. The next time core 1 reads that variable, *its* copy is Invalid, so it must fetch the new value from core 0 — a cache-to-cache transfer across the interconnect, tens of nanoseconds.

```
Two cores, one shared counter, both keep it cached:

  core0: write counter ─► must own it Exclusive ─► INVALIDATE core1's copy ─► write
  core1: read counter  ─► my copy is Invalid ─► fetch fresh from core0 (cross-core)
  core1: write counter ─► INVALIDATE core0's copy ─► fetch ─► write
  core0: read ...       ─► Invalid ─► fetch ...

  The line PING-PONGS between caches. Every access is a coherence transaction.
```

Now the punchline. Imagine N threads on N cores all incrementing one shared counter. *Logically* it's the cheapest operation imaginable. *Physically*, that one cache line is bouncing between all N cores' caches, and every single increment pays a cross-core coherence round trip while the others stall waiting their turn. The counter becomes a serialization point — worse than a lock in some ways — and your "embarrassingly parallel" workload runs *slower* on 32 cores than on 4. This is **the** scalability killer, and it's invisible in code: the variable looks innocent. The fixes are all "stop sharing the line": per-core/per-thread counters summed occasionally (sharding the contention away), or padding hot variables onto separate cache lines.

Which brings back **false sharing** (introduced in the arrays chapter), now fully explained: two *logically unrelated* variables that happen to sit in the *same 64-byte line* suffer this exact ping-pong even though no thread shares any actual data — because coherence operates at line granularity, not variable granularity. Thread A writing `counter_a` invalidates the line in thread B's cache, evicting B's untouched `counter_b` along with it. The bug looks impossible ("they don't share anything!") until you remember the hardware doesn't know about your variables, only lines. The fix is `alignas(64)` to push them onto separate lines. Every high-performance concurrent data structure pads its hot fields for exactly this reason.

### Layer 8 — Memory ordering: the hardest idea in the chapter

Cache coherence guarantees that all cores eventually agree on the value of *each single location.* It guarantees **nothing** about the *order* in which writes to *different* locations become visible to other cores. That gap is **memory ordering**, and it is the single most counterintuitive thing in concurrent programming, the place where even experienced engineers are simply *wrong* about what their code does.

Start with the canonical example — "message passing," which you have absolutely written:

```
Shared:   int data = 0;   bool ready = false;

Thread A (producer):           Thread B (consumer):
    data = 42;                     while (!ready) { }     // spin until ready
    ready = true;                  print(data);           // expect 42
```

Obvious, right? A writes the data, *then* sets the flag; B waits for the flag, *then* reads the data; B must print 42. **This is wrong, and it can print 0.** Two independent mechanisms can break it:

1. **The compiler reorders.** To the compiler, in thread A alone, `data = 42` and `ready = true` are independent writes with no dependency — it may emit them in either order, or hoist the flag write earlier, because *single-threaded* semantics are preserved. It has no idea another thread is watching.
2. **The CPU reorders.** Even if the instructions are emitted in order, modern cores have a **store buffer**: a write doesn't go straight to cache, it sits in a per-core buffer and drains to cache later, possibly *out of order* relative to other writes. So core A might make `ready = true` visible to other cores *before* `data = 42` has drained. And on the read side, core B might speculatively load `data` *before* the `ready` check completes (out-of-order execution), grabbing the stale 0.

Either way, B can see `ready == true` while `data` is still 0. The program has a **data race**, and here is the part that trips people: in C, C++, and most languages, a data race is not "you get one value or the other" — it is **undefined behavior**, meaning the compiler is permitted to assume it never happens and optimize accordingly, which can produce outcomes far weirder than a stale read (e.g., the `while (!ready)` loop being hoisted into `if (!ready) while(true)` because the compiler proved `ready` can't change *within this thread*).

Why does the hardware allow this madness? **Speed.** The store buffer lets a core keep executing instead of stalling ~100 ns on every write waiting for cache. Out-of-order execution keeps the pipeline full. These optimizations are enormous wins and totally safe *for a single thread*. The cost is that the multi-core memory model is *relaxed*: each core sees its own operations in program order, but the order in which one core's writes appear to *another* core is not guaranteed — unless you explicitly demand it.

How you demand it: **memory barriers (fences)** and the **acquire/release** semantics built on them.

- A **release** operation (typically a store, like setting `ready`) says: "all my memory writes *before* this point must be visible to any thread that observes this operation." It's a one-way fence — nothing above it can sink below it.
- An **acquire** operation (typically a load, like reading `ready`) says: "all my memory reads *after* this point must see writes that happened before the matching release." Nothing below it can hoist above it.

Pair them and you get **happens-before**: if A's release of `ready` is observed by B's acquire of `ready`, then everything A did *before* the release is guaranteed visible to B *after* the acquire. *That's* what makes the message-passing example correct:

```
Thread A:  data = 42;                 Thread B:  while (!ready.load(acquire)) {}
           ready.store(true, release);            print(data);   // now guaranteed 42
```

The release/acquire pair forces the store buffer to drain and forbids the reorderings, *at this one point*, paying the synchronization cost only where you actually need ordering. This is the real machinery underneath every higher abstraction: a mutex `unlock` is a release, a `lock` is an acquire (that's *why* data you wrote under one lock acquisition is visible to the next thread that takes the lock); a Go channel send/receive establishes happens-before; Java's `volatile` and `synchronized` are defined in terms of this; Rust's `Ordering::{Acquire,Release,SeqCst}` exposes it directly.

Two crucial clarifications that catch people:

- **`volatile` in C/C++ is NOT a memory barrier and does NOT make threads safe.** It tells the compiler "don't optimize away these accesses" (for memory-mapped hardware registers), but it imposes no inter-thread ordering and no atomicity. Using `volatile` for thread communication in C/C++ is a classic, dangerous mistake. (Java's `volatile` is a *completely different thing* that *does* mean acquire/release — same word, opposite trap.)
- **Hardware memory models differ, which is why bugs hide.** x86 has a relatively *strong* model (TSO — Total Store Order): it only allows store-load reordering, so racy code is often *accidentally* correct on x86 and then explodes when ported to ARM or POWER, which have *weak* models that reorder far more aggressively. A data race that "worked fine for years" on x86 servers can corrupt data the day it runs on an ARM (Graviton, Apple Silicon) instance. The bug was always there; the strong x86 model was hiding it. This is one of the most expensive real-world consequences of not understanding memory ordering.

If you take one thing from Part II: **you cannot reason about shared-memory concurrency by imagining the threads' instructions interleaved in program order.** They aren't in program order. The only ordering you can rely on between threads is the ordering you *explicitly establish* with synchronization. Everything else, the hardware is free to scramble.

### Layer 9 — Atomics and the compare-and-swap primitive

We need operations that are *indivisible* — that complete fully or not at all, with no other core able to interleave in the middle — and that carry the ordering guarantees of Layer 8. These are **atomic operations**, implemented with special CPU instructions (`LOCK`-prefixed on x86, LL/SC — load-linked/store-conditional — on ARM) that the hardware guarantees are atomic with respect to all cores.

The humble example of why you need them: `counter++` is not one operation, it's three — *load* counter, *add* one, *store* counter. Two threads can both load 5, both add to get 6, both store 6 — and you've lost an increment. This is the most basic **race condition**, and it's why even a single shared counter needs `atomic.Add` (or a lock), not `++`. An atomic increment fuses the load-add-store into one indivisible step the hardware serializes.

The deepest atomic, the one from which all lock-free programming is built, is **compare-and-swap (CAS)**:

```
CAS(address, expected, new):
    atomically {
        if (*address == expected) { *address = new; return true; }
        else                       { return false; }    // someone else changed it
    }
```

"If this location still holds what I last saw, replace it; otherwise tell me I'm stale." This is the universal primitive (Herlihy proved CAS can implement *any* lock-free data structure) because it lets you do optimistic updates: read a value, compute a new one, and CAS it in — if someone raced you and changed it, the CAS fails and you retry with the fresh value. A lock-free counter is `do { old = load(); } while (!CAS(&counter, old, old+1));`. A lock-free stack push is: read the head, point your new node at it, CAS the head to your node, retry if it moved.

But CAS hides a genuinely hard trap that is the rite of passage of lock-free programming: **the ABA problem.** CAS checks that the value is *equal* to what you expected — not that it *never changed.* Suppose you read a pointer A from the top of a lock-free stack. Before your CAS, another thread pops A, pops B, and pushes A back. The top is *A again* — so your CAS *succeeds* — but the stack's internal state (what A points to) has completely changed underneath you, and you've just corrupted the structure by reattaching it to a stale `next`. The value went A→B→A; CAS can't tell. The fixes are subtle and they're the same problem as memory reclamation: **tagged pointers** (pack a version counter alongside the pointer so A-with-tag-1 ≠ A-with-tag-2), or deferred reclamation schemes — **hazard pointers**, **epoch-based reclamation**, and **RCU** (read-copy-update) — that ensure a node can't be *reused* while any thread might still be looking at it. (Those reclamation schemes are deep enough that we treat them properly in chapter 02, because they're fundamentally about *when is it safe to free memory* — but note the connection: lock-free data structures and garbage collection are solving two halves of the same problem.)

The honest summary on lock-free: it can eliminate lock contention and the risk of a thread dying while holding a lock, but it forces you to confront memory ordering (Layer 8), the ABA problem, and safe reclamation *all at once*, with no mutex to hide behind. It is genuinely expert territory, and the right default for almost everyone is a well-placed lock. Knowing *why* lock-free is hard is more valuable than attempting it casually.

### Layer 10 — How a mutex actually works: the futex

Here's a question that separates people who use locks from people who understand them: **is taking a mutex a system call?** The intuitive answer is yes — locking is a kernel thing, threads block, the kernel manages it. And that answer would make every lock cost a syscall (~hundreds of ns, chapter 03), which would make fine-grained locking unaffordable. The real answer, and the reason locking is practical, is **no — not in the common case.** Modern mutexes (Linux's pthread mutex, built on the **futex** — "fast userspace mutex") have a two-tier design that is genuinely beautiful:

```
LOCK:
  fast path (UNCONTENDED):  atomic CAS on a userspace integer  0 → 1.  Success? Done.
                            No syscall. No kernel. ~20 ns.        ← the common case
  slow path (CONTENDED):    CAS failed (lock held). NOW call futex(WAIT) to sleep
                            in the kernel until the holder wakes us. ~µs + a syscall.

UNLOCK:
  fast path:  atomic store 1 → 0.  If no waiters, done, no syscall.
  slow path:  if waiters were registered, futex(WAKE) one of them.
```

The insight: **the kernel only needs to get involved when a thread actually has to *sleep*.** An uncontended lock — which, in well-designed code, is the overwhelming majority — is just an atomic CAS on a word in your own memory, with acquire/release ordering (Layer 8) baked in. Pure user space, no boundary crossing. The futex *syscall* happens only on the slow path, when there's contention and a thread must block (futex's whole API is "atomically check this userspace word and sleep if it still has this value," letting userspace handle the fast path and the kernel handle only the sleeping).

This explains a pile of real behavior:

- **Uncontended locks are cheap** (~20 ns), so "just add a lock" is fine *as long as contention is low.* Lock granularity is about keeping contention low, not avoiding locks.
- **Contended locks fall off a cliff** — now every lock/unlock pays syscalls *plus* the cache-line ping-pong of the lock word itself (Layer 7: the lock variable is shared mutable state bouncing between cores) *plus* context switches as threads sleep and wake. A hot, contended mutex doesn't gently degrade; it collapses. This is why the fix for a contended lock is *reducing contention* (sharding the data, shorter critical sections, per-core structures), not micro-optimizing the lock.
- **Adaptive mutexes spin before sleeping.** Sleeping costs a context switch (~µs) and waking another (~µs); if the lock will be free in a few hundred nanoseconds, it's cheaper to *spin* (busy-wait) briefly than to sleep. Good mutex implementations spin a little, then fall back to futex-sleep — and a pure **spinlock** (spin forever, never sleep) is the right tool *only* for very short critical sections where the holder is guaranteed running on another core (the kernel uses them with interrupts disabled). Spinlocking on a userspace lock that might be held by a *descheduled* thread is a disaster — you burn a whole CPU spinning for a thread that isn't even running.

### Layer 11 — Deadlock, livelock, and the discipline that prevents them

Locks introduce a failure mode that has nothing to do with performance: the program *stops*. **Deadlock** — two or more threads each waiting forever for a lock the other holds. The textbook shape is two threads grabbing two locks in opposite orders:

```
Thread 1:  lock(A); lock(B); ...     Thread 2:  lock(B); lock(A); ...

  Interleave:  T1 gets A.  T2 gets B.  T1 waits for B (held by T2).
               T2 waits for A (held by T1).  Both wait forever. ☠
```

Coffman (1971) proved deadlock requires **four conditions simultaneously** — and this is worth memorizing because *breaking any one prevents deadlock*:

1. **Mutual exclusion** — the resource can't be shared (it's a lock).
2. **Hold and wait** — a thread holds one resource while waiting for another.
3. **No preemption** — you can't forcibly take a lock from a thread.
4. **Circular wait** — a cycle in the "who waits for whom" graph (which is *exactly* the cycle-detection / SCC problem from the graphs chapter — a database's deadlock detector literally builds this wait-for graph and runs DFS for cycles).

The practical defenses each kill one condition. The dominant one is killing **circular wait** via a **global lock ordering**: establish a total order on all locks (e.g., by address, or by a documented hierarchy) and *always acquire them in that order.* If everyone grabs A-before-B, the opposite-order interleaving above is impossible, no cycle can form, done. This is why mature codebases have rules like "always lock accounts in ascending ID order" — it's not bureaucracy, it's the one discipline that makes deadlock structurally impossible. Other defenses: **lock-free** (kills mutual exclusion), **try-lock with backoff** (kills hold-and-wait — grab all locks at once or release and retry), and **lock timeouts** (a crude way to break no-preemption, what databases do with `lock_wait_timeout`).

Two cousins of deadlock that are easy to miss:

- **Livelock** — threads aren't blocked, they're *actively* responding to each other in a way that makes no progress. The image: two people in a hallway each stepping aside in the same direction, repeatedly, forever. A naïve "detect contention, release everything, retry immediately" scheme livelocks under load because everyone retries in lockstep. The fix is the same as Ethernet's: **randomized exponential backoff** — retry after a random, growing delay so the threads desynchronize.
- **Priority inversion** — a *low*-priority thread holds a lock that a *high*-priority thread needs, and a *medium*-priority thread (which doesn't need the lock) preempts the low-priority holder, so the low thread never runs to release the lock, so the high thread is stuck behind the medium one. Priority got inverted. This famously nearly killed the 1997 **Mars Pathfinder** mission (its watchdog kept rebooting the lander because a high-priority task starved on a held mutex), fixed by remotely enabling **priority inheritance** (temporarily boost the lock holder to the priority of the highest waiter). It's a deep enough scheduling interaction that chapter 04 treats it in full — but it's born here, at the intersection of locks and priorities.

### Layer 12 — Signals: the other concurrency, the one inside a single thread

There's a second, sneakier form of concurrency that exists even in a single-threaded program: **signals.** A signal (SIGINT, SIGTERM, SIGSEGV, SIGCHLD) is the kernel *interrupting* your thread mid-instruction to run a handler, then resuming where it left off. That handler runs *concurrently* with your normal code in the sense that it can fire *between any two instructions*, including in the middle of a `malloc`, a `printf`, or a mutex acquisition.

This creates a brutal, under-appreciated constraint: **async-signal-safety.** If a signal fires while your main code is halfway through `malloc` (holding the allocator's internal lock, chapter 02), and your handler *also* calls `malloc`, the handler tries to take a lock the interrupted code already holds — instant self-deadlock. So inside a signal handler you may call only **async-signal-safe** functions, a tiny whitelist (`write`, `_exit`, a few others) that explicitly excludes `malloc`, `printf`, most of the standard library, and anything that takes a lock. Engineers who call `printf` in a SIGSEGV handler to log the crash and then see the program hang (or corrupt) are meeting this rule the hard way.

The standard escape — and a genuinely elegant pattern worth knowing — is to do *almost nothing* in the handler: the **self-pipe trick** (write a single byte to a pipe the event loop is watching) or Linux's **`signalfd`** (turn signals into a file descriptor you can `read()` in your normal epoll loop). Both convert the unsafe, can-fire-anywhere asynchronous signal into a safe, synchronous "there's data to read" event handled by your ordinary code, on your own terms. This is *the* reason production servers handle SIGTERM cleanly: the signal handler just flips a flag or pokes a pipe, and the real shutdown logic runs in the main loop where it's safe to allocate, log, and take locks. Signals are concurrency, and they obey concurrency's rules.

### Layer 13 — Namespaces + cgroups = the container (where it all comes together)

Finally, the abstraction you actually deploy into, built entirely from the pieces above. A **container is not a virtual machine** — there's no guest kernel, no hardware emulation. A container is *just a normal Linux process* (or process tree) that the kernel has given two things: a restricted *view* of the system (namespaces) and a *budget* of resources (cgroups).

- **Namespaces** virtualize what a process can *see*. A PID namespace makes the process believe it's PID 1 with no siblings (it can't see other containers' processes). A mount namespace gives it its own filesystem view (its own `/`). A network namespace gives it its own interfaces, ports, and routing table. There are namespaces for users, hostnames (UTS), IPC, and more. `clone()` — the same syscall that makes threads and processes — takes flags (`CLONE_NEWPID`, `CLONE_NEWNET`, …) that create a process in fresh namespaces. *Isolation of view, with one shared kernel underneath.*
- **cgroups** (control groups) virtualize what a process can *consume* — CPU time, memory, I/O bandwidth. This is where this entire module reconnects: a container's `limits.cpu` is a **cgroup CPU quota** that the *scheduler* (chapter 04) enforces by throttling — the cause of "high p99 at low utilization." A container's `limits.memory` is a **cgroup memory limit** that the *memory manager* (chapter 02 / 05) enforces, triggering the OOM killer at a ceiling far below host RAM. The container's memory pressure, its CPU throttling, its I/O limits are all the kernel mechanisms from the rest of this module, scoped to a group of processes.

So when you debug a container, you are debugging a *process* — with all the thread/memory/scheduling/IO behavior of this module — that simply has a narrowed view and a hard resource budget. There's no magic layer. The "it works on my laptop but the container is slow/OOMs/throttles" mysteries are *always* one of: a cgroup limit (CPU throttle, memory cap) or a namespace boundary (can't see a resource it expects). Understanding that a container is a namespaced, cgroup-limited process — not a tiny VM — is what lets you reason about it instead of treating it as a black box. It's the capstone: every concept in this module, scoped and shipped.

---

## A Ladder From L1 to Principal

- **L1 / new grad:** A process has its own memory; threads share memory and need locks; threads are cheaper than processes. You can write multithreaded code that mostly works.
- **L3–L4 / solid engineer:** You know the cost table (stack/address-space, kernel-switched vs. user-switched), why a context switch is expensive, and the basic hazards — race conditions, the need for atomics on shared counters, deadlock from inconsistent lock order. You use thread pools and connection pools correctly.
- **Senior:** You understand cache coherence and false sharing (and pad hot fields), what a mutex costs uncontended vs. contended (the futex fast/slow path), the four deadlock conditions and global lock ordering, and that signals demand async-signal-safety. You design critical sections to minimize contention.
- **Staff:** You reason about memory ordering — acquire/release, why racy code "works" on x86 and breaks on ARM, why `volatile` isn't a barrier — diagnose contention/false-sharing/priority-inversion in production, and know when lock-free is worth its hazards (ABA, reclamation) and when it isn't.
- **Principal:** You choose concurrency architectures to *minimize shared mutable state* in the first place (share-nothing, actor, single-writer, sharding) because the cheapest concurrency bug is the one the design makes impossible. You reason about the full stack — coherence, ordering, contention, scheduling, cgroups — as one system, and predict both scaling ceilings and correctness hazards from the design.

The two arcs converge here: *concurrency costs memory and switch time to run, and correctness/scalability to share — and the master move is arranging your system so units of work share as little mutable state as possible, because that single decision dissolves most of Part II.*

## Complexity Analysis

| Operation | Cost | What dominates it |
|-----------|------|-------------------|
| `fork()` | O(virtual pages) | Building the child's page tables (COW defers data copy) |
| `pthread_create()` | ~50 µs | Kernel thread setup + stack reservation |
| goroutine / async task | ~200 ns | User-space allocation; no kernel |
| Context switch (thread, same process) | 1–10 µs | Register save/restore; no TLB flush |
| Context switch (process) | 1–10 µs + penalty | + TLB flush + cache pollution |
| Context switch (goroutine/async) | ~100 ns | User-space register swap; no kernel, no TLB |
| Atomic op / CAS (uncontended) | ~10–20 ns | One `LOCK`-prefixed instruction, acquire/release ordering |
| Mutex lock/unlock (uncontended) | ~20 ns | Userspace CAS on the futex word; **no syscall** |
| Mutex lock (contended) | ~µs+ | `futex` syscall + sleep/wake + lock-word cache ping-pong |
| Shared cache-line write (N cores contending) | tens of ns × serialized | MESI invalidation + cross-core transfer per write |
| Cross-core coherence miss | ~tens of ns | Cache-to-cache line transfer over the interconnect |

The number that decides whether concurrency *scales*: the gap between an uncontended atomic/lock (~20 ns, no sharing) and a contended one (µs+, plus coherence traffic). Contention, not raw operation cost, is the ceiling — and contention is set by how much mutable state you share.

## War Stories (the shape of the bug in the wild)

- **The counter that scaled backwards.** A metrics path used one shared `atomic` counter incremented by every request. At 8 cores, fine; at 48 cores, throughput *dropped* — the counter's cache line was ping-ponging across all 48 cores (Layer 7), serializing the "parallel" work. Fix: per-CPU counters summed on read. The variable looked free; coherence made it the bottleneck.
- **The race that only happened on Graviton.** Code with a benign-looking unsynchronized flag ran flawlessly for years on x86. Moved to ARM instances, it began returning stale data intermittently — ARM's weak memory model exposed a store reordering x86's TSO had been hiding (Layer 8). The data race was always a bug; the strong x86 model was concealing it. Fix: proper acquire/release.
- **The deadlock that took two locks in two orders.** Two code paths locked `user` then `account` and `account` then `user`. Invisible until a production interleave hit the cycle and two requests hung forever (Layer 11). Fix: a global lock-ordering rule (always lock lower object-ID first). Deadlock became structurally impossible.
- **The crash handler that hung.** A SIGSEGV handler called `printf`/`malloc` to log the crash; under load it deadlocked against the allocator lock the faulting thread already held (Layer 12). Fix: handler does a single async-signal-safe `write` (or signalfd), real logging in the main loop.
- **The lock that was secretly a syscall storm.** A hot path took a fine-grained mutex millions of times/sec. Uncontended it was ~20 ns; once contention rose, every acquire became a `futex` syscall + context switch and throughput cratered (Layer 10). Fix: shard the protected data so contention dropped and the fast path returned.

## Key Takeaways

1. **Your concurrency model is an OS-cost decision in disguise** — process (~10 MB, isolated), OS thread (~MBs, kernel-switched), or user task (~2 KB, user-switched) — and that choice sets your scaling ceiling. User-space (M:N) threads cheat the table; the price is that blocking I/O must be wrapped in epoll so it doesn't stall the OS thread.
2. **A context switch throws away the CPU's warm state** (TLB flush on cross-process, cache pollution always); thrash enough and adding load reduces throughput.
3. **Shared mutable state is a physics problem, not just a logic problem.** Cache coherence (MESI) makes every write to a shared line a cross-core transaction, so a "free" shared counter can serialize 48 cores. The cure is to stop sharing the line (per-core data, padding against false sharing).
4. **The hardware does not run your program in order.** Compiler and CPU reorder freely as long as one core can't tell; other cores can. Inter-thread ordering exists *only* where you establish it with **acquire/release** synchronization. Racy code is undefined behavior, often "works" on strong x86 and breaks on weak ARM, and `volatile` (in C/C++) is not a barrier.
5. **Atomics make read-modify-write indivisible; CAS is the universal lock-free primitive** — and it brings the **ABA problem** and safe-reclamation hazards that make casual lock-free programming a trap. A well-placed lock is the right default.
6. **A mutex is not a syscall in the common case** — uncontended lock/unlock is a userspace CAS (~20 ns, futex fast path); the kernel is involved only when a thread must *sleep* (contention). So uncontended locks are cheap and contended ones fall off a cliff; fix contention, don't micro-optimize locks.
7. **Deadlock needs all four Coffman conditions; break one (usually circular wait, via a global lock order) and it's impossible.** Watch for livelock (fix with randomized backoff) and priority inversion (fix with priority inheritance).
8. **Signals are concurrency inside one thread** — handlers can fire mid-instruction, so only async-signal-safe calls are allowed; defer real work to the main loop (self-pipe / signalfd).
9. **A container is a namespaced, cgroup-limited process, not a VM** — its CPU throttling (chapter 04) and memory caps (chapter 02/05) are this module's kernel mechanisms scoped to a process group. Debugging a container is debugging a process with a narrowed view and a hard budget.
10. **The best concurrency design minimizes shared mutable state** (share-nothing, single-writer, sharding, actors) — because that one decision dissolves most of the hazards in Part II before they can exist.

## Related Modules

- `05-virtual-memory.md` — page tables and the TLB a context switch flushes; copy-on-write mechanics and its sharp edges; address-space isolation as the wall between processes
- `04-scheduling.md` — the scheduler that picks the next thread at every switch; priority inversion and inheritance in full; cgroup CPU throttling
- `02-memory-management.md` — the allocator lock (and why signal handlers can't allocate); GC and lock-free **memory reclamation** (RCU, hazard pointers, epochs) — the other half of the ABA problem
- `03-io-and-syscalls.md` — the syscall boundary a futex slow-path crosses; epoll, which async runtimes use to park blocked tasks; signalfd
- `../02-data-structures-and-algorithms/01-arrays-and-memory-layout.md` — cache lines and false sharing, from first principles
- `../02-data-structures-and-algorithms/04-graphs-and-network-algorithms.md` — deadlock detection as cycle/SCC detection on the wait-for graph
- `../07-core-backend-engineering/04-threading-vs-async-vs-event-loop.md` — this chapter's cost and correctness models applied to real concurrency-architecture choices
