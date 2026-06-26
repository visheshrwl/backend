# Scheduling

## Problem

You have 8 CPU cores. Your server is running 400 threads. The arithmetic is brutal and unavoidable: at any instant *at most 8 threads are actually running*, and the other 392 are waiting for a turn. Something has to decide, thousands of times per second, which 8 get the cores right now and which 392 keep waiting — and that something is the **scheduler**, a piece of the kernel you never call, never see, and whose decisions nonetheless set your tail latency, your throughput, and whether your "idle" service is mysteriously burning CPU. The scheduler is the invisible hand on every thread in your system, and most engineers go their whole careers without once thinking about how it chooses. That's fine until the day its choices become your incident.

The tension it exists to resolve is genuinely hard because the two goals *fight*. You want **throughput** — most total work done — which argues for letting each thread run a long time before switching, because every switch is wasted overhead (chapter 01's TLB flush and cache pollution). But you also want **low latency / fairness** — when an interactive request arrives, it should run *soon*, not wait behind a batch job hogging a core for 200 ms — which argues for switching *often*. Run threads too long and interactive traffic gets janky; switch too often and you drown in switch overhead. Every scheduler is a particular answer to "balance responsiveness against the cost of switching."

And in 2026 this matters *urgently* for backend engineers for one reason: **containers and CPU throttling.** The moment you deploy to Kubernetes with a CPU limit, you've handed the scheduler a second job — your container says `limits.cpu: "1"` and the kernel will now *forcibly freeze your threads* whenever they've spent their time slice in the current window, **even if cores are idle**. This produces one of the most baffling symptoms of the cloud era: a service at "20% CPU" on the dashboard with terrible p99, because it's throttled in bursts the average hides. If you've stared at a latency graph that made no sense against a calm CPU graph, the scheduler was the culprit. But this is just the surface. Underneath sit the genuinely hard things people break on and rarely learn: **how the throttling actually works** (the bandwidth period timer, slack, the burst feature), the scheduler that *replaced* CFS in 2023 (**EEVDF**), how the kernel even *measures* load to balance it (**PELT**), why a low-priority thread can starve a high-priority one (**priority inversion** — the bug that nearly killed a Mars mission), how real-time **deadline** scheduling works, and how you *isolate* CPUs from the scheduler entirely for latency-critical work. We're going to take all of it head-on — **Part I** is the fair-scheduling model and basic throttling; **Part II** is the hard internals.

## Why It Matters (Latency, Throughput, Cost)

**The throughput-vs-latency trade is set by the time slice, and it's a real dial.** The scheduler gives each thread a slice before considering a switch. Longer slices → fewer switches → less overhead → more throughput, but interactive requests wait longer. Shorter slices → snappier → more overhead → less throughput. Linux targets a *scheduling latency* (the window in which every runnable thread should run once — a few to tens of ms) and divides it among runnable threads. Far more runnable threads than cores → each slice shrinks → switching overhead climbs — the scheduling-side explanation for chapter 01's "too many threads makes you slower."

**CPU throttling turns "low utilization" into "high latency," and it's everywhere in containers.** A cgroup CPU limit is enforced over a window (default 100 ms): your container gets a *quota* of CPU-time per window, and once spent, every thread is **throttled** — frozen — until the next window, regardless of idle cores. A multithreaded service can burn its whole 100 ms quota in 25 ms using 4 threads, then sit frozen for 75 ms. Average CPU: 25%. Reality: three-quarters of every window stopped, and any request spanning a throttle period eats up to ~75 ms of stall. `cat /sys/fs/cgroup/cpu.stat` → if `nr_throttled`/`throttled_time` climb, the scheduler is freezing you.

**Where a thread runs matters as much as whether it runs.** Moving a thread from core A to core B abandons its warm cache (arrays chapter) — it restarts cold. On multi-socket (NUMA) machines it's worse: the thread now accesses memory attached to its *old* socket, a remote-memory penalty on every access (chapter 02's NUMA). So the scheduler tries to preserve cache and NUMA affinity, and latency-critical services *pin* threads to cores (`taskset`, `numactl`) to forbid migration. The scheduler's *placement* decisions, not just its timing, show up in your latency.

**The hard internals are where the deep incidents live.** Priority inversion can hang your highest-priority work behind your lowest. A misunderstanding of the bandwidth controller makes you set CPU limits that throttle you needlessly. Not knowing about CPU isolation means your latency-critical thread keeps getting interrupted by kernel housekeeping. These aren't trivia — they're the difference between a system whose tail latency you can *explain and control* and one that has unexplained spikes forever.

