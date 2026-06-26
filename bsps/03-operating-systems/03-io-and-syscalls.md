# I/O and Syscalls

## Problem

Your program lives in a sandbox. It can do arithmetic, move data around its own memory, and follow its own logic all day long at full CPU speed — but the instant it needs to do anything *real* in the world (read a file, send a network packet, get the time, allocate a page), it cannot. Your code runs in **user mode**, deliberately stripped of the privileges to touch hardware, because letting every program directly command the disk and network card would be chaos and a security catastrophe. The only way out of the sandbox is to ask the kernel to do the privileged thing on your behalf, through a **system call** — and a syscall is not a function call. It's a *mode transition*, a controlled trap across the wall between user space and the kernel, with a real, measurable cost every single time you cross.

That cost — ~100–500 ns per syscall on modern Linux (inflated by the Spectre/Meltdown mitigations that now guard the boundary) — sounds trivial until you notice how often naïve code crosses. Read a 1 GB file one byte at a time with a syscall per byte and you've issued a *billion* syscalls: ~100+ seconds of pure boundary-crossing overhead to move data that the disk could stream in a couple of seconds. The same file read in 64 KB chunks is ~16,000 syscalls — microseconds of overhead. Identical bytes moved, identical disk, a five-orders-of-magnitude difference in overhead, decided entirely by *how you batched your trips across the wall.* This is the first and most important fact about I/O: **the syscall boundary is expensive, so the entire art of I/O performance is making fewer, bigger crossings instead of many small ones.**

But there's a second, deeper problem that shapes whole architectures. Most I/O is *slow* — not CPU-slow, physics-slow. A disk read is microseconds-to-milliseconds; a network round trip is hundreds of microseconds to hundreds of milliseconds. When your thread issues a blocking `read()` on a socket with no data yet, it doesn't spin — the kernel *parks* it, and that thread is dead weight until the data arrives. With one thread per connection (chapter 01), ten thousand idle-but-waiting connections means ten thousand parked threads, which we already proved is impossible. So the question "how do I wait for thousands of slow I/O operations without burning a thread on each?" is not a micro-optimization — it's the question that produced epoll, async/await, event loops, and io_uring. This chapter is about both problems: the *cost* of crossing into the kernel, and the *architecture* of waiting efficiently once you're there.

## Why It Matters (Latency, Throughput, Cost)

**Syscall overhead is a tax you pay per crossing, so batching is the lever.** Every `read`, `write`, `send`, `recv`, `gettimeofday` that isn't optimized away costs ~100–500 ns of pure transition — saving registers, switching to kernel stack, the mitigation barriers, and back. A web server that does six small `write()`s to assemble one HTTP response pays six crossings; one `writev()` (gather-write) or a buffered write that coalesces them pays one. This is why every language's standard library wraps raw file descriptors in **buffered** I/O (`BufferedReader`, `bufio`, `FILE*`): the buffer's entire purpose is to turn your thousand tiny `read(1 byte)` calls into a handful of `read(64KB)` syscalls. Unbuffered I/O in a hot loop is one of the most common and most invisible performance bugs there is — it's correct, it's just paying the syscall tax thousands of times over.

**The page cache makes most file I/O not actually hit the disk — and that's the whole performance model.** When you `read()` a file, you're usually not reading the disk; you're copying from the kernel's **page cache** (chapter 05), where the file's pages live in RAM after their first access. First read: a major fault / disk I/O, ~ms. Every subsequent read of those bytes: a memory copy, ~µs. This ~1000× gap is *the* reason file-I/O-heavy systems are designed around cache hit rates, why "the OS will cache it" is a real performance strategy, and why benchmarks must distinguish cold-cache from warm-cache numbers or they're measuring fiction. Writes are even sneakier: a `write()` typically just marks pages dirty in the page cache and returns *immediately* — the actual disk write happens later, asynchronously, unless you `fsync()`. That's fast, but it means "the write returned" and "the data is durable on disk" are different events, and conflating them is how databases lose data on power failure.

