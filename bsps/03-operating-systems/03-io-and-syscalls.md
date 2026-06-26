# I/O and Syscalls

## Problem

Your program lives in a sandbox. It can do arithmetic, move data around its own memory, and follow its own logic all day at full CPU speed — but the instant it needs to do anything *real* in the world (read a file, send a packet, get the time, allocate a page), it cannot. Your code runs in **user mode**, deliberately stripped of the privileges to touch hardware, because letting every program command the disk and network card directly would be chaos and a security catastrophe. The only way out of the sandbox is to ask the kernel to do the privileged thing on your behalf, through a **system call** — and a syscall is not a function call. It's a *mode transition*, a controlled trap across the wall between user and kernel space, with a real, measurable cost every time you cross.

That cost — ~100–500 ns per syscall on modern Linux (inflated by the Spectre/Meltdown mitigations now guarding the boundary) — sounds trivial until you see how often naïve code crosses. Read a 1 GB file one byte at a time with a syscall per byte and you've issued a *billion* syscalls: ~100+ seconds of pure boundary-crossing to move data the disk could stream in a couple of seconds. The same file in 64 KB chunks is ~16,000 syscalls — microseconds of overhead. Identical bytes, identical disk, five orders of magnitude difference, decided entirely by *how you batched your trips across the wall.* That's the first law of I/O: **the boundary is expensive, so I/O performance is the art of fewer, fatter crossings.**

But there's a deeper problem that shapes whole architectures, and it's where most engineers actually break. Most I/O is *slow* — physics-slow, not CPU-slow — so you spend most of your time *waiting*, and how you wait determines everything. Wait naïvely (block a thread per connection) and you can't scale past a few thousand connections (chapter 01). Wait cleverly (one thread watching ten thousand connections via epoll) and you unlock C10K — but now you're in a world of edge-triggered-vs-level-triggered subtleties, thundering herds, and readiness-vs-completion models that produce some of the nastiest, most intermittent bugs in backend systems. And underneath *all* of it sits a question people get catastrophically wrong: **when `write()` returns, is your data safe?** The answer is no, and the gap between "written" and "durable" — the world of the page cache, writeback, `fsync`, and a famous data-loss bug called fsyncgate — is where databases live and die. This chapter takes all of it at full depth: **Part I** is the cost of crossing and the basics of waiting; **Part II** is the hard machinery — edge-triggering, io_uring's completion model, the block layer, O_DIRECT, and the brutal subtleties of durability.

## Why It Matters (Latency, Throughput, Cost)

**Syscall overhead is a per-crossing tax, so batching is the lever.** Every `read`/`write`/`send`/`recv` not optimized away costs ~100–500 ns of pure transition. A server doing six small `write()`s to assemble a response pays six crossings; one `writev()` or a buffered write pays one. This is why every standard library wraps file descriptors in **buffered** I/O — the buffer turns a thousand tiny `read(1 byte)` calls into a handful of `read(64KB)` syscalls. Unbuffered I/O in a hot loop is one of the most common invisible performance bugs there is.

**The page cache makes most file I/O not hit the disk — and that's the whole performance model.** A `read()` is usually a copy from the kernel's **page cache** (chapter 05), where file pages live in RAM after first access. First read: disk I/O, ~ms. Subsequent reads: memory copy, ~µs. This ~1000× gap is why cache hit rate dominates file-I/O-heavy systems, and why benchmarks must separate cold from warm or they're fiction.

**Blocking I/O wastes a thread per wait, so the wait model caps concurrency.** A thread blocked in `read()` consumes its full memory footprint doing nothing. One-thread-per-connection ceilings at the thread ceiling — low thousands. Readiness notification (epoll/kqueue) breaks this: one thread watches tens of thousands of descriptors and handles only the ready ones. This is the foundation under Nginx, Redis, Node.js, and every async runtime — the difference between a thread per connection and a thread per *core* juggling thousands. **Your wait model is your concurrency ceiling.**

**"Written" is not "durable," and conflating them loses data.** A `write()` typically just dirties pages in the page cache and returns *immediately* — the disk write happens later. Durability requires `fsync()`, which is expensive (it actually waits for storage) and, as fsyncgate revealed, has error semantics so treacherous that a generation of databases were silently mishandling them. The cost of durability and the correctness of durability are both in Part II, and getting them wrong is how you lose committed transactions on a power cut.

## Mental Model

First picture: **the wall, and what it costs to cross.**