## Mental Model

Picture a fair scheduler as an **accountant tracking how much CPU time each thread has consumed, always running whoever's gotten the least.**

```
   Each runnable thread carries a tally: "CPU time I've consumed" (vruntime)
   thread A: ▓▓▓▓▓▓▓▓ 80ms        the scheduler ALWAYS picks the SMALLEST tally → B.
   thread B: ▓▓ 20ms     ◄── run  B runs, its tally grows, eventually another is smallest.
   thread C: ▓▓▓▓ 40ms
   thread D: ▓▓▓▓▓▓ 60ms          over time everyone's tally stays ≈ equal → "fair"
```

That's the core of Linux's long-running **Completely Fair Scheduler (CFS)**: every thread accrues **virtual runtime** (vruntime) as it runs, and the rule is "always run the lowest vruntime." Fairness emerges from "serve whoever's had least." A thread that just woke from waiting has a low tally → runs soon; a CPU-hog has a high tally → gets deprioritized. CFS keeps threads in a **red-black tree** keyed by vruntime, so "find the minimum" is O(log N) — the balanced tree from module 02 doing real work in the kernel's hottest path.

Two refinements complete the picture, and both become Part II's hard material. First, **priorities (nice) bend the tally's growth rate** — a high-priority thread accrues vruntime slower, so it stays "behind" and runs more. Second — and this is the crucial reframing for backend work — **fairness decides who runs among the runnable; cgroup throttling decides whether your group may run *at all* right now.** CFS can be scrupulously fair to your threads while the bandwidth controller freezes every one because the container spent its quota. Hold both: *a fair accountant inside each container, a hard spending limit around it.* And know that as of kernel 6.6 (2023), the "accountant" itself was replaced by a smarter one — **EEVDF** — that tracks not just "who's had the least" but "who's owed time and has the nearest deadline." That's where we're going.

---

## PART I — The Fair-Scheduling Model

### Layer 1 — What "schedule" means and when it happens

The scheduler isn't a thread watching continuously — it's a *function* (`schedule()`) that runs at specific moments to pick the next thread for a CPU. It's invoked when: a **timer interrupt** fires (so a CPU-bound thread can't run forever — *preemption*), a thread **blocks** (on I/O, a lock, sleep — *voluntary yield*), a thread **wakes** (its I/O completed), or a thread **exits**.

The distinction between **preemption** (kernel forcibly takes the CPU at a tick) and **voluntary yield** (a thread blocks and gives it up) is foundational. Preemption guarantees fairness and responsiveness — without it, one infinite loop hogs a core forever. Voluntary yields make I/O-bound work efficient — a thread waiting on the network isn't burning a core. A key consequence: I/O-bound threads (lots of yields, little CPU used) keep a *low* vruntime, so when their I/O completes they schedule *quickly* — fair schedulers naturally favor interactive work for latency, with no special-casing, purely because such threads sit at the front of the "least consumed" ordering.

### Layer 2 — CFS and virtual runtime, precisely

Each thread's **vruntime** increases as it runs, scaled by its weight (from nice). The scheduler runs the minimum-vruntime thread until it's no longer the minimum, then the next. CFS uses no fixed slices; it has a target **scheduling latency** (the window in which every runnable thread should run once, ~6–24 ms, auto-tuned by count) divided among runnable threads by weight. With 3 threads and a 24 ms target, each gets ~8 ms; with 100 threads, slices shrink toward a floor (`sched_min_granularity`, ~0.75–3 ms) to avoid switching so often that overhead dominates. *This* is the throughput-vs-latency dial, self-adjusting: more runnable threads → thinner slices → more switching. When people say "the box is overloaded," part of it is the scheduler slicing below the granularity floor and burning cycles on switches.

### Layer 3 — Priorities, niceness, and scheduling classes

Within normal scheduling, the **nice value** (−20 to +19; lower = higher priority) adjusts vruntime growth weight — a niced-down (+19) batch job accrues vruntime fast and yields readily; a niced-up (−20) thread accrues slowly and dominates. Niceness is *relative pressure*, not a guarantee. Above normal sit **scheduling classes** with strict precedence: real-time (`SCHED_FIFO`, `SCHED_RR`) always preempt normal (`SCHED_OTHER`); below sits `SCHED_IDLE`. The hierarchy is strict (real-time beats normal beats idle); *within* normal, fair vruntime ordering (bent by nice) decides. For backend services you live almost entirely in the normal class — but the real-time and deadline classes (Layer 10) matter when you have genuine latency constraints.