**Blocking I/O wastes a thread per wait, so the wait model caps your concurrency.** A thread blocked in `read()` consumes its full memory footprint (chapter 01's megabytes of stack) while doing nothing but waiting. Serve connections one-thread-each and your concurrency ceiling is your thread ceiling — low thousands. The readiness-notification model (epoll/kqueue) breaks this: *one* thread can watch tens of thousands of file descriptors and be told which ones are ready, handling only those, never blocking on the idle ones. This single capability is the foundation under Nginx, Redis, Node.js, and every async runtime — the difference between "a thread per connection" and "a thread per *core*, juggling thousands of connections each." Your choice of wait model is your concurrency ceiling.

## Mental Model

The first picture to hold is **the wall, and what it costs to cross it.**

```
   USER MODE (your code, unprivileged)          KERNEL MODE (privileged)
   ┌──────────────────────────────┐    syscall   ┌────────────────────────────┐
   │  compute, move your own RAM   │  ──trap──►   │  touch hardware, disk, NIC, │
   │  (full speed, no crossings)   │              │  page tables, other procs    │
   │                               │  ◄─return──  │                              │
   └──────────────────────────────┘   ~100–500ns └────────────────────────────┘
                                    ▲
                        every crossing has a fixed cost — so cross rarely, carry a lot
```

Your program is fast and powerless inside the wall; the kernel is privileged and slow-to-reach across it. The single design principle that falls out: **amortize the crossing.** Don't cross per byte — cross per buffer. Don't `write()` six times — gather and `write()` once. Don't ask the kernel "is this ready?" in a loop — ask it once to watch many things and tell you. Every I/O optimization in this chapter is a variation of "make the trips across the wall fewer and fatter."

The second picture is **what happens to a thread that waits**, and it's the fork in the road for all of concurrency:

```
BLOCKING:  thread issues read() ─► no data ─► KERNEL PARKS THE THREAD ─► ... idle ...
           one thread, stuck, burning its whole memory footprint doing nothing

READINESS: one thread asks epoll "tell me which of these 10,000 fds are ready"
           ─► kernel returns the 12 that are ─► thread handles those 12 ─► asks again
           one thread, never parked, juggling thousands — only works on ready ones
```

The blocking model is *simple* (linear code, easy to reason about) but spends a thread per concurrent wait. The readiness model is *efficient* (one thread, thousands of connections) but inverts your control flow into callbacks/events. The entire history of high-performance I/O — and the reason async/await and goroutines exist — is the project of getting the readiness model's efficiency *back* into the blocking model's simple, linear-looking code. Hold these two pictures and every I/O architecture decision becomes legible.

## Underlying Theory

### Layer 1 — What a syscall actually is

A regular function call jumps to another address in *your* code, in user mode, costing a few cycles. A syscall is categorically different: it executes a special instruction (`syscall` on x86-64) that traps into the kernel, switches the CPU from user mode to kernel (privileged) mode, switches to a kernel stack, and jumps to the kernel's syscall handler — which validates your arguments (the kernel must never trust a user pointer), does the privileged work, and then reverses all of that to return you to user mode. The mode transitions, the argument validation, and — since 2018 — the **Spectre/Meltdown mitigations** that flush or partition CPU state at the boundary to prevent speculative-execution leaks, all add up to that ~100–500 ns. (Those mitigations roughly *doubled* syscall cost on many systems, which is why "reduce syscalls" got materially more important post-2018.)

This is the root justification for an entire category of design. The **vDSO** (virtual dynamic shared object) exists because some "syscalls" — `gettimeofday`, `clock_gettime` — were called so often that the kernel maps a little read-only page into every process so those can be serviced *in user space without crossing the wall at all*. Buffered I/O exists to batch crossings. `readv`/`writev` exist to do scatter/gather in one crossing. And the newest answer, io_uring (Layer 5), exists to let you submit *hundreds* of I/O operations with essentially *zero* syscalls. Every one of these is a response to the same fact: the wall is expensive, so engineer around crossing it.

### Layer 2 — The journey of a read(), and the page cache

Follow a `read(fd, buf, 65536)` on a file and you see where the time actually goes:

```
1. trap into kernel (~hundreds of ns)
2. kernel finds the file's pages in the PAGE CACHE (chapter 05)
      ├─ HIT (warm): pages already in RAM ─────────────► just copy them (µs)
      └─ MISS (cold): pages not resident ─► disk I/O ──► major fault, ~ms, thread blocks
3. copy the bytes from kernel page cache → your user-space buf   ← note: a COPY
4. return to user mode
```

Two things to extract. First, **the page cache is doing the heavy lifting**, and whether you hit or miss it dwarfs the syscall cost — a warm read is µs, a cold read is ms, a 1000× swing from the same call. The kernel keeps file pages in RAM after first use precisely so the second access is a memory copy, not a disk trip; this is *why* read-heavy workloads live or die by cache hit rate. Second, notice step 3 is a **copy** — bytes move from the kernel's page cache into your buffer. For a web server shipping a file to a socket, the data gets copied multiple times (disk→page cache→user buffer→socket buffer→NIC), and those copies are pure overhead. Killing them is Layer 4.

On the write side, the asymmetry that bites people: `write()` normally just copies your bytes into the page cache, marks the pages **dirty**, and returns — the kernel flushes dirty pages to disk *later*, in the background. So `write()` returning means "the kernel has your data," **not** "the data survives a power cut." Durability requires `fsync()` (or `O_DIRECT`/`O_SYNC`), which forces the dirty pages to physical storage and is *expensive* (it actually waits for the disk). This is the single most important fact in database durability: the gap between fast-buffered-write and slow-durable-fsync is exactly where the WAL, fsync-on-commit, and "we lost data on power failure" incidents all live.

### Layer 3 — Blocking, non-blocking, and the C10K problem

A file descriptor is **blocking** by default: `read()` on a socket with no data *waits* (the kernel parks your thread) until data arrives. Simple, but one wait = one stuck thread. Set the fd **non-blocking** and the same `read()` instead returns immediately with `EAGAIN` ("nothing ready right now") — your thread keeps going. But that just relocates the problem: now *you* have to figure out *when* to try again, and busy-polling thousands of fds in a loop ("ready? ready? ready?") burns a CPU core doing nothing.

The synthesis is **readiness notification**: ask the kernel to watch many fds and *tell you* which become ready, so you neither block on idle ones nor busy-poll. The history is a ladder of getting this right:

- **`select`/`poll`** (old): hand the kernel the *entire* list of fds you care about on *every* call; the kernel scans all of them and returns which are ready. The fatal flaw is O(N) per call — at 10,000 fds you re-scan 10,000 every time to find the dozen that are ready. This is *the* C10K bottleneck.
- **`epoll`** (Linux) / **`kqueue`** (BSD/macOS): you *register* your fds once, and the kernel maintains the readiness set internally, handing you only the ready ones — **O(ready), not O(total)**. Watching 10,000 mostly-idle connections costs work proportional to the *active* handful, not the total. This O(1)-per-event scaling is the algorithmic breakthrough that *solved* C10K and underlies every modern event loop.

```
select/poll:  give kernel ALL 10,000 fds every call ─► kernel scans 10,000 ─► O(N) ✗
epoll:        register once ─► kernel tracks readiness ─► returns the 12 ready ─► O(ready) ✓
```

An event loop is then just: `epoll_wait()` for ready fds → handle each ready one without blocking → repeat. One thread, thousands of connections, work proportional to activity. Nginx, Redis, Node.js, and the I/O cores of Go/Rust async runtimes are all this loop. The cost, as chapter 01 noted, is that your logic fragments into callbacks/state-machines — which is the problem async/await and goroutines exist to paper back over.

### Layer 4 — Zero-copy: stop copying bytes you're just passing through

Recall the read() journey copied bytes disk→page-cache→user-buffer, and a server then copies them again user-buffer→socket. For the common case "ship this file out over this socket," the data passes *through* your program without you even looking at it — yet you paid to copy it into and back out of user space, plus two crossings. **Zero-copy** I/O eliminates that waste:

- **`sendfile(out_socket, in_file, ...)`** tells the kernel "send this file directly to this socket" — the data goes page-cache→socket *inside the kernel*, never copied to user space, in one syscall. This is how Nginx and Kafka serve static files and log segments at near-wire-speed: Kafka's famous throughput is substantially `sendfile` letting the OS DMA log data straight from page cache to NIC.
- **`mmap`** (chapter 05) maps the file into your address space so you access it as memory, removing the explicit `read()` copy (you fault pages in instead).
- **`splice`/`vmsplice`** move data between fds via kernel pipe buffers without user-space copies.

The principle: if you're a conduit for bytes, don't drag them across the wall and back — tell the kernel to move them where they're going. Zero-copy is "fewer, fatter crossings" taken to its limit: *zero* crossings for the data itself.

### Layer 5 — io_uring: making the syscall (almost) disappear

The newest chapter (Linux 5.1+, 2019) attacks the wall itself. Even with epoll, every actual `read`/`write` is still a syscall — so a busy server still crosses the wall constantly. **io_uring** replaces that with two **shared ring buffers** in memory that *both* your program and the kernel can see: a submission queue (SQ) where you write I/O requests, and a completion queue (CQ) where the kernel writes results. You add a batch of operations to the SQ ring by just writing to shared memory — *no syscall* — and the kernel picks them up and posts completions to the CQ ring — *no syscall* to read them. With the kernel polling the SQ, you can do thousands of I/O operations *per second per syscall*, approaching genuinely syscall-free I/O.

```
io_uring:  [ your program ] ── writes requests ──► [ SQ ring (shared mem) ] ◄── kernel reads
           [ your program ] ◄── reads results ──── [ CQ ring (shared mem) ] ◄── kernel writes
           batch many ops; amortize or eliminate the syscall entirely
```

It's also a *unified async interface* for both file and network I/O (epoll historically didn't do regular-file I/O well), and it supports linked operations and fixed buffers. io_uring is the current frontier — databases, proxies, and runtimes are adopting it precisely because it collapses the syscall cost that this whole chapter has been fighting. It's the logical endpoint: having spent decades making crossings fewer and fatter, io_uring makes them, for the data path, nearly *none*.

