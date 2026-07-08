---
title: Goroutines and the GMP Scheduler
description: What a goroutine really is, how the G-M-P scheduler multiplexes millions of them onto a few OS threads, work-stealing, preemption, syscalls and the netpoller, and how to read scheduler behavior in production.
tags: ["go", "scheduler", "goroutines", "concurrency", "runtime", "gmp"]
status: published
access: public
publishedAt: 2026-07-08
---

# Goroutines and the GMP Scheduler

## Learning objectives

You will understand what a goroutine is at the runtime level, how the G-M-P model schedules millions of goroutines onto a handful of OS threads, how work-stealing balances load, how blocking syscalls and network I/O are handled without wasting threads, how preemption prevents a hot loop from starving everyone, and how to observe all of this in a running service.

## Why this matters

The reason you can write `go handleRequest(conn)` per connection and serve a million concurrent connections on a laptop — something that would melt a thread-per-connection C++ or Java (pre-Loom) server — is the Go scheduler. Every performance conversation about a Go service eventually reaches it: "why is my p99 spiky," "why is one core pegged," "why did latency jump when I added a CGO call." Understanding the scheduler turns those from mysteries into diagnoses.

## The mechanics: goroutines are not threads

A goroutine is a **user-space, runtime-scheduled** unit of execution. Contrast with an OS thread:

```
                 OS thread                 Goroutine
Created by       kernel (clone/pthread)    Go runtime (`go` statement)
Stack            fixed, ~1–8 MB            starts ~8 KB, grows/shrinks
Scheduling       preemptive, by kernel     cooperative+preemptive, by Go runtime
Context switch   ~1–2 µs (kernel trap)     ~tens of ns (user space)
How many         thousands (RAM-bound)     millions
```

The two numbers that matter: a goroutine's stack starts at **8 KB** (vs a thread's megabytes), and switching between goroutines happens **in user space** without a kernel trap (~an order of magnitude cheaper than a thread context switch). That is the whole reason "just spawn a goroutine per unit of work" is viable Go advice and "spawn a thread per unit of work" is not.

Goroutine stacks are **growable**: they start small and, when a function call would overflow the stack, the runtime allocates a bigger stack, copies the frames over, and fixes up pointers (this is why Go pointers into stack memory are fine — the runtime knows about them). This is *contiguous stack growth*, and it is why a million idle goroutines cost ~8 GB in the worst case but usually far less.

## The G-M-P model

The scheduler has three entities. Learn these three letters; every scheduler discussion uses them:

```
G = Goroutine   — a unit of work: stack, instruction pointer, state.
M = Machine     — an OS thread. The thing the kernel actually schedules.
P = Processor   — a scheduling context / resource. Holds a run queue of Gs.
                  There are GOMAXPROCS of them (default = CPU count).
```