### Layer 4 — Multicore: run queues, load balancing, affinity

A single global run queue would be a scalability disaster — every scheduling decision on every core contending on one lock (the allocator lesson from chapter 02). So Linux keeps a **per-CPU run queue**: each core schedules from its own red-black tree, no global lock. The cost is imbalance — 5 threads on core 0, core 3 idle — so a periodic **load balancer** migrates threads to even out. But migration abandons warm L1/L2 cache, and across **NUMA** sockets it makes memory remote (~1.5–2× penalty until pages migrate). So the scheduler balances *spread load for utilization* against *preserve cache/NUMA affinity*, preferring within-socket migration and preferring not to migrate. When you need certainty you override it: **pin** threads (`taskset`/`sched_setaffinity`, `numactl`) so a latency-critical thread keeps its warm cache and local memory. Databases, big-heap JVMs, and packet fast-paths routinely pin.

### Layer 5 — cgroups and CPU throttling: the container reality (the basics)

**Control groups** let the kernel account and limit CPU per group — how `requests`/`limits` are enforced — via *two distinct mechanisms* people conflate:

- **CPU shares/weight (`requests.cpu`)** — *proportional, soft.* Relative priority under the fair scheduler: 2000 shares gets twice the CPU of 1000 *when contended*; when CPUs are idle, you can use more. Almost never hurts you.
- **CPU quota/bandwidth (`limits.cpu`)** — *absolute, hard, dangerous.* A quota of CPU-time per period (default 100 ms): use it up and **every thread in the cgroup is frozen until the next period, even if cores are idle.**

The throttling failure mode:

```
limits.cpu="1" → 100ms quota per 100ms period. 4 worker threads, a burst of work:
  t=0–25ms:   4 threads flat-out → 4×25 = 100ms CPU-time → quota GONE
  t=25–100ms: ALL THREADS FROZEN, even though cores are idle
  Average CPU: 25%.  Reality: frozen 75% of every period. A request at t=30ms waits ~70ms.
```

The dashboard shows 25% and the p99 is a disaster, and the two look contradictory until you know throttling exists. Fix: raise/remove the limit (keep `requests`), reduce thread count, or right-size the limit. *How* this throttling is implemented — and a feature that fixes the bursty-but-under-budget case — is Part II, Layer 8.

---

## PART II — The Hard Internals

### Layer 6 — EEVDF: the scheduler that replaced CFS

In kernel 6.6 (late 2023), Linux replaced CFS — its scheduler for 16 years — with **EEVDF (Earliest Eligible Virtual Deadline First)**. If you're running any recent distro, *this* is your scheduler, and it fixes a real weakness in pure vruntime fairness: CFS was fair about *throughput* (everyone gets equal CPU over time) but had no clean notion of *latency* (who needs to run *soon* vs. who just needs to run *eventually*). Two threads could be equally "owed" CPU, but one is a latency-sensitive request handler that needs a quick 1 ms burst and the other is a batch job happy to wait — CFS treated them identically. EEVDF adds the missing dimension. It rests on two concepts:

