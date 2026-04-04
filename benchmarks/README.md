# Benchmarks

Reproducible performance benchmarks for key backend engineering decisions. Each benchmark isolates one variable and measures its impact.

## Available Benchmarks

| # | Benchmark | What It Measures | Key Finding |
|---|-----------|-----------------|-------------|
| 01 | [Thread vs Async vs Event Loop](01-thread-vs-async-vs-event-loop/) | Memory and CPU cost per concurrent task | Async uses 500x less memory than threads |
| 02 | [TCP vs HTTP Overhead](02-tcp-vs-http-overhead/) | Protocol overhead per request | HTTP/2 eliminates per-request connection cost |
| 03 | [JSON vs Protobuf](03-json-vs-protobuf/) | Serialization speed and wire size | Protobuf is 3x smaller and 6x faster |
| 04 | [DB Indexing Impact](04-db-indexing-impact/) | Query time with/without index | Index reduces 1M-row lookup from 800ms to 0.5ms |
| 05 | [N+1 vs Batching](05-n-plus-one-vs-batching/) | Query count impact on latency | JOIN is 100x faster than N+1 at 100 records |
| 06 | [Cache vs No Cache](06-cache-vs-no-cache/) | Cache hit rate vs effective latency | 95% hit rate reduces effective latency 10x |

## Running Benchmarks

Each benchmark directory contains a `README.md` with complete, runnable code. Requirements: Python 3.8+ (standard library only) unless otherwise specified.

## Interpreting Results

All benchmarks report:
- **p50** (median): typical case
- **p99**: tail latency (worst 1%)  
- **Memory usage**: peak heap during benchmark
- **Throughput**: requests per second at steady state

Run each benchmark 3 times and take the median. Results vary by hardware; focus on relative comparisons, not absolute values.