```
   USER MODE (your code, unprivileged)          KERNEL MODE (privileged)
   ┌──────────────────────────────┐    syscall   ┌────────────────────────────┐
   │  compute, move your own RAM   │  ──trap──►   │  touch hardware, disk, NIC,  │
   │  (full speed, no crossings)   │              │  page tables, other procs    │
   │                               │  ◄─return──  │                              │
   └──────────────────────────────┘   ~100–500ns └────────────────────────────┘
                          every crossing costs — so cross rarely, carry a lot
```

Your program is fast and powerless inside the wall; the kernel is privileged and slow-to-reach across it. The design principle: **amortize the crossing.** Don't cross per byte — per buffer. Don't `write()` six times — gather and `write()` once. Don't poll "ready?" in a loop — ask once to watch many things. Every optimization in this chapter is a variant of "fewer, fatter crossings," culminating in io_uring, which makes the crossings *nearly zero.*

Second picture: **what happens to a thread that waits**, the fork in the road for all of concurrency:

```
BLOCKING:  thread issues read() ─► no data ─► KERNEL PARKS THE THREAD ─► ... idle ...
           one thread, stuck, burning its whole memory footprint doing nothing

READINESS: one thread asks epoll "which of these 10,000 fds are ready?"
           ─► kernel returns the 12 ready ─► thread handles those 12 ─► asks again
           one thread, never parked, juggling thousands — only works on the ready ones
```

Blocking is *simple* (linear code) but spends a thread per wait. Readiness is *efficient* (one thread, thousands of connections) but inverts control flow into events. The entire history of high-performance I/O — and the reason async/await and goroutines exist — is the project of getting readiness efficiency back into blocking-style simple code. Hold both pictures and every I/O architecture becomes legible. Part II adds a *third* picture (the **completion** model — "tell me when it's *done*, not when it's *ready*") that io_uring brings, and it changes the game again.

---

## PART I — Crossing the Wall, and Waiting Well

### Layer 1 — What a syscall actually is

A regular function call jumps within *your* code in user mode, a few cycles. A syscall executes a special instruction (`syscall` on x86-64) that traps into the kernel, switches the CPU to privileged mode and a kernel stack, and jumps to the syscall handler — which *validates your arguments* (the kernel must never trust a user pointer), does the privileged work, then reverses everything to return you to user mode. The mode transitions, argument validation, and — since 2018 — the **Spectre/Meltdown mitigations** that flush/partition CPU state at the boundary all add up to that ~100–500 ns. Those mitigations roughly *doubled* syscall cost, which is why "reduce syscalls" got materially more important post-2018.

This justifies an entire category of design. The **vDSO** (virtual dynamic shared object) exists because `gettimeofday`/`clock_gettime` were called so often that the kernel maps a read-only page into every process so they're serviced *in user space with no crossing at all.* Buffered I/O batches crossings. `readv`/`writev` do scatter/gather in one crossing. io_uring (Layer 9) submits hundreds of operations with ~zero syscalls. All are responses to one fact: the wall is expensive.

### Layer 2 — The journey of a read(), and the page cache

Follow `read(fd, buf, 65536)` on a file:

```
1. trap into kernel (~hundreds of ns)
2. kernel finds the file's pages in the PAGE CACHE (chapter 05)
      ├─ HIT (warm): pages already in RAM ────────────► copy them (µs)
      └─ MISS (cold): pages not resident ─► disk I/O ──► major fault, ~ms, thread blocks
3. copy bytes from kernel page cache → your user buffer   ← note: a COPY
4. return to user mode
```

Two takeaways. The **page cache** does the heavy lifting — hit vs. miss is a ~1000× swing, dwarfing syscall cost, which is why read-heavy systems live by cache hit rate. And step 3 is a **copy** — for a server shipping a file to a socket, data gets copied repeatedly (disk→page cache→user buffer→socket buffer→NIC), pure overhead that zero-copy (Layer 4) kills.

On the write side, the asymmetry that bites: `write()` normally just copies into the page cache, marks pages **dirty**, and returns — the kernel flushes later. So `write()` returning means "the kernel has your data," **not** "it survives a power cut." That gap is the entire subject of Layer 12.

### Layer 3 — Blocking, non-blocking, and how epoll solved C10K

A descriptor is **blocking** by default: `read()` on an empty socket parks your thread until data arrives. Simple, but one wait = one stuck thread. Set it **non-blocking** and the same `read()` returns immediately with `EAGAIN` ("nothing ready") — but now *you* must decide when to retry, and busy-polling thousands of fds burns a core.