- **Lag (eligibility):** EEVDF tracks each thread's **lag** — the difference between the CPU time it *should* have received (its fair share) and what it *actually* got. A thread that's been shortchanged has positive lag (it's *owed* time); one that ran more than its share has negative lag. A thread is **eligible** to run only when its lag is non-negative — i.e., it has actually fallen behind its fair share. This prevents a thread that just ran a lot from immediately running again, enforcing fairness more precisely than vruntime alone.
- **Virtual deadline:** among *eligible* threads, each is assigned a **virtual deadline** computed from its requested time slice — a thread asking for a *shorter* slice gets an *earlier* deadline. EEVDF runs the eligible thread with the **earliest deadline.** This is the latency knob: a thread that declares it only needs short bursts (interactive, latency-sensitive) gets earlier deadlines and thus runs *sooner*, while still being bounded by fairness (it must be eligible — owed time — to run at all).

```
EEVDF:  among threads that are ELIGIBLE (lag ≥ 0, i.e. owed their fair share),
        run the one with the EARLIEST virtual DEADLINE (shorter requested slice ⇒ sooner).
        → fairness (eligibility) AND latency (deadline) in one rule.
```

The practical upshot: EEVDF gives the kernel a principled way to favor short-running, latency-sensitive tasks *without* sacrificing fairness, and it exposes a per-task latency hint (`sched_setattr` with a latency-nice value) so you can tell the scheduler "this thread cares about latency." For backend work this is mostly invisible (your interactive threads get slightly better tail latency for free), but it matters that you know the model changed — advice tuned for CFS's pure vruntime (and some old assumptions about how nice values behave) doesn't map perfectly onto EEVDF, and the latency-nice interface is a new, real lever for mixed interactive/batch workloads on one box.

### Layer 7 — PELT: how the scheduler even knows the load

Load balancing (Layer 4) raises a question nobody asks but everybody depends on: **how does the scheduler measure how "loaded" a thread or a core is**, so it can decide what to migrate? A thread isn't just "running" or "not" — it bursts, sleeps, bursts again. Counting runnable threads is too crude (one CPU-bound thread and one that runs 1% of the time are not equal load). The answer is **PELT (Per-Entity Load Tracking)**, and it's a small, elegant piece of signal processing inside the scheduler.

PELT maintains, for every thread (and aggregated per-core, per-cgroup), a **geometrically-decaying running average** of how much it has actually run. Time is divided into ~1 ms windows; recent activity is weighted heavily and older activity decays exponentially (with a tunable half-life, historically ~32 ms). So a thread that's been busy recently has a high tracked load that *decays* as it idles, and ramps up as it runs — a smoothed, recency-weighted estimate of its real CPU demand. This is exactly an EWMA (exponentially weighted moving average), the same tool the statistics chapter uses for latency.

Why it matters beyond curiosity: PELT's load signal drives **load balancing** (migrate to even out *tracked* load, not raw thread counts), and — increasingly important — **CPU frequency scaling and big.LITTLE/heterogeneous scheduling.** On modern CPUs (and especially ARM phones/servers with fast and efficient cores), the kernel uses PELT's per-thread utilization estimate to decide both *which* core to place a thread on (a heavy thread → a big/fast core; a light one → an efficient core) and *what frequency* to run the core at (schedutil governor: ramp frequency to match tracked utilization). So PELT is the bridge between the scheduler and power/performance: a thread's tracked load decides where it runs *and how fast the silicon goes.* A known wrinkle is **ramp-up lag** — because the average decays/grows gradually, a suddenly-bursty thread's load estimate lags reality, so it may briefly run on a slow core at low frequency before the signal catches up, adding latency. Schedulers tune PELT half-lives and add utilization "boosts" (`util_clamp`/`uclamp`) to let you tell the kernel "treat this latency-sensitive thread as needing high utilization immediately" — a real lever for interactive workloads on heterogeneous hardware.

### Layer 8 — CFS bandwidth control: how throttling actually works, and the burst fix

Layer 5 said the cgroup CPU limit "freezes your threads when the quota is spent." *How*, mechanically? Understanding this turns throttling from a black box into something you can reason about and tune. The mechanism is **CFS bandwidth control**, defined by two knobs:

- **`cpu.cfs_period_us`** — the length of the accounting window (default 100,000 µs = 100 ms).
- **`cpu.cfs_quota_us`** — the CPU-time budget per period (e.g., 100,000 µs = "1 CPU"; 200,000 = "2 CPUs"; 50,000 = "0.5 CPU").

Inside the kernel, the cgroup has a **runtime pool** refilled to `quota` at the start of each period by a high-resolution **period timer**. As the cgroup's threads run, they consume from the pool (each CPU pulls a slice of runtime to spend locally, to avoid contending on the global pool every tick). When the pool hits zero, **every runnable thread in the cgroup is dequeued and throttled** — marked un-runnable — until the period timer fires and refills the pool, at which point they're requeued. That's the freeze, precisely: a runtime pool draining to zero mid-period and the threads parked until refill.

This mechanism explains the failure mode's *shape*, not just its existence:

- **Multithreading drains the pool faster.** With N threads running in parallel, you consume N µs of quota per µs of wall time — so a "1 CPU" cgroup with 8 threads burns its 100 ms quota in ~12.5 ms of wall time, then freezes for ~87.5 ms. *The more parallel your service, the more violently it throttles for a given limit* — which is why people are shocked that adding threads made latency worse under a CPU limit.
- **The bursty-but-under-budget trap.** A service that's idle most of the time but does a short CPU burst per request can be *well under* its average limit yet *still throttle* on each burst, because the burst momentarily exceeds the per-period quota. Average utilization 15%, but throttled on every spike.

The fix for that last case is a real, underused feature: **CFS bandwidth burst** (`cpu.cfs_burst_us`, kernel 5.14+). It lets a cgroup *accumulate* unused quota from periods where it ran under budget, up to a burst limit, and *spend* the accumulated surplus during a spike — so a bursty-but-overall-under-budget workload can absorb its spikes without throttling, while still being capped on average over time. This is the principled answer to "my service is under its CPU limit on average but throttles on every request": enable burst so saved-up quota covers the spikes. The broader lesson: CPU *limits* are a hard, period-based bandwidth mechanism with sharp edges (parallelism amplifies it, bursts trigger it), and the right responses are (a) prefer `requests`/shares and avoid hard `limits` where you can, (b) match thread count to the limit, and (c) use burst for spiky workloads. A huge fraction of "Kubernetes made us slow" is this one mechanism, now demystified to the timer level.

### Layer 9 — Priority inversion and priority inheritance (the Mars Pathfinder bug)

Chapter 01 introduced this and promised the full treatment; here it is, because it's the canonical example of scheduling and locking interacting to produce a disaster. **Priority inversion**: a *high*-priority thread is blocked waiting on a lock held by a *low*-priority thread — and a *medium*-priority thread (which doesn't need the lock) preempts the low-priority holder. Now the low thread can't run (medium preempts it), so it can't release the lock, so the high thread stays blocked — *behind the medium-priority thread it should outrank.* Priority has been inverted: medium effectively beats high.