## A Ladder From L1 to Principal

- **L1 / new grad:** I/O reads and writes data; a syscall asks the kernel to do it; use buffered I/O, not byte-at-a-time. You know `read`/`write`/`open`/`close`.
- **L3–L4 / solid engineer:** You know syscalls cost ~hundreds of ns and batch accordingly; you understand the page cache makes warm reads fast and that `write` isn't durable without `fsync`. You use buffered I/O reflexively.
- **Senior:** You reason about blocking vs. non-blocking, why `epoll` beats `select`/`poll` (O(ready) vs O(N)), and you understand event loops and zero-copy (`sendfile`, `mmap`). You diagnose unbuffered-I/O and excessive-syscall hot spots.
- **Staff:** You design the I/O architecture — event loop vs. thread pool, when zero-copy matters, fsync/durability semantics for data integrity — and tune against page-cache behavior and syscall counts (`strace -c`, `perf`).
- **Principal:** You treat the user/kernel boundary and the page cache as primary design surfaces — choosing io_uring vs. epoll, structuring data flow to be zero-copy, defining durability guarantees, and predicting throughput from crossing counts and cache hit rates. "Fewer, fatter crossings" and "is it warm?" are reflexes.

One idea climbing: *real work requires crossing into the kernel, the crossing and the slow I/O behind it both cost, and all of I/O engineering is crossing rarely, carrying much, caching aggressively, and never burning a thread to wait.*

