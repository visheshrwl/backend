# Processes and Threads

## Problem

Every database connection is an OS process. Every thread in your application is a kernel-scheduled entity with memory costs. Understanding the difference between processes, threads, and user-space threads explains the fundamental trade-offs in all concurrency models.

## Why It Matters (Latency, Throughput, Cost)

```
PostgreSQL connection model: 1 client = 1 forked OS process
  fork() cost:         ~2ms (copy page tables for 50MB process)
  Per-connection RAM:  ~5–10MB (shared memory + stack)
  1000 connections:    ~10GB RAM on DB server alone

Thread model (application server):
  Thread creation:     ~50μs
  Per-thread stack:    1–8MB (default)
  Context switch:      1–10μs + TLB flush

Goroutine (Go):
  Creation cost:       ~200ns
  Per-goroutine stack: 2KB (grows on demand)
  "Context switch":    ~100ns (user-space scheduler)
```

## Mental Model

```
Process:
  ┌─────────────────────────────────────────┐
  │  Virtual Address Space (unique per proc)│
  │  ┌─────────┐ ┌─────────┐ ┌──────────┐  │
  │  │  Text   │ │  Data   │ │   Heap   │  │
  │  │ (code)  │ │(globals)│ │(malloc'd)│  │
  │  └─────────┘ └─────────┘ └──────────┘  │
  │  ┌─────────────────────────────────┐    │
  │  │  Thread 1 Stack (8MB)           │    │
  │  └─────────────────────────────────┘    │
  │  File descriptors, signal handlers, ... │
  └─────────────────────────────────────────┘

Thread (within a process):
  Shares: heap, code, file descriptors
  Own:    stack, registers, thread-local storage
```

## OS Scheduler

The Linux CFS (Completely Fair Scheduler) uses a red-black tree of runnable tasks ordered by virtual runtime. At each scheduling point, it picks the task with the smallest virtual runtime.

```
Schedule() invoked when:
  1. Timer interrupt (every ~4ms by default with HZ=250)
  2. Syscall completes and returns to user space
  3. Thread blocks on I/O (yields voluntarily)
  4. Thread exits

Context switch steps:
  1. Save current thread registers to kernel stack (RSP, RIP, general purpose)
  2. Save FPU/SIMD state if dirty (~100 cycles)
  3. Switch page tables if different process → TLB flush
  4. Load new thread registers
  5. Return to user space
```

## Underlying Theory

**fork() and copy-on-write:** When PostgreSQL forks for a new connection, the child starts with a copy of the parent's page tables. Physical pages are shared until either process writes — then the kernel creates a private copy (COW fault, ~1μs per page). PostgreSQL's shared buffers (buffer pool) are mapped shared memory, not COW'd.

**Process isolation:** Separate address spaces mean a bug in one PostgreSQL backend cannot corrupt another backend's memory — important for a multi-tenant DB server. Threads within a process share memory — a corrupt pointer in one thread can crash all threads.

## Complexity Analysis

| Operation | Cost | Notes |
|-----------|------|-------|
| fork() | O(virtual_pages) | Must copy page table |
| pthread_create() | O(1) | ~50μs |
| goroutine creation | O(1) | ~200ns |
| Context switch (thread) | O(1) | 1–10μs + TLB |
| Context switch (goroutine) | O(1) | ~100ns, no TLB |

## Benchmark

```
10,000 concurrent clients:
  Thread-per-client:      10,000 × 8MB stack = 80GB RAM → infeasible
  Process-per-client:     10,000 × 10MB = 100GB RAM → infeasible
  Goroutine-per-client:   10,000 × 2KB = 20MB RAM → trivial
  Async coroutine-per:    10,000 × 2KB state = 20MB RAM → trivial
```

## Key Takeaways

1. PostgreSQL uses one process per connection — this is why connection pools matter.
2. OS threads cost 1–8MB each. Goroutines cost 2KB. This is why Go handles more connections.
3. Context switches cost 1–10μs + TLB flush. At 10K threads switching every 1ms: significant CPU waste.
4. fork() is cheap for small processes, expensive for large ones (PostgreSQL's 50MB is "large").
5. Process isolation is a feature: bugs are contained. Thread-sharing is a feature for communication.

## Related Modules

- `../04-scheduling.md` — Linux CFS scheduler internals
- `../02-memory-management.md` — Virtual memory and COW
- `../../07-core-backend-engineering/04-threading-vs-async-vs-event-loop.md` — Applied to concurrency choices