```
  HIGH  ──blocked, waiting for lock L───────────────────────► (starving!)
  MED   ──────────── runs, preempts LOW ───────────────────►  (doesn't even need L)
  LOW   ──holds lock L──[preempted by MED, can't release L]──  (stuck, holding L)
```

The famous case: **NASA's 1997 Mars Pathfinder.** On the Martian surface, the lander began mysteriously resetting itself — a watchdog timer kept detecting that a high-priority bus-management task hadn't run and triggered a full system reset, losing data each time. The cause was textbook priority inversion: a high-priority task blocked on a mutex held by a low-priority meteorological task, while medium-priority tasks ran and starved the low one, so the lock was never released, so the high task missed its deadline, so the watchdog reset the spacecraft. JPL engineers reproduced it on a replica on Earth and fixed it *remotely, on Mars*, by enabling a feature that had been switched off.

That feature is **priority inheritance**: when a high-priority thread blocks on a lock held by a lower-priority thread, the kernel *temporarily boosts the lock holder to the priority of the highest waiter*, so it can't be preempted by medium-priority threads — it runs, releases the lock quickly, and reverts to its original priority. The inversion window collapses. Linux implements this via **PI-futexes** (priority-inheritance futexes), and `pthread_mutex` can be configured with the `PTHREAD_PRIO_INHERIT` protocol. An alternative is the **priority ceiling protocol** (a lock is given a "ceiling" priority and any holder runs at that ceiling). The lesson for backend systems: priority inversion is real wherever you mix thread priorities (or real-time classes) with shared locks, the symptom is "my high-priority work mysteriously starves," and the fix is priority inheritance — but the deeper takeaway is that mixing priorities and locks is subtle enough that, where you can, *not* relying on thread priorities for correctness (and keeping critical sections short) avoids the whole class of bug.

### Layer 10 — Real-time and deadline scheduling: when "soon" must be guaranteed

Fair scheduling (CFS/EEVDF) optimizes *average* behavior; it makes no hard promise about *when* a specific thread runs. Some work needs that promise — audio/video processing (a missed buffer is an audible glitch), industrial control, high-frequency trading, robotics. Linux offers scheduling classes above the normal one for this:

- **`SCHED_FIFO` / `SCHED_RR`** (POSIX real-time): fixed priorities (1–99) that *always* preempt normal tasks. `FIFO` runs a thread until it yields or a higher-priority RT thread preempts it; `RR` adds round-robin time-slicing among equal priorities. Powerful but dangerous — a `SCHED_FIFO` thread in an infinite loop can monopolize a core and lock out everything below it (which is why Linux has an RT throttling safety valve, `sched_rt_runtime_us`, reserving a sliver of CPU for non-RT work).
- **`SCHED_DEADLINE`** — the most sophisticated, based on **EDF (Earliest Deadline First)** plus the **CBS (Constant Bandwidth Server)**. Instead of a priority, you give each thread three numbers: a **runtime** (how much CPU it needs), a **period** (how often), and a **deadline** (by when within each period). The scheduler runs the thread with the *earliest deadline* and, crucially, the CBS *enforces* that each thread can't exceed its declared runtime (so a misbehaving deadline thread can't starve others — its bandwidth is bounded). EDF is provably optimal for single-core deadline scheduling: if any schedule meets all deadlines, EDF does. `SCHED_DEADLINE` even does **admission control** — it refuses to admit a new deadline task if the total requested bandwidth would exceed capacity, *guaranteeing* the existing tasks still meet their deadlines.