The rule that ties them together: **an M must hold a P to run Go code.** The number of Ps (`GOMAXPROCS`) caps how many goroutines run *in parallel*; the number of Ms can exceed it (threads blocked in syscalls don't hold a P). Each P has a **local run queue** (LIFO-ish, up to 256 Gs) plus there is one **global run queue**.

```
        GOMAXPROCS = 4

   P0        P1        P2        P3          global run queue
 ┌────┐    ┌────┐    ┌────┐    ┌────┐        ┌──────────────┐
 │runq│    │runq│    │runq│    │runq│        │ G G G G ...   │
 │ GGG│    │ G  │    │ GG │    │    │◄─steal─│              │
 └─┬──┘    └─┬──┘    └─┬──┘    └─┬──┘        └──────────────┘
   M0        M1        M2        M3
   │         │         │         │
  CPU       CPU       CPU       CPU
```

The scheduling loop each P runs (`runtime.schedule`): pick the next G to run — from the local queue, occasionally from the global queue (to avoid starvation), and if the local queue is empty, **steal** half the Gs from another P's queue. Run it until it blocks, yields, finishes, or is preempted; then repeat.

## Work-stealing

When a P's local run queue empties, its M does not go idle — it becomes a **thief**: it picks a random other P and steals **half** of that P's runnable goroutines. This keeps all Ps busy without a central bottleneck and is why Go balances uneven workloads well. It also picks up timers and a share of the global queue. The design goal is that no P sits idle while another has a backlog — load balancing with minimal coordination.

## Blocking syscalls and the netpoller

This is the part that makes Go servers scale, and the part most engineers never learn. What happens when a goroutine makes a blocking call?

**Blocking syscall (e.g. reading a file, a CGO call):** the M is about to block in the kernel, taking its P with it — which would waste a whole `GOMAXPROCS` slot. So the runtime **hands off the P**: it detaches the P from the blocking M and either wakes a parked M or spawns a new one to take over that P and keep running other goroutines. When the syscall returns, the original M tries to reacquire a P; if it can't, its goroutine goes back on a run queue. Net effect: a blocking syscall costs you one extra OS thread, not one of your parallelism slots.

```
M0 holds P0, running G1. G1 makes a blocking read():
   1. runtime detaches P0 from M0 (handoff)
   2. M0 + G1 block in the kernel
   3. P0 attaches to a fresh/parked M4, keeps running G2, G3, ...
   4. read() returns; M0 looks for a P; if none, G1 waits on a run queue
```

**Network I/O (the common case):** Go does *not* block an M per connection. All network file descriptors are registered with the **netpoller**, an event-notification layer over `epoll` (Linux), `kqueue` (BSD/macOS), or IOCP (Windows). When `conn.Read` would block, the goroutine is **parked** and the fd handed to the netpoller; the M is free to run other goroutines. When the fd becomes readable, the netpoller marks the goroutine runnable and a P picks it up. This is how one Go process serves a million connections with only `GOMAXPROCS` threads doing work — it is an event loop under the hood, but you write straight-line blocking code and the runtime turns it into async I/O for you.

This is the single biggest ergonomic win of Go: **you write synchronous-looking code, and get epoll-based async scalability**, with no callbacks, futures, or `async/await` coloring your functions.

## Preemption

If a goroutine runs a tight loop with no function calls, can it hog a P forever and starve others? Historically (pre-1.14) — yes, this was a real problem, because Go used **cooperative** preemption: a goroutine only yielded at function-call safepoints (where the stack-growth check lives). A CPU-bound loop with no calls never hit a safepoint.

Since **Go 1.14**, the runtime does **asynchronous preemption**: the `sysmon` background thread notices a goroutine that has run more than ~10 ms and sends the M a signal (`SIGURG` on Unix); the signal handler stops the goroutine at a safe point and reschedules. So a hot loop can now be preempted. This matters for latency: without it, one CPU-bound goroutine could delay GC (which needs all goroutines to reach a safepoint) and starve request handlers. The `sysmon` thread also retakes Ps from long syscalls and manages timers — it is the runtime's watchdog, running without a P of its own.

## Production engineering: what actually goes wrong

- **`GOMAXPROCS` in containers.** By default `GOMAXPROCS` equals the number of CPUs the OS reports. In a container with a CPU *limit* (cgroup quota) but many host cores visible, older Go versions set `GOMAXPROCS` to the host core count, creating far more parallelism than the quota allows — causing throttling and latency spikes. For years the fix was `uber-go/automaxprocs`. **Go 1.25 (2025)** made the runtime **container-aware**, honoring cgroup CPU limits by default. Know which Go version your service runs; on older ones, set `GOMAXPROCS` from the cgroup quota.
- **Goroutine leaks.** A goroutine blocked forever on a channel or a `<-ctx.Done()` that never fires is never collected — its stack and everything it references stays live. Leaked goroutines are the #1 Go memory leak. Watch the `go_goroutines` metric (or `runtime.NumGoroutine()`); a monotonic climb is the signature.
- **Unbounded goroutine creation.** `go handle(x)` per incoming item with no limit lets a burst spawn millions of goroutines, exhausting memory and drowning the scheduler. Bound concurrency with a worker pool or a semaphore (`golang.org/x/sync/semaphore`, or a buffered channel as a token bucket).
- **CGO and blocking calls.** Every CGO call and blocking syscall can cost an extra OS thread (via handoff). A flood of them inflates the thread count (`runtime.ReadMemStats` / `pprof` threadcreate), which has its own memory and scheduling cost. The default thread cap is 10,000 (`runtime/debug.SetMaxThreads`); hitting it crashes the process.

## Observing the scheduler

```
GODEBUG=schedtrace=1000 ./server
# every 1000ms: number of Ps, idle Ps, Ms, run-queue lengths, ...
SCHED 1000ms: gomaxprocs=4 idleprocs=0 threads=8 spinningthreads=1 runqueue=12 ...

GODEBUG=scheddetail=1,schedtrace=1000 ./server   # per-P, per-M detail

go tool trace trace.out   # visual timeline: per-P goroutine execution, GC, syscalls, netpoll
```

`runqueue=12` persistently high with `idleprocs=0` means you are CPU-bound (more work than cores). High `threads` with low CPU use suggests syscall/CGO thread inflation. The `execution trace` (`go tool trace`) is the definitive view — it shows exactly which goroutine ran on which P when, where GC stole time, and where goroutines blocked. Reach for it when latency is spiky and CPU profiles don't explain it.

## Common mistakes

- Spawning unbounded goroutines per request/item; no concurrency limit.
- Launching goroutines with no stop condition (no context, no done channel) → leaks.
- Assuming `GOMAXPROCS` matches your container's CPU quota on Go < 1.25.
- Believing goroutines are parallel — they are *concurrent*; parallelism is capped at `GOMAXPROCS`. On one core, goroutines interleave, they do not run simultaneously.
- Expecting a tight CPU loop to yield on old runtimes; add work or rely on 1.14+ async preemption.

## Best practices

- Bound concurrency: worker pools or semaphores, never unbounded `go`.
- Give every goroutine a lifetime: a `context` and a `select { case <-ctx.Done(): return }`.
- Monitor `go_goroutines`; alert on unbounded growth.
- On Go < 1.25 in containers, set `GOMAXPROCS` from the cgroup quota (automaxprocs).
- Keep CGO/blocking syscalls off the hottest paths, or pool them, to avoid thread inflation.

## Performance analysis

The scheduler's cost is mostly invisible until it isn't. A useful mental cost model: a goroutine switch is tens of nanoseconds; a channel operation that parks/unparks a goroutine is on the order of ~100 ns; a blocked network read costs nothing but a park (the M runs other work). Compare to a thread context switch (~1–2 µs) and the scaling advantage is clear. When you *do* see the scheduler in a profile, it is usually `runtime.schedule`, `runtime.findrunnable`, or `runtime.gcBgMarkWorker` showing up — those point at excessive goroutine churn, lock contention forcing parks, or GC pressure, respectively.

## Production case study

A gRPC service showed periodic latency spikes every few seconds that CPU profiles didn't explain. `go tool trace` revealed the cause: a background job iterated a huge in-memory structure in a tight loop with no function calls, and on the (pre-1.14) runtime it could not be preempted, so it monopolized a P for hundreds of milliseconds at a time and delayed the GC's stop-the-world phase (which waits for every goroutine to reach a safepoint) — stalling all request handlers. Two fixes applied: upgrading the runtime (async preemption made the loop preemptible), and chunking the background work so it yielded regularly. The spikes vanished. The diagnostic lesson: when latency is spiky and CPU graphs look fine, the *scheduler trace*, not the CPU profile, holds the answer.

## Exercises

1. Spawn 1,000,000 goroutines that each sleep, and measure process memory. Compute the per-goroutine cost and compare to a thread.
2. Run a program with `GOMAXPROCS=1` and two CPU-bound goroutines; use `GODEBUG=schedtrace=1000` to watch them share the single P. Then set `GOMAXPROCS=2` and compare.
3. Write a server that leaks a goroutine per request (blocked on a channel no one sends to). Watch `runtime.NumGoroutine()` climb. Fix it with a context.
4. Capture a `go tool trace` of a load test and identify a GC stop-the-world pause and a goroutine being preempted.

## Summary

- A goroutine is a cheap, user-scheduled unit with a small growable stack (~8 KB start). Switching is a user-space operation (~tens of ns), so millions of goroutines are practical.
- The **G-M-P** model multiplexes goroutines (G) onto OS threads (M) via scheduling contexts (P). `GOMAXPROCS` = number of Ps = parallelism cap. Each P has a local run queue; idle Ps **steal** work.
- **Blocking syscalls** hand off the P (cost: an extra thread); **network I/O** parks the goroutine and uses the **netpoller** (epoll/kqueue) — you write blocking code, get async scalability.
- **Async preemption** (Go 1.14+) stops long-running goroutines via signals so they can't starve others or block GC. `sysmon` is the watchdog.
- Production hazards: unbounded goroutines, leaks (no stop condition), `GOMAXPROCS` vs container quotas (fixed by default in **1.25**), and CGO/syscall thread inflation. Diagnose with `schedtrace` and `go tool trace`.

Next → [Channels: the runtime structure and correct usage](/backend-guide/go/03-runtime/02-channels)