The synthesis is **readiness notification**: ask the kernel to watch many fds and tell you which became ready. The history is a ladder:

- **`select`/`poll`** — hand the kernel the *entire* fd list on *every* call; it scans all of them. O(N) per call — at 10,000 fds you re-scan 10,000 to find the dozen ready. *The* C10K bottleneck.
- **`epoll`** (Linux) / **`kqueue`** (BSD/macOS) — *register* fds once; the kernel maintains the readiness set internally and hands you only the ready ones. **O(ready), not O(total).** Watching 10,000 mostly-idle connections costs work proportional to the active handful. This O(1)-per-event scaling *solved* C10K and underlies every event loop.

An event loop is then: `epoll_wait()` → handle each ready fd without blocking → repeat. One thread, thousands of connections, work proportional to activity. But epoll has a sharp edge that causes real bugs — edge vs. level triggering — which is Part II.

### Layer 4 — Zero-copy: stop copying bytes you're just passing through

For "ship this file out over this socket," the data passes *through* your program without you looking at it — yet you paid to copy it into and out of user space. **Zero-copy** eliminates that: **`sendfile(out_socket, in_file)`** moves data page-cache→socket *inside the kernel*, never touching user space, in one syscall — how Nginx serves static files and Kafka serves log segments at near-wire-speed (Kafka's throughput is substantially `sendfile` DMA-ing log data straight from page cache to NIC). **`mmap`** (chapter 05) maps the file into your address space so you access it as memory, removing the explicit copy. **`splice`** moves data between fds via kernel pipe buffers. The principle: if you're a conduit for bytes, don't drag them across the wall and back — tell the kernel to move them where they're going.

---

## PART II — The Hard Machinery

### Layer 5 — Edge-triggered vs. level-triggered epoll: the gotcha that causes hangs

This is the single most common source of subtle epoll bugs, and it's worth getting *exactly* right. When you register an fd with epoll, you choose a notification mode:

- **Level-triggered (LT, the default):** epoll reports an fd as ready *as long as there is data to read.* If you read only half the buffered data and call `epoll_wait` again, it tells you the fd is *still* ready (because data remains). Forgiving — like a doorbell that keeps ringing while anyone's at the door.
- **Edge-triggered (ET):** epoll reports readiness *only on the transition* from not-ready to ready — once per arrival of new data. If you read only half and call `epoll_wait` again, it says **nothing** (no *new* data arrived), even though data is sitting in the buffer. A doorbell that rings once and never again until someone *new* arrives.

Here's the bug that has cost countless engineer-hours: you use edge-triggered mode (for performance — fewer wakeups), a 10 KB message arrives, your handler reads 4 KB and returns to the loop, and... the remaining 6 KB sits in the socket buffer *forever*, because epoll already delivered its one edge notification and won't deliver another until *more* data arrives — which may be never. The connection hangs, mysteriously, intermittently, only under certain message sizes. Nothing is "broken"; you violated edge-triggering's contract.

```
Edge-triggered contract:  on EACH ready notification, you MUST drain the fd completely —
  loop read() until it returns EAGAIN — because you won't be told again about what's left.

while (true) {
    n = read(fd, buf, size);
    if (n > 0)   process(buf, n);          // keep going
    else if (n < 0 && errno == EAGAIN) break;  // NOW the buffer is truly empty — stop
    else if (n == 0) { close(fd); break; }     // peer closed
}
```

So the rule: **edge-triggered means "drain until EAGAIN, every time."** Why use ET at all if it's so error-prone? Because it's essential with **multiple threads sharing one epoll** (LT would wake several threads for the same fd — the thundering herd of Layer 7) and it reduces redundant wakeups under high load. Level-triggered is simpler and safer and the right default for most single-threaded event loops; edge-triggered is the high-performance, must-drain-completely mode that powers the fastest servers and the trickiest bugs. Knowing which you're in — and honoring its contract — is a senior-level competence that separates "my server randomly hangs" from "my server is fast and correct."

### Layer 6 — Readiness vs. completion: reactor and proactor

Step back and notice what epoll fundamentally *is*: a **readiness** model. It tells you "this fd is ready — now *you* do the read." You still execute the `read()` syscall yourself, you still copy the data, you still cross the wall. This architecture has a name — the **reactor** pattern: wait for readiness, react by performing the I/O. It's what Nginx, Node.js (libuv), and Netty are built on.

There's a fundamentally different model: **completion.** Instead of "tell me when I *can* read," you say "**go read this for me, and tell me when it's *done*** — with the data already in my buffer." You submit the *operation*; the kernel performs the entire I/O (including the copy) asynchronously; you get a *completion* notification with the result ready. This is the **proactor** pattern, and it's what Windows IOCP pioneered and Linux's **io_uring** finally brought to Linux properly. The distinction is not academic:

```
REACTOR (epoll):     "fd is READY"   → you call read() → you copy → you handle
                     you still do the syscall and the work yourself, on every I/O

PROACTOR (io_uring): "read DONE, here's your data" → you just handle it
                     the kernel did the syscall-equivalent and the copy for you, async
```

Why does completion matter beyond elegance? Three reasons. First, **regular file I/O can't be readiness-based** — a disk file is "always ready" to epoll (it doesn't have the not-ready→ready transitions sockets do), so epoll historically couldn't do async *file* I/O at all; you needed a thread pool to fake it. Completion handles files and sockets uniformly. Second, completion lets you **batch** — submit many operations at once and collect many completions — which is the path to amortizing the syscall to nearly nothing. Third, completion is a more natural fit for the way storage hardware (NVMe) actually works (submission/completion queues in hardware). io_uring is the proactor model arriving on Linux, and it's why it's not just "faster epoll" — it's a different, more powerful shape.