```
SCHED_DEADLINE task = (runtime, period, deadline):  "I need `runtime` CPU every `period`,
   finished by `deadline`."  Scheduler runs earliest-deadline-first, CBS caps each task's
   runtime so it can't overrun, admission control refuses tasks that won't fit. → guarantees.
```

Most backend services never touch these — and *shouldn't*, casually, because real-time priorities interact viciously with locks (priority inversion, Layer 9) and can starve the rest of the system. But knowing they exist, and that `SCHED_DEADLINE` gives *mathematically guaranteed* timing via EDF+CBS+admission-control, is what lets you recognize when a latency-critical component (a media transcoder, a packet-processing fast path) genuinely needs them versus when you're better served by isolation (Layer 12) and careful normal-class design.

### Layer 11 — Scheduler domains and the topology-aware balancing hierarchy

Load balancing (Layer 4) isn't a flat "move threads to even out cores" — it's *topology-aware*, because migrating a thread has different costs depending on *how far* it moves, and the kernel models this with **scheduler domains.** Modern CPUs are a hierarchy: hyperthreads (SMT siblings) share a physical core's execution units and L1/L2; cores on a socket share an L3; sockets share only main memory (and form NUMA nodes). Moving a thread *within* a physical core (between SMT siblings) is nearly free (shared caches); moving it to another core on the same socket loses L1/L2 but keeps L3; moving it to another socket loses everything *and* makes memory remote. The cost of migration grows as you climb the hierarchy.

Scheduler domains encode exactly this. The kernel builds a tree of domains — SMT domain (siblings), MC domain (cores sharing L3), NUMA domains (sockets) — and runs load balancing at each level with *different aggressiveness*: balance frequently and eagerly within cheap levels (SMT, shared-L3), reluctantly and rarely across expensive levels (NUMA), because a cross-NUMA migration must be *worth* the remote-memory penalty it incurs. This is also where **wake affinity** lives: when a thread wakes (say, after I/O), the scheduler decides whether to run it on the CPU it last ran on (warm cache) or the CPU of whoever woke it (cache locality for the data being passed) or somewhere idle — a heuristic balancing cache warmth against waiting in a queue. And **NUMA balancing** (`numa_balancing`) is a related, deeper mechanism: the kernel periodically samples which NUMA node a thread's memory actually lives on versus where the thread runs, and can *migrate either the thread or its pages* to bring them onto the same node — automatically fixing the first-touch-on-the-wrong-node problem from chapter 02, at the cost of some background page-fault overhead. The practical relevance: this topology-awareness is *why* the scheduler sometimes "stubbornly" leaves a core idle rather than migrating a thread to it (the migration would cost more than the idle time saved), and why on big NUMA boxes you sometimes pin or use `numactl` to override heuristics that can't know your application's intent.

### Layer 12 — CPU isolation and the tickless kernel: getting the scheduler out of the way

For the most latency-critical work — a thread that must *never* be interrupted, where even a microsecond of jitter is unacceptable — the answer isn't a better scheduling policy, it's **removing the CPU from the scheduler's jurisdiction entirely.** Linux offers a stack of isolation mechanisms that high-performance and real-time systems combine:

