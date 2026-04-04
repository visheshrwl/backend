# 04-scheduling

## Problem

Operating system internals directly determine the cost of I/O operations, memory allocations, and scheduling decisions in backend services.

## Why It Matters (Latency, Throughput, Cost)

System calls, page faults, memory allocations — each has a measurable cost that shows up in application latency when done in hot paths.

## Mental Model

The OS provides abstractions (files, sockets, processes) backed by hardware resources. Understanding the cost of each abstraction is the key to avoiding unnecessary overhead.

## Underlying Theory

References: processes-and-threads.md (01), virtual-memory.md (05), and network programming (05 module).

## Complexity Analysis

Operating system operations range from O(1) (hash table lookups in VFS) to O(N) (scanning process lists). Hot-path operations are engineered to be O(1) or O(log N).

## Benchmark

Syscall overhead: ~100–500ns per syscall (on modern Linux with Spectre/Meltdown mitigations).
Page fault: ~1–10μs for minor fault, ~100μs+ for major fault (disk I/O).

## Key Takeaways

1. System calls cross the user/kernel boundary — ~100–500ns each.
2. I/O is expensive; buffering amortizes the cost.
3. Page faults are unavoidable on first access; prefetching reduces their impact.
4. The OS scheduler determines thread fairness and response time.

## Related Modules

- `./01-processes-and-threads.md`
- `../../05-network-programming/02-multiplexing-epoll-kqueue.md`