### Layer 7 — The thundering herd: when readiness wakes everyone

A specific, important bug in multithreaded I/O. Suppose N worker threads all `accept()` on the same listening socket, or all wait on the same epoll fd, to spread incoming connections. A connection arrives. The kernel, naïvely, wakes *all N* threads — but only *one* can actually take the connection; the other N−1 wake up, discover there's nothing for them (`EAGAIN`), and go back to sleep. That's N wakeups and N−1 wasted context switches for one event — the **thundering herd**, and at scale on many-core boxes it's a real CPU sink and latency source.

The kernel evolved two fixes worth knowing:

- **`EPOLLEXCLUSIVE`** (Linux 4.5+): a flag telling epoll to wake only *one* (or a few) waiters per event instead of all of them, directly killing the herd for the shared-epoll case.
- **`SO_REUSEPORT`** (Linux 3.9+): the more elegant architectural fix — let *multiple* sockets bind the *same* port, one per worker thread/process, and the kernel load-balances incoming connections across them by hashing. Now there's no shared socket to stampede on; each worker has its own listening socket and its own accept queue. This is how modern Nginx (with `reuseport`), Envoy, and high-performance servers scale `accept()` across cores without a herd — and it also smooths load distribution. (It has its own subtlety: if a worker dies, connections already hashed to its queue can be dropped, which is why some setups prefer a single acceptor that hands off.)

The thundering herd is a general concurrency pattern (it shows up with mutexes and condition variables too — wake-one vs. wake-all), but in I/O it's specifically why naïve "N threads share one listening socket" doesn't scale, and why the fix is either wake-one semantics or per-thread sockets.

### Layer 8 — io_uring in depth: making the syscall disappear

Now the full picture of the model Part I only sketched. **io_uring** (Linux 5.1+, 2019) attacks the wall itself with two **shared ring buffers** that *both* your program and the kernel can read and write directly in shared memory: the **submission queue (SQ)** where you post I/O requests, and the **completion queue (CQ)** where the kernel posts results.

```
io_uring:  [ your program ] ── writes requests ──► [ SQ ring (shared mem) ] ◄── kernel reads
           [ your program ] ◄── reads results ──── [ CQ ring (shared mem) ] ◄── kernel writes
```

You add a batch of operations to the SQ by *just writing shared memory* — no syscall — then *optionally* one `io_uring_enter()` syscall tells the kernel "I've queued work" (and even that can be skipped). The kernel performs the operations and posts completions to the CQ, which you read from shared memory — *no syscall*. The advanced features are what make it transformative, and they're the hard, worth-knowing part:

- **SQPOLL mode:** a dedicated kernel thread *polls* the submission queue, so you can submit I/O with **literally zero syscalls** — write to the ring and the kernel thread picks it up. Genuinely syscall-free I/O.
- **Registered (fixed) buffers and files:** normally the kernel must validate and pin your buffer and look up your fd on every operation. Pre-*register* them once and subsequent operations skip that overhead — a major win for high-throughput paths.
- **Linked operations:** chain dependent operations ("read this, then write the result there") so the kernel executes the sequence without round-tripping to you between steps.
- **A unified async interface for *everything*** — files, sockets, `fsync`, `accept`, timeouts — completion-based, where epoll could only do socket *readiness*.