## Complexity Analysis

| Operation | Cost | What's happening |
|-----------|------|------------------|
| Function call (user space) | ~1 ns | Jump within your code; no wall crossing |
| Syscall (round trip) | ~100–500 ns | User→kernel→user mode transition + mitigations |
| `gettimeofday` via vDSO | ~few ns | Serviced in user space; no crossing at all |
| `read` (page-cache hit, warm) | ~µs | Copy from kernel page cache to your buffer |
| `read` (page-cache miss, cold) | ~ms | Disk I/O / major fault; thread blocks |
| `write` (buffered) | ~µs | Copy to page cache, mark dirty, return — **not durable** |
| `fsync` | ~ms | Force dirty pages to physical disk; waits for storage |
| `select`/`poll` | O(N) per call | Re-scan all watched fds every call — the C10K wall |
| `epoll_wait` | O(ready) | Kernel returns only ready fds — scales to 10⁴–10⁶ fds |
| `sendfile` (zero-copy) | one syscall, no user copy | Page cache → socket inside the kernel |
| io_uring batch | ~0 syscalls/op amortized | Shared SQ/CQ rings; kernel polls submissions |

The spread that defines the chapter: a user-space call is ~1 ns, a syscall is ~100–500 ns, a warm read is ~µs, a cold read or fsync is ~ms. Six orders of magnitude, and *which one you hit* is mostly determined by how you batched crossings and whether the data was warm.

