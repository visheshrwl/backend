# Module 07: Core Backend Engineering

## Purpose

This module covers the highest-impact practical patterns in backend engineering. Every file addresses a problem that affects the majority of production systems and has measurable, significant impact on latency and throughput.

## Contents

| File | Impact | Tags |
|------|--------|------|
| `01-n-plus-one-query-problem.md` | ⚡⚡⚡ Critical | databases, ORM, algorithms |
| `02-connection-pooling.md` | ⚡⚡⚡ Critical | databases, OS, queueing theory |
| `03-caching-strategy.md` | ⚡⚡⚡ Critical | Redis, LRU, performance |
| `04-threading-vs-async-vs-event-loop.md` | ⚡⚡⚡ Critical | OS, concurrency, Python, Go, Node.js |
| `05-rate-limiting.md` | ⚡⚡ High | security, reliability, algorithms |
| `06-api-design.md` | ⚡⚡ High | HTTP, REST, gRPC, versioning |

## Reading Order

Read 01 through 04 first — these are the foundational four. Files 05 and 06 build on them.

## Labs

- `../../labs/lab-01-n-plus-one-profiling/` — Module 01
- `../../labs/lab-02-connection-pool-tuning/` — Module 02

## Benchmarks

- `../../benchmarks/01-thread-vs-async-vs-event-loop/` — Module 04
- `../../benchmarks/05-n-plus-one-vs-batching/` — Module 01
- `../../benchmarks/06-cache-vs-no-cache/` — Module 03

## Cross-Module Dependencies

These modules assume knowledge from:
- `../03-operating-systems/` — processes, threads, file descriptors
- `../04-computer-networks/` — TCP, RTT, connection lifecycle
- `../06-databases/` — query planning, indexes, connection model

If you're new to these topics, read the foundation modules first.