- **`isolcpus`** (boot parameter) — removes specified CPUs from the general scheduler's load-balancing domains, so the kernel won't migrate normal threads onto them. You then *manually* pin your critical thread there (`taskset`), and it runs essentially alone — no other tasks scheduled onto its core, no migration disruption.
- **`nohz_full` (the full tickless mode)** — this one is subtle and important. Normally every CPU takes a periodic **timer interrupt** (the "tick," ~100–1000 Hz) for scheduling accounting and housekeeping — even a CPU running a single thread gets interrupted hundreds of times a second, each interrupt evicting cache and adding jitter. `nohz_full` makes a CPU running a *single runnable thread* go **tickless** — it stops the periodic tick entirely, so the thread runs genuinely uninterrupted, no scheduler tick stealing cycles. This is essential for the lowest-jitter workloads (packet processing at line rate, HFT), where the periodic tick was a measurable latency source. (It requires offloading the housekeeping that tick did — RCU callbacks, timekeeping — onto other "housekeeping" CPUs, which is why isolation is configured as a *partition* of the machine: isolated cores for the hot threads, housekeeping cores for the kernel's chores.)
- **IRQ affinity** — steering hardware interrupts *away* from the isolated cores (onto housekeeping cores) so a network or disk interrupt doesn't disrupt the latency-critical thread.

Combine `isolcpus` + `nohz_full` + IRQ affinity + a pinned thread and you get a CPU that runs *your* code and essentially nothing else — no scheduler ticks, no migrations, no other threads, no interrupts — the closest a general-purpose OS gets to bare-metal determinism. This is how DPDK packet processors, low-latency trading systems, and real-time controllers achieve single-digit-microsecond jitter on Linux.

One more, for completeness and because it's a fascinating security/scheduling crossover: **core scheduling.** Hyperthreading (SMT) runs two threads on one physical core sharing execution units and L1 — which the Spectre/L1TF/MDS class of vulnerabilities (chapter 03's mitigations) can exploit to *leak data between the two SMT siblings.* The brute-force defense was disabling hyperthreading entirely (losing ~30% throughput). **Core scheduling** is the surgical alternative: it lets you tag threads with a "cookie" and guarantees the scheduler only ever co-schedules threads with the *same* cookie on sibling hyperthreads — so two mutually-distrusting workloads (e.g., two different tenants' containers) never share a physical core, closing the cross-sibling leak while keeping SMT's throughput for threads that *do* trust each other. It's a striking example of the scheduler being conscripted into *security* — placement as an isolation boundary, not just a performance one.

---

## A Ladder From L1 to Principal

- **L1 / new grad:** The OS shares CPUs among many threads; only as many run as there are cores; the scheduler picks. More threads than cores means time-sharing.
- **L3–L4 / solid engineer:** You understand preemption vs. yielding, fairness via "run who's-had-least" (vruntime/EEVDF), and that nice bends it. You know too many runnable threads means more switching overhead.
- **Senior:** You reason about cache/NUMA affinity and migration cost, when to pin, and — critically — recognize cgroup CPU *throttling* as the cause of high-latency-at-low-utilization, reading `cpu.stat` to confirm. You know EEVDF replaced CFS.
- **Staff:** You understand the bandwidth controller to the period-timer level (parallelism amplifies throttling; burst fixes spiky-under-budget), priority inversion + inheritance, PELT's role in placement/frequency, and tune limits/requests/affinity against real tail latency.
- **Principal:** You design for the scheduler — concurrency matched to cores and quota, CPU isolation (`isolcpus`/`nohz_full`/IRQ affinity) for latency-critical paths, `SCHED_DEADLINE` where timing must be guaranteed, core scheduling for tenant isolation — and predict tail behavior from thread count, topology, and cgroup config before it's an incident.

One idea climbing: *more work than cores means someone always waits; the scheduler decides who and for how long (fairness + latency: EEVDF), where (topology-aware domains, NUMA, PELT-driven placement), and — in the cloud — whether your group may run at all (a hard, period-based bandwidth cap). The deepest control is removing critical work from the scheduler's reach entirely (isolation).*

## Complexity Analysis

| Concept | Cost / behavior | What's happening |
|---|---|---|
| Pick next thread (CFS/EEVDF) | O(log N) | Red-black (or augmented) tree keyed by vruntime/deadline |
| Preemption (timer tick) | a context switch (chapter 01) | Forcibly take the CPU to preserve fairness/responsiveness |
| Voluntary yield (block on I/O) | a context switch | Keeps low vruntime → fast re-schedule |
| More runnable threads than cores | thinner slices, more switches | Slice → `min_granularity`; overhead rises |
| Thread migration | cold cache; cross-NUMA = remote memory | Domains make far migrations rare and reluctant |
| CPU shares (`requests`) | proportional, soft | Relative weight; only bites under contention |
| CPU quota (`limits`) | hard freeze on exhaustion | Runtime pool drains → throttled until period refill |
| Parallelism under a quota | throttles *harder* | N threads drain the pool N× faster |
| `nohz_full` isolated CPU | ~zero scheduler jitter | No periodic tick, no migration, pinned thread runs alone |
| `SCHED_DEADLINE` | guaranteed timing | EDF + CBS bandwidth enforcement + admission control |

The row that causes the most production pain remains the hard quota: a period-based cap that freezes your threads mid-work and hides behind a calm average-utilization graph — and now you know it's a draining runtime pool, amplified by parallelism, fixable with burst.

## War Stories (the shape of the bug in the wild)

- **High p99, 25% CPU, total confusion.** A containerized service had awful tail latency at calm ~25% CPU. `cpu.stat`'s `nr_throttled`/`throttled_time` told the truth: it burned its `limits.cpu` quota in bursts and froze for the rest of each 100 ms window (Layers 5, 8). Removing the hard limit (keeping requests) erased the tail. The graph contradiction *was* the diagnosis.
- **The service that throttled despite being under budget.** A spiky request handler averaged 15% CPU but throttled on nearly every request — each short burst momentarily exceeded the per-period quota (Layer 8). Enabling CFS burst (`cpu.cfs_burst_us`) let saved quota absorb the spikes; throttling vanished without raising the average limit.
- **The spacecraft that kept rebooting.** Mars Pathfinder's high-priority task starved on a mutex held by a low-priority task that medium tasks kept preempting — priority inversion, watchdog reset, repeat (Layer 9). Fixed remotely by enabling priority inheritance. The same bug shape appears anywhere priorities meet locks.
- **The latency-critical thread that kept going cold.** A low-latency path had unpredictable spikes; the load balancer kept migrating its thread across cores and sockets, losing cache and hitting remote NUMA memory (Layers 4, 11). Pinning with `taskset`+`numactl`, then adding `isolcpus`+`nohz_full` for the hot core (Layer 12), flattened the jitter to microseconds.
- **The batch job that starved the API.** A nightly batch job at default priority stole cores during traffic, spiking API latency. Moving it to its own cgroup with low shares (and nice-ing it down) let the fair scheduler yield to interactive work the moment it arrived.

## Key Takeaways

1. **The scheduler decides who runs when there's more work than cores**, balancing throughput (run long, switch rarely) against latency/fairness (switch often) — the dial behind much of your performance behavior.
2. **Fair scheduling runs "whoever's had the least"** (CFS vruntime), kept in an O(log N) tree. **EEVDF replaced CFS in kernel 6.6**, adding *eligibility* (lag — are you owed time?) and *virtual deadlines* (shorter requested slice → run sooner) to get latency-awareness *and* fairness, plus a latency-nice lever.
3. **PELT tracks per-thread load as a decaying average**, driving load balancing *and* CPU frequency/big.LITTLE placement — so a thread's tracked utilization decides where it runs and how fast the silicon goes (with ramp-up lag you can override via `uclamp`).
4. **cgroup CPU throttling is a draining runtime pool refilled by a period timer.** Parallelism drains it *faster* (so more threads throttle *harder* under a given limit), and bursty-but-under-budget workloads throttle on spikes — fixable with **CFS burst**. Prefer `requests`/shares; treat hard `limits` carefully.
5. **Priority inversion** (low-priority lock holder, preempted by medium, starves a high-priority waiter) nearly killed Mars Pathfinder; the fix is **priority inheritance** (boost the holder). Mixing thread priorities with shared locks is subtle — avoid relying on priorities for correctness where you can.
6. **Real-time classes guarantee timing the fair scheduler can't:** `SCHED_FIFO/RR` (fixed priority) and `SCHED_DEADLINE` (EDF + CBS bandwidth enforcement + admission control — mathematically guaranteed deadlines). Powerful, dangerous, rarely needed in backend services.
7. **Load balancing is topology-aware (scheduler domains):** cheap migrations within a core/L3, reluctant ones across NUMA — which is why the scheduler sometimes leaves a core idle, and why NUMA balancing migrates threads or pages to co-locate them.
8. **The deepest latency control is removing CPUs from the scheduler:** `isolcpus` + `nohz_full` (tickless — no periodic timer interrupt) + IRQ affinity + pinning gives near-bare-metal determinism. **Core scheduling** even conscripts placement for *security*, keeping distrusting tenants off shared SMT siblings.

## Related Modules

- `01-processes-and-threads.md` — the context switch the scheduler triggers (TLB flush, cache pollution); priority inversion born from locks + priorities; the futex that PI-futexes extend
- `05-virtual-memory.md` — the TLB flush on cross-process switches; NUMA balancing migrating pages; how memory pressure compounds scheduling latency
- `03-io-and-syscalls.md` — blocking I/O as a voluntary yield; how I/O-bound threads keep low vruntime and reschedule fast
- `02-memory-management.md` — per-CPU run queues mirror per-thread allocator caches; NUMA placement; GC pauses and RCU grace periods as scheduling events
- `../02-data-structures-and-algorithms/03-trees-and-indexing.md` — the red-black tree CFS/EEVDF uses to pick the next thread
- `../09-performance-engineering/` — diagnosing scheduler-induced tail latency, reading `cpu.stat`/PSI, right-sizing limits, pools, and isolation