## War Stories (the shape of the bug in the wild)

- **The byte-at-a-time reader.** A log processor read input one byte per `read()` syscall and took minutes on files that should parse in seconds. The bytes weren't the cost — a hundred million syscalls were. Wrapping the fd in a 64 KB buffered reader cut runtime ~100×. Correct code, paying the syscall tax a hundred million times.
- **The data that "saved" but vanished on power loss.** A service `write()`-then-reported-success, and lost the last seconds of data on every hard crash. `write()` only dirtied the page cache; durability needed `fsync()`. The fix (fsync on commit, or a proper WAL) traded a little latency for not losing data — exactly the trade databases make deliberately.
- **The server that died at 10K connections.** A service using `poll()` scaled fine to a few thousand connections, then fell over — each event loop iteration re-scanned every fd, O(N), and at 10K idle-but-watched connections the scanning ate the CPU. Switching to `epoll` (O(ready)) flattened it; the active work was tiny, the *scanning* was the cost.
- **The static-file server pegged on memcpy.** A file server's CPU was dominated by copying file bytes through user space to sockets. Switching to `sendfile` (zero-copy, page-cache→socket in-kernel) dropped CPU dramatically and raised throughput to near wire speed — the same trick that gives Kafka its numbers.

## Key Takeaways

1. **A syscall is a mode transition across a wall, not a function call** — ~100–500 ns each (doubled by Spectre/Meltdown mitigations). The whole art of I/O performance is *fewer, fatter crossings*: buffer, batch, gather/scatter, and cache translations of time (vDSO).
2. **Buffered I/O exists to amortize the syscall tax.** Byte-at-a-time `read`/`write` in a hot loop is a correct, invisible, ~100× performance bug; always buffer.
3. **The page cache makes warm file reads ~1000× faster than cold ones.** Read-heavy systems are designed around cache hit rate, and benchmarks must separate cold from warm or they're fiction.
4. **`write()` returning ≠ durable.** Buffered writes just dirty the page cache; durability requires the expensive `fsync()`. This gap is where WALs, commit latency, and "lost data on power failure" live.
5. **Blocking I/O burns a thread per wait, capping concurrency.** Non-blocking + **readiness notification** breaks the cap: `epoll`/`kqueue` return only the ready fds (O(ready)), where `select`/`poll` re-scan everything (O(N)). This O(ready) scaling solved C10K and underlies every event loop.
6. **Zero-copy (`sendfile`, `mmap`, `splice`) stops copying bytes you're only passing through** — page cache straight to socket, in-kernel — which is how Nginx and Kafka hit near-wire-speed.
7. **io_uring attacks the wall itself** with shared submission/completion rings, approaching syscall-free batched I/O for both files and sockets — the current frontier and the logical endpoint of "fewer, fatter crossings."

## Related Modules

- `05-virtual-memory.md` — the page cache (the heart of file I/O), mmap, and the major faults a cold read triggers are all virtual-memory machinery
- `01-processes-and-threads.md` — why a blocked thread is expensive, and how async runtimes park goroutines on epoll instead of blocking OS threads
- `04-scheduling.md` — a blocking syscall yields the CPU (a voluntary context switch); how I/O waits interact with the scheduler
- `02-memory-management.md` — user buffers vs. kernel buffers, and the copies zero-copy eliminates
- `../05-network-programming/02-multiplexing-epoll-kqueue.md` — epoll/kqueue and event loops applied to network servers in depth
- `../06-databases/` — `fsync`, the WAL, and durability guarantees built on the buffered-write-vs-durable-write distinction