This is why databases (ScyllaDB was an early adopter), proxies, and language runtimes are moving to io_uring: it collapses the syscall cost this whole chapter has been fighting, *and* it's a true completion (proactor) model that handles file I/O properly. It's the logical endpoint: having spent decades making crossings fewer and fatter, io_uring makes them, for the data path, nearly *none*. (It's also been a notable *security* surface — its power and complexity produced enough kernel vulnerabilities that some hardened environments disable it, a reminder that "more powerful kernel interface" and "bigger attack surface" travel together.)

### Layer 9 — The block layer and I/O schedulers: below the page cache

When a read *does* miss the page cache and must hit storage, it enters the kernel's **block layer**, and what happens there matters for latency. Requests don't go straight to the disk; they pass through an **I/O scheduler** that can reorder, merge, and prioritize them. The historical reason was spinning disks: on an HDD, the seek (moving the head) dominates, so reordering requests into the head's sweep direction (the "elevator algorithm") massively improved throughput, and *merging* adjacent requests into one big sequential I/O avoided redundant seeks. **Plugging** is a related trick: the kernel briefly "plugs" the queue to accumulate requests so they can be merged before dispatch.

The modern twist, and the practical lesson: **on NVMe SSDs, the seek penalty is gone, and the old schedulers can *hurt*.** An NVMe drive has its own deep hardware queues and handles parallelism internally, so spending CPU reordering requests is wasted work that just adds latency. Linux moved to a **multi-queue** block layer (blk-mq) with per-CPU submission queues (avoiding a global lock — the same per-CPU theme as everywhere in this module), and the right scheduler choice changed: for NVMe, **`none`** (no scheduling, just submit) is often best; for SSDs needing some fairness, **`mq-deadline`**; for desktop interactivity, **`BFQ`** (fair queueing). You set it per-device in `/sys/block/<dev>/queue/scheduler`, and on a database server using the wrong scheduler (e.g., a heavy fair-queueing scheduler on fast NVMe) can measurably raise tail latency. The takeaway: the storage stack made assumptions tuned for spinning rust, and on modern flash you sometimes get faster by telling it to *do less*.

### Layer 10 — O_DIRECT: when the page cache is the enemy

Everything so far assumed the page cache is your friend — and usually it is. But sometimes it's exactly wrong, and that's why **`O_DIRECT`** exists. With `O_DIRECT`, reads and writes *bypass the page cache entirely*, doing DMA straight between the disk and your *user-space* buffer. Why would you ever want to skip the cache that gives you a 1000× speedup?

Because **a database manages its own cache (buffer pool), and double-caching is waste.** PostgreSQL, MySQL/InnoDB, and Oracle keep their own carefully-managed cache of pages in their own memory. If they *also* went through the OS page cache, the same data would be cached *twice* (once by the DB, once by the OS), halving effective memory and letting the OS's eviction policy (which doesn't understand the database's access patterns) fight the DB's. `O_DIRECT` lets the database own its caching completely. It's also used for huge sequential workloads (backups, copies) that would otherwise *blow out* the page cache — evicting everyone else's hot data to cache bytes you'll read exactly once (cache pollution).

The catch, and it's a hard one: **`O_DIRECT` has brutal alignment requirements.** Because it DMAs directly, your buffer's memory address, the file offset, and the transfer length must all be aligned to the device's block size (typically 512 bytes or 4 KB). Get it wrong and the I/O fails with `EINVAL`. This is why `O_DIRECT` code is full of `posix_memalign` and careful block-size arithmetic, and why it's genuinely hard to use correctly. (Linus Torvalds famously called `O_DIRECT` "a horrible interface" — but databases need it.) The lesson: the page cache is a great default that some applications must *opt out of* precisely because they have better information than the OS about how their data will be used — the recurring systems theme that the generic mechanism is right until a specialist knows better.

### Layer 11 — Writeback and dirty ratios: the asynchronous write machine

Back to the write side. When you `write()`, pages go dirty in the page cache and the kernel flushes them later — but *when*, and *how much* can accumulate? This is governed by the **writeback** machinery and its dirty-page thresholds, which cause real latency cliffs when misunderstood:

- **`dirty_background_ratio`** (default ~10% of RAM): once this much of memory is dirty, the kernel's background flusher threads *start* writing pages to disk asynchronously, while your program keeps running.
- **`dirty_ratio`** (default ~20%): the hard ceiling. Once this much memory is dirty, the kernel **forces your writing process to block** and flush pages synchronously — it *throttles* you until dirty pages drop below the threshold. Your innocent `write()` suddenly takes hundreds of milliseconds because you hit the dirty ceiling and the kernel made you wait for the disk.

This produces a classic, baffling symptom: a write-heavy service runs fine, then *periodically* stalls hard. What's happening is dirty pages accumulating fast (cheap, async) until they cross `dirty_ratio`, at which point *everyone writing gets throttled* into synchronous flushing — a write cliff. On a box with lots of RAM and a slow disk, the *default* ratios let an enormous amount of dirty data pile up, so when the dam breaks the stall is huge. Tuning these (lowering the ratios so writeback is smoother and more continuous, or using `dirty_bytes`/`dirty_background_bytes` for absolute limits on big-RAM machines) is a real lever for write-latency consistency. The mental model: writes are cheap and async *until* too much is outstanding, at which point the kernel applies the brakes to everyone — and the brakes feel like a random latency spike if you don't know about the dirty thresholds.

### Layer 12 — fsync, durability, and fsyncgate: the hardest correctness problem in I/O

Now the question that determines whether your database loses data: **how do you actually make data durable?** `write()` only dirties the page cache. To force data to physical storage you call **`fsync(fd)`** (flush this file's data *and* metadata) or **`fdatasync(fd)`** (flush data and only the metadata needed to read it back — faster, skips updating things like mtime). `fsync` is *expensive* — it genuinely waits for the storage device to confirm the data is on stable media (and on real hardware, may need to flush the drive's *own* volatile write cache via a FUA/flush command). This is why databases batch commits, why "fsync on every transaction" is the durability/throughput dial, and why the **write-ahead log (WAL)** exists: instead of fsyncing scattered data pages, append the change to a sequential log and fsync *that* (one sequential fsync), reconstructing on crash.

But here's where it gets genuinely dark, and it's a story every backend engineer should know — **fsyncgate (2018).** The question: *if `fsync` returns an error, what state is your data in, and what do you do?* The catastrophic discovery, found by the PostgreSQL team, was that on Linux (and others), the error handling was a minefield:

- When the kernel fails to write back a dirty page (disk error, thin-provisioned volume out of space, USB drive yanked), it marks the error — but historically, **once it reported that error on an `fsync`, it *cleared* the error state and marked the failed pages clean.** So a *retry* of `fsync` would return *success* — reporting durability for data that was never written. The application, following the reasonable "if fsync fails, retry it" logic, would get a success on retry and conclude the data was safe. It wasn't.
- Worse, the error might be reported to *whichever file descriptor happened to call fsync*, not necessarily the one that did the write — and in PostgreSQL's architecture (checkpointer process fsyncs files written by other backends), the error could be delivered to a process that then *didn't even know* which write failed, or lost entirely if no one was listening on the right fd at the right moment.

The result: a database could believe a checkpoint succeeded, advance its WAL, discard the log records it would need to recover — and have silently lost data, with no error surfaced. This was a *years-old, cross-database* latent data-loss bug (MySQL, MongoDB, and others shared the exposure) rooted in the subtle, under-specified semantics of what `fsync` failure *means*. The fixes were deep: the kernel changed to keep reporting errors more reliably (not clearing them on first read, tracking errors per file rather than losing them), and databases changed their strategy — most drastically, PostgreSQL's decision that **if `fsync` ever returns an error, the safest response is to PANIC and crash-recover from the WAL**, rather than trust any retry. The enduring lesson, and the reason this belongs in any serious treatment: **durability is not "call fsync and check the return code" — the error semantics of the entire write→writeback→fsync path are subtle, were genuinely broken for years, and the correct handling is defensive to the point of crashing.** If you ever build something that must not lose data, you must understand this path at this depth, because the naïve version is wrong.

### Layer 13 — mmap I/O and its sharp edges

The last hard piece: **memory-mapped I/O** as an alternative to read/write, and why it's seductive but dangerous for databases. `mmap` (chapter 05) maps a file into your address space so you access its bytes as memory, faulting pages in on demand — no explicit `read()` syscalls, no copy into a user buffer, and the OS page cache transparently handles caching and writeback. It feels like a free lunch: treat a 100 GB file as an array. LMDB and the old MongoDB MMAPv1 storage engine were built on it. But the sharp edges are severe, and the database community has largely concluded mmap is the *wrong* tool for a serious storage engine (the well-known "Are You Sure You Want to Use MMAP in Your DBMS?" paper lays this out):

- **SIGBUS on truncation:** if the underlying file is truncated (or an I/O error occurs) while you're accessing a mapped page, you don't get a clean error return — you get a **SIGBUS signal** that, unhandled, *crashes your process* (and recall from chapter 01 how nasty signal handling is). A read() would have returned an error code you could handle; mmap turns an I/O error into a fatal signal mid-instruction.
- **You lose control of I/O:** with mmap, *any* memory access can secretly become a blocking major fault (disk I/O) at an unpredictable point — you can't decide when to do I/O, can't do readahead intelligently, can't bound latency, because a page fault can fire on any pointer dereference. A database wants to *control* its I/O (prefetch, schedule, bound), and mmap takes that control away.
- **Writeback timing you don't control:** dirty mapped pages are written back by the kernel on *its* schedule (Layer 11's dirty ratios), not when the database's durability protocol needs them. `msync` forces it, but now you're back to managing flushes manually, having given up the simplicity that made mmap attractive — and you *still* can't get the transactional ordering guarantees a WAL needs.
- **Transactional safety and page-cache eviction** fight the database's own buffer management (the same double-caching/eviction issues O_DIRECT solves).

