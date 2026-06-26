# Scheduling

## Problem

You have 8 CPU cores. Your server is running 400 threads. The arithmetic is brutal and unavoidable: at any instant, *at most 8 threads are actually running*, and the other 392 are waiting for a turn. Something has to decide, thousands of times per second, which 8 get the cores right now and which 392 keep waiting — and that something is the **scheduler**, a piece of the kernel you never call, never see, and whose decisions nonetheless determine your tail latency, your throughput, and whether your "idle" service is mysteriously burning CPU. The scheduler is the invisible hand on every thread in your system, and most engineers go their whole careers without once thinking about how it makes its choices. That's fine until the day its choices become your incident.

Here's the tension the scheduler exists to resolve, and it's a genuinely hard one because the two goals *fight each other.* You want **throughput** — get the most total work done — which argues for letting each thread run a long time before switching, because every switch is wasted overhead (chapter 01's TLB flush and cache pollution). But you also want **low latency / fairness** — when an interactive request arrives, it should run *soon*, not wait behind some batch job hogging a core for 200 ms — which argues for switching *often* so everyone gets a quick turn. Run threads too long and your interactive traffic gets janky; switch too often and you drown in context-switch overhead. Every scheduler is a particular answer to "how do I balance responsiveness against the cost of switching," and Linux's answer (the Completely Fair Scheduler) is a genuinely elegant one worth understanding.

But the reason this chapter matters *urgently* for a backend engineer in 2026 isn't the classic theory — it's containers. The moment you deploy to Kubernetes with a CPU limit, you've handed the scheduler a second job: **CPU throttling.** Your container says `limits.cpu: "1"`, and now the kernel will *forcibly stop your threads* — freeze them, mid-work — whenever they've used their slice of CPU time in the current window, even if cores are sitting idle. This produces one of the most baffling and widespread production symptoms of the cloud era: a service with "20% CPU utilization" on the dashboard that nonetheless has terrible p99 latency, because it's being throttled in bursts the average hides. If you've ever stared at a latency graph that made no sense against a calm CPU graph, the scheduler — specifically cgroup CPU throttling — was very likely the culprit, and by the end of this chapter you'll know exactly how to see it and fix it.

## Why It Matters (Latency, Throughput, Cost)

**The throughput-vs-latency trade is set by the time slice, and it's a real dial.** The scheduler gives each thread a slice of CPU before considering a switch. Longer slices → fewer switches → less overhead → higher throughput, but interactive requests wait longer for a turn → worse latency. Shorter slices → snappier response → more switching overhead → lower throughput. Linux's CFS targets a *scheduling latency* (roughly, the period in which every runnable thread should get to run once — a few milliseconds to tens of ms) and divides it among runnable threads. When you have far more runnable threads than cores, each one's slice shrinks and switching overhead climbs — which is the scheduling-side explanation for chapter 01's "too many threads makes you slower": it's not just memory, it's the scheduler forced to slice thinner and switch more.

**CPU throttling turns "low utilization" into "high latency," and it's everywhere in containers.** A cgroup CPU limit is enforced over a window (default 100 ms): your container gets a *quota* of CPU-time per window, and once it's spent, every thread is **throttled** — stopped until the next window — regardless of idle cores. A multithreaded service can burn its whole 100 ms quota in 25 ms using 4 threads, then sit *frozen for 75 ms*. Average CPU over the window: 25%. Reality: three-quarters of every window, fully stopped, and any request unlucky enough to span a throttle period eats up to ~75 ms of pure stall. This is the mechanism behind the infamous "we set a CPU limit and our p99 got *worse* while utilization looks fine" — and it's why a huge amount of real-world Kubernetes performance tuning is really *scheduling* tuning. `cat /sys/fs/cgroup/cpu.stat` and look at `nr_throttled` / `throttled_time`; if those are climbing, the scheduler is freezing you.

**Where a thread runs matters as much as whether it runs (cache & NUMA affinity).** Moving a thread from core A to core B means its warm cache data is on the wrong core — it starts cold and re-warms L1/L2 from scratch (arrays chapter). On multi-socket (NUMA) machines it's worse: a thread moved to a different socket now accesses memory that's physically attached to its *old* socket, paying a remote-memory penalty on every access. So the scheduler tries to keep threads on the same core (cache affinity) and the same NUMA node (memory locality), and high-performance services (databases, low-latency trading, packet processors) often *pin* threads to specific cores (`taskset`, `sched_setaffinity`) to forbid migration entirely. The scheduler's placement decisions, not just its timing decisions, show up in your latency.

## Mental Model

The cleanest way to understand a fair scheduler is to imagine it as an **accountant tracking how much CPU time each thread has consumed, always running whoever has gotten the least.**

```
   Each runnable thread carries a running tally: "CPU time I've consumed" (vruntime)

   thread A: ▓▓▓▓▓▓▓▓ 80ms        the scheduler ALWAYS picks the thread with the
   thread B: ▓▓ 20ms     ◄── run  SMALLEST tally → here, B. B runs, its tally grows,
   thread C: ▓▓▓▓ 40ms            eventually another thread becomes the smallest.
   thread D: ▓▓▓▓▓▓ 60ms

   result: over time, everyone's tally stays roughly equal → "completely fair"
```

That's the core of Linux's **Completely Fair Scheduler (CFS)** in one picture: every thread accumulates **virtual runtime** (vruntime) as it runs, and the scheduler's rule is dead simple — *always run the thread with the lowest vruntime.* A thread that's been running a lot has a high tally and gets deprioritized; a thread that just woke up from waiting has a low tally and gets to run soon. Fairness emerges automatically from "always serve whoever's had the least." No explicit time slices to tune, no priority queues to balance — just a single ordering by accumulated runtime. (Implementation note for later: CFS keeps threads in a **red-black tree** ordered by vruntime, so "find the lowest" is O(log N) — chapter 03 of module 02's balanced tree, doing real work in the kernel's hottest path.)

Two refinements complete the model. First, **priorities (nice values) bend the tally's growth rate**: a high-priority thread accumulates vruntime *slower* (as if its time counts for less), so it stays "behind" longer and runs more often — fairness, but weighted. Second, and this is the crucial reframing for backend work: **this fairness is per-cgroup-aware, and cgroups impose a hard ceiling on top.** Fairness decides *who runs among the runnable*; throttling decides *whether your group is allowed to run at all right now.* CFS can be scrupulously fair to your threads and the cgroup controller can still freeze every one of them because your container spent its quota. Hold both: a fair accountant *inside* each container, and a hard spending limit *around* it.

## Underlying Theory

### Layer 1 — What "schedule" means and when it happens

The scheduler isn't a thread that runs continuously watching things — it's a *function* (`schedule()` in Linux) that runs at specific moments to pick the next thread for a CPU. It's invoked when:

- a **timer interrupt** fires (periodically, so a CPU-bound thread can't run forever without the scheduler getting a chance to preempt it — this is *preemption*),
- a thread **blocks** (on I/O, a lock, a sleep) and *voluntarily* gives up the CPU,
- a thread **wakes up** (its I/O completed) and might deserve to run,
- a thread **exits**.

The distinction between **preemption** (the kernel forcibly takes the CPU from a running thread at a timer tick) and **voluntary yield** (a thread blocks and gives it up) is foundational. Preemption is what guarantees fairness and responsiveness — without it, one CPU-bound infinite loop would hog a core forever and starve everyone else. Voluntary yields are what make I/O-bound workloads efficient — a thread waiting on the network isn't burning a core. A key consequence: I/O-bound threads (lots of voluntary yields, little CPU used) keep a *low* vruntime, so when their I/O completes they get scheduled *quickly* — CFS naturally favors interactive/I-O work for latency without any special-casing, purely because such threads have consumed little and thus sit at the front of the "least consumed" ordering.

### Layer 2 — CFS and virtual runtime, precisely

Now make the accountant rigorous. Each thread has a **vruntime** that increases as it runs, scaled by its weight (from its nice value). The scheduler picks the runnable thread with the minimum vruntime, runs it, and as it runs its vruntime climbs until it's no longer the minimum — then the next-lowest runs. Over time all runnable threads' vruntimes track together: that's fairness.

CFS doesn't use fixed time slices. Instead it has a target **scheduling latency** — the window in which it wants every runnable thread to run at least once (say ~6–24 ms, auto-tuned by thread count). It divides that window among runnable threads weighted by priority, giving each a dynamically-sized slice. With 3 runnable threads and a 24 ms target, each gets ~8 ms before the next runs; with 100 runnable threads, slices shrink toward a floor (`sched_min_granularity`, ~0.75–3 ms) to avoid switching so often that overhead dominates. *This* is the throughput-vs-latency dial made concrete and self-adjusting: more runnable threads → thinner slices → more switching → the overhead chapter 01 warned about. When people say "the box is overloaded," part of what they mean is the scheduler is slicing below the granularity floor and burning cycles on switches.

The data structure makes it efficient: runnable threads live in a **red-black tree keyed by vruntime**, so "pick the minimum" is the leftmost node (O(1) cached, O(log N) to rebalance on insert/remove). The kernel's most performance-critical decision, made millions of times a second, is a balanced-BST lookup — the exact structure from module 02. (Newer kernels, 6.6+, replace CFS with **EEVDF** — Earliest Eligible Virtual Deadline First — which adds latency deadlines for better interactivity, but the vruntime-fairness intuition carries over.)

### Layer 3 — Priorities, niceness, and scheduling classes

Not all work is equal, and Linux layers this in two ways. Within normal scheduling, the **nice value** (−20 to +19; lower = higher priority) adjusts a thread's vruntime growth weight — a niced-down (+19) batch job accumulates vruntime fast and yields the CPU readily to interactive threads, while a niced-up (−20) thread accumulates slowly and dominates. Niceness is *relative pressure*, not a hard guarantee.

Above normal scheduling sit **scheduling classes** with strict precedence: real-time classes (`SCHED_FIFO`, `SCHED_RR`) always preempt normal (`SCHED_OTHER`/CFS) threads — a real-time thread runs until *it* yields, used for latency-critical work like audio or packet processing where a millisecond of jitter is unacceptable (and a buggy `SCHED_FIFO` loop can hang a core, which is why it's privileged). Below normal sits `SCHED_IDLE` for "only run this when literally nothing else wants the CPU." The mental model: classes are a strict hierarchy (real-time beats normal beats idle), and *within* the normal class, CFS's fair vruntime ordering (bent by nice) decides. For backend services you live almost entirely in CFS/`SCHED_OTHER`; the classes matter when you have genuine real-time constraints or want to firmly deprioritize background work.

### Layer 4 — Multicore: run queues, load balancing, and affinity

A single global run queue across all cores would be a scalability disaster — every scheduling decision on every core contending on one lock (the same lesson as the allocator in chapter 02). So Linux keeps a **per-CPU run queue**: each core schedules largely independently from its own red-black tree, no global lock in the hot path. The cost of that independence is that queues can become *unbalanced* — 5 threads piled on core 0 while core 3 sits idle — so a periodic **load balancer** migrates threads between cores to even things out.

But migration isn't free, and here's where placement becomes a latency story. Moving a thread to a new core abandons its warm L1/L2 cache — it restarts cold and re-warms from L3 or memory (arrays chapter, again). On a **NUMA** machine (multiple CPU sockets, each with its own attached RAM), migrating across sockets is far worse: the thread's memory is now *remote*, attached to the old socket, so every access pays a cross-socket penalty (~1.5–2× latency) until/unless its pages migrate too. So the scheduler balances a tension: spread load for utilization, but respect **cache and NUMA affinity** to preserve locality. It prefers migrating within a socket, and prefers not migrating at all. When you need certainty, you override it: **pin** threads to cores with `taskset`/`sched_setaffinity` and `numactl` for memory, forbidding migration so a latency-critical thread keeps its warm cache and local memory. Databases, JVMs with large heaps, and packet-processing fast paths routinely pin for exactly this reason — the scheduler's *placement* is part of their tail latency.

### Layer 5 — cgroups and CPU throttling: the container reality

This is the layer that turns scheduling from academic to urgent for anyone on Kubernetes. **Control groups (cgroups)** let the kernel account and limit CPU per group of processes — that's how your container's `requests` and `limits` are enforced — and there are *two distinct mechanisms* people constantly conflate:

- **CPU shares / weight (`requests.cpu`)** — *proportional, soft.* It's relative priority under CFS: a container with 2000 shares gets twice the CPU of one with 1000 *when the CPUs are contended.* When CPUs are idle, a container can freely use more than its share. This is fairness-under-contention, and it (almost) never hurts you — you only get less than you "asked for" when someone else needs it too.
- **CPU quota / bandwidth (`limits.cpu`)** — *absolute, hard, and the dangerous one.* It's a quota of CPU-time per period (default 100 ms): use it up and **every thread in the cgroup is throttled — frozen — until the next period**, even if every core is idle. A `limits.cpu: "1"` means "1 CPU-second per wall-second," enforced in 100 ms slices of 100 ms quota.

The throttling failure mode, precisely, because it's *the* container performance bug:

```
limits.cpu = "1"  →  100ms quota per 100ms period.
Service has 4 worker threads. A burst of work arrives.
  t=0–25ms:   4 threads run flat-out → consume 4×25 = 100ms of CPU-time → quota GONE
  t=25–100ms: ALL THREADS THROTTLED (frozen), even though cores are idle
  Average CPU over the period: 25%.   Reality: frozen 75% of every period.
  A request that arrives at t=30ms waits ~70ms doing nothing → garbage p99.
```

The dashboard shows 25% CPU and the p99 is a disaster, and the two facts look contradictory until you know throttling exists. The signal is in `cpu.stat`: `nr_throttled` and `throttled_time` climbing. The fixes are real and well-known: raise or *remove* the CPU limit (keep `requests` for fairness, drop the hard `limits` cap — a widely-recommended practice precisely because of this), reduce thread count so you can't burn quota in a burst, or right-size the limit to actual parallelism. A staggering amount of "Kubernetes made our service slow" is this one mechanism, and recognizing it is one of the highest-leverage things in this entire module.

## A Ladder From L1 to Principal

- **L1 / new grad:** The OS shares CPUs among many threads; only as many threads run as there are cores; the scheduler picks who runs. You know more threads than cores means time-sharing.
- **L3–L4 / solid engineer:** You understand preemption vs. yielding, that fairness comes from running who's-had-least (CFS/vruntime), and that priorities (nice) bend it. You know too many runnable threads means more switching overhead.
- **Senior:** You reason about cache/NUMA affinity and migration cost, when to pin threads, and — critically — you recognize cgroup CPU *throttling* as the cause of high-latency-at-low-utilization in containers, and read `cpu.stat` to confirm it.
- **Staff:** You tune the system — CPU limits vs. requests, thread-pool sizing against cores and quota, affinity/NUMA for latency-critical paths, real-time classes where justified — and diagnose scheduler-induced tail latency in production.
- **Principal:** You design for the scheduler — concurrency level matched to cores and quota, isolation and placement policy across a fleet, throttle-free latency budgets — and you predict tail-latency behavior from thread count, core count, and cgroup limits before it becomes an incident. "Fair inside, capped outside" and "throttling hides behind low utilization" are reflexes.

One idea climbing: *more work than cores means someone always waits, the scheduler decides who and for how long, and your latency is the sum of its timing decisions (slices, preemption) and its placement decisions (cores, NUMA) — capped, in the cloud, by a hard quota that freezes you when spent.*

## Complexity Analysis

| Operation / concept | Cost / behavior | What's happening |
|---|---|---|
| Pick next thread (CFS) | O(1) cached min, O(log N) update | Leftmost node of a red-black tree keyed by vruntime |
| Preemption (timer tick) | a context switch (chapter 01) | Forcibly take the CPU to preserve fairness/responsiveness |
| Voluntary yield (block on I/O) | a context switch | Thread gives up CPU; keeps low vruntime → fast re-schedule |
| More runnable threads than cores | thinner slices, more switches | Slice shrinks toward `min_granularity`; overhead rises |
| Thread migration (load balance) | cold cache; cross-NUMA = remote memory | Lost L1/L2 warmth; ~1.5–2× memory penalty across sockets |
| CPU shares (`requests`) | proportional, soft | Relative CFS weight; only bites under contention |
| CPU quota (`limits`) | hard freeze on exhaustion | Throttled until next 100 ms period, **even if cores idle** |

The one row that causes the most production pain is the last: a *hard* cap that stops your threads mid-work and hides behind a calm average-utilization graph.

## War Stories (the shape of the bug in the wild)

- **High p99, 25% CPU, total confusion.** A containerized service had awful tail latency while its CPU dashboard sat calm at ~25%. `cpu.stat` told the truth: `nr_throttled` was climbing — the service burned its `limits.cpu` quota in bursts and froze for the rest of each 100 ms window. Removing the hard CPU limit (keeping requests) erased the tail. The contradiction between the graphs *was* the diagnosis.
- **The thread pool that fought the scheduler.** A service ran 200 worker threads on 8 cores "for throughput." Under load, latency got *worse* — the scheduler sliced thinner and switched constantly, and the CPU-limit quota vanished in milliseconds. Cutting the pool to ~2× cores improved both latency and throughput. More threads than cores is rarely the answer.
- **The latency-critical thread that kept going cold.** A low-latency path had unpredictable spikes; the load balancer kept migrating its thread across cores (and sometimes sockets), abandoning warm cache and hitting remote NUMA memory. Pinning it with `taskset` + `numactl` to a fixed core and local memory node flattened the jitter.
- **The batch job that starved the API.** A nightly batch job ran at default priority on the same box as the API and stole cores during traffic, spiking API latency. `nice`-ing the batch job down (and ultimately moving it to its own cgroup with low shares) let CFS yield the CPU to interactive work the moment it arrived.

## Key Takeaways

1. **The scheduler decides who runs when there's more work than cores**, balancing throughput (run long, switch rarely) against latency/fairness (switch often, respond fast). That trade is the dial behind a huge fraction of performance behavior.
2. **CFS is a fair accountant:** every thread accrues vruntime as it runs, and the scheduler always runs the one with the least — fairness emerges from "serve whoever's had the least," kept in a red-black tree (O(log N)). I/O-bound threads stay low and get scheduled fast, favoring interactivity for free.
3. **Preemption (forced at timer ticks) guarantees responsiveness; voluntary yields (blocking) make I/O efficient.** Nice values bend vruntime growth; scheduling classes (real-time > normal > idle) form a strict hierarchy above it.
4. **More runnable threads than cores means thinner slices and more switching overhead** — the scheduling-side reason "too many threads" makes you slower (chapter 01's other side).
5. **Placement is latency:** migrating a thread loses its warm cache, and across NUMA sockets it pays a remote-memory penalty. Pin latency-critical threads (`taskset`/`numactl`) to preserve locality.
6. **cgroup CPU has two mechanisms:** `requests`/shares are *soft, proportional* (only bite under contention) and rarely hurt; `limits`/quota are a *hard cap* that **throttles — freezes — all your threads when the quota is spent, even with idle cores.**
7. **CPU throttling is *the* container performance bug:** it produces high p99 at low average utilization, the two graphs looking contradictory until you check `nr_throttled`/`throttled_time`. Fix by removing/raising the limit (keep requests) or cutting thread count. Recognizing it is one of the highest-leverage skills in this module.

## Related Modules

- `01-processes-and-threads.md` — the context switch the scheduler triggers (its cost: TLB flush, cache pollution), and why thread count interacts with scheduling overhead
- `05-virtual-memory.md` — the TLB flush on cross-process switches and how memory pressure compounds scheduling latency
- `03-io-and-syscalls.md` — blocking I/O as a voluntary yield; how I/O-bound threads keep low vruntime and reschedule quickly
- `02-memory-management.md` — per-CPU run queues mirror per-thread allocator caches (avoid the global lock); GC stop-the-world pauses as a scheduling/latency event
- `../02-data-structures-and-algorithms/03-trees-and-indexing.md` — the red-black tree CFS uses to pick the next thread, doing real work in the kernel's hottest path
- `../09-performance-engineering/` — diagnosing scheduler-induced tail latency, reading `cpu.stat`, and right-sizing limits and pools