So mmap is brilliant for *read-mostly*, *crash-tolerant*, *can-accept-SIGBUS* workloads (loading a shared library, a read-only index, IPC via shared memory) and treacherous for a write-heavy transactional store that needs to control durability and latency. The lesson mirrors O_DIRECT's: the convenient generic abstraction (here, "files as memory") hides exactly the control a specialized system needs, and knowing *when the abstraction's leak will hurt you* is the senior judgment call.

---

## A Ladder From L1 to Principal

- **L1 / new grad:** I/O reads and writes data via syscalls; use buffered, not byte-at-a-time. You know `read`/`write`/`open`/`close`.
- **L3–L4 / solid engineer:** Syscalls cost ~hundreds of ns so you batch; the page cache makes warm reads fast; `write` isn't durable without `fsync`; `epoll` beats `select`/`poll`. You use buffered I/O and basic event loops.
- **Senior:** You honor edge- vs. level-triggered epoll contracts, understand zero-copy and reactor vs. completion models, recognize thundering herd and use `SO_REUSEPORT`/`EPOLLEXCLUSIVE`, and know the WAL/fsync durability dial. You diagnose excessive-syscall and unbuffered hot spots.
- **Staff:** You design the I/O architecture (event loop vs. completion/io_uring, O_DIRECT for self-caching engines, block-layer scheduler choice for NVMe, dirty-ratio tuning for write consistency) and reason about durability *failure* semantics, not just the happy path.
- **Principal:** You treat the user/kernel boundary, the page cache, and the durability path as primary design surfaces — choosing io_uring vs. epoll, mmap vs. read vs. O_DIRECT with eyes open to their failure modes (SIGBUS, fsync error handling, cache pollution), and defining durability guarantees you can actually defend after a power cut. "Fewer, fatter crossings," "is it warm?", and "is it *really* durable?" are reflexes.

One idea climbing: *real work means crossing into the kernel and waiting on slow storage; you cross rarely and carry much, cache aggressively, never burn a thread to wait, and — hardest of all — you treat the gap between "written" and "durable" with the paranoia it has historically earned.*

## Complexity Analysis

| Operation | Cost | What's happening |
|-----------|------|------------------|
| Function call (user space) | ~1 ns | Jump within your code; no crossing |
| Syscall (round trip) | ~100–500 ns | User→kernel→user mode transition + mitigations |
| `gettimeofday` via vDSO | ~few ns | Serviced in user space; no crossing |
| `read` (page-cache hit) | ~µs | Copy from page cache to your buffer |
| `read` (page-cache miss) | ~ms | Disk I/O / major fault; thread blocks |
| `write` (buffered) | ~µs | Dirty the page cache, return — **not durable** |
| `fsync` / `fdatasync` | ~ms+ | Force data to physical media; waits for storage |
| `select`/`poll` | O(N) per call | Re-scan all watched fds — the C10K wall |
| `epoll_wait` | O(ready) | Returns only ready fds — scales to 10⁴–10⁶ |
| `sendfile` (zero-copy) | one syscall, no user copy | Page cache → socket inside the kernel |
| io_uring (SQPOLL) | ~0 syscalls/op | Shared SQ/CQ rings; kernel polls submissions |
| Hitting `dirty_ratio` | sudden ~100ms+ stall | Kernel throttles your writes into synchronous flush |

The spread that defines the chapter: user call ~1 ns, syscall ~100–500 ns, warm read ~µs, cold read / fsync ~ms, dirty-ratio stall ~100ms+. Six orders of magnitude, and which one you hit is mostly *your* batching, warmth, and durability choices.

## War Stories (the shape of the bug in the wild)

- **The connection that hung at a certain message size.** An edge-triggered epoll handler read once per notification instead of draining to `EAGAIN`. Small messages (one read drained them) worked; larger ones left bytes in the socket buffer with no further edge ever delivered, hanging the connection (Layer 5). Fix: drain-until-EAGAIN.
- **The accept storm on 64 cores.** N worker threads shared one listening socket; every connection woke all N, N−1 wasting a context switch (Layer 7). `SO_REUSEPORT` gave each worker its own listening socket and the kernel load-balanced — herd gone, latency dropped.
- **The database that lost a committed transaction.** A storage engine trusted `fsync` retry-on-error; a transient disk error got reported once, cleared, and the retry returned success for data never written (fsyncgate, Layer 12). The data was gone with no error surfaced. Fix: treat any fsync error as fatal, recover from the WAL.
- **The periodic write stall.** A write-heavy service stalled hard every ~30 s. Dirty pages accumulated cheaply until they crossed `dirty_ratio`, then the kernel throttled all writers into synchronous flush (Layer 11). Lowering the dirty thresholds turned one big cliff into smooth continuous writeback.
- **The mmap'd store that crashed on a full disk.** A service mmap'd its data files; when a volume filled and a mapped write failed, the process took a SIGBUS and died instead of getting an error to handle (Layer 13). Moving the hot write path to explicit `write`+`fsync` restored graceful error handling.

## Key Takeaways

1. **A syscall is a mode transition across a wall (~100–500 ns), not a function call.** I/O performance is *fewer, fatter crossings*: buffer, batch, gather/scatter, vDSO, and ultimately io_uring (~zero syscalls).
2. **The page cache makes warm reads ~1000× faster than cold;** systems live by hit rate. But `write()` returning ≠ durable — it only dirties the cache.
3. **Edge-triggered epoll means "drain until EAGAIN, every time."** Reading partially and returning to the loop leaves data stranded with no further notification — a classic, intermittent hang. Level-triggered is the safer default; edge-triggered is the fast, must-drain mode.
4. **Readiness (reactor/epoll) and completion (proactor/io_uring) are different models.** Readiness tells you to *do* the I/O; completion *does it for you* and hands back the result — which uniquely handles async *file* I/O, enables batching, and is why io_uring is more than "faster epoll."
5. **The thundering herd** (one event waking N waiters) is why naïve shared-socket designs don't scale; fix with `EPOLLEXCLUSIVE` (wake-one) or `SO_REUSEPORT` (per-worker sockets).
6. **The storage stack assumes spinning disks it no longer has** — on NVMe, the `none` scheduler often beats reordering ones, and `O_DIRECT` lets self-caching databases bypass the page cache (with brutal alignment requirements) to avoid double-caching and pollution. Generic mechanisms are right until a specialist knows better.
7. **Writes are cheap and async until `dirty_ratio`, then the kernel throttles you** into synchronous flush — a periodic write cliff you tune away with the dirty thresholds.
8. **Durability is not "call fsync and check the return."** fsyncgate showed the entire write→writeback→fsync error path was subtly broken for years across databases; correct handling is defensive to the point of PANIC-and-recover-from-WAL. **mmap** turns I/O errors into fatal SIGBUS and surrenders I/O control, which is why serious transactional engines avoid it.

## Related Modules

- `05-virtual-memory.md` — the page cache, mmap, demand paging, the major faults a cold read triggers, and dirty-page writeback are all virtual-memory machinery
- `01-processes-and-threads.md` — why a blocked thread is expensive; async runtimes park goroutines on epoll; SIGBUS/signal handling for mmap failures; signalfd
- `04-scheduling.md` — a blocking syscall is a voluntary yield; how I/O waits and writeback throttling interact with scheduling
- `02-memory-management.md` — user vs. kernel buffers and the copies zero-copy eliminates; the page cache competes with the allocator for RAM
- `../05-network-programming/02-multiplexing-epoll-kqueue.md` — epoll/kqueue, edge vs. level triggering, and event loops applied to network servers in depth
- `../06-databases/` — the WAL, `fsync`/`fdatasync`, durability guarantees, buffer pools and `O_DIRECT`, and crash recovery built on this chapter's write path
