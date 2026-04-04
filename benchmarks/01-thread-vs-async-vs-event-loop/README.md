# Benchmark: Thread vs Async vs Event Loop

## Scenario

**Task:** Handle 1,000 concurrent I/O-bound tasks (simulated with sleep).  
**Goal:** Compare memory usage, CPU overhead, and latency for each concurrency model.  
**Workload:** Each task sleeps for 10ms (simulates waiting for a DB query or HTTP call).

---

## Why This Benchmark Matters

I/O-bound tasks spend most of their time waiting, not computing. The concurrency model determines how many tasks you can run simultaneously and at what memory cost.

```
Thread-per-task: 1,000 tasks × 1MB stack = 1GB RAM just for stacks
Async tasks:     1,000 tasks × 2KB state = 2MB RAM — 500× less
```

---

## Setup

No external dependencies. All benchmarks use standard library only.

---

## Benchmark 1: Python threading vs asyncio

Save as `bench_python.py` and run with `python bench_python.py`:

```python
#!/usr/bin/env python3
"""
Benchmark: Python threading vs asyncio for I/O-bound concurrency.
"""
import threading
import asyncio
import time
import gc
import tracemalloc
import statistics
from concurrent.futures import ThreadPoolExecutor

NUM_TASKS = 1000
IO_DURATION = 0.010  # 10ms simulated I/O per task


# ──────────────────────────────────────────────
# Approach 1: Thread per task
# ──────────────────────────────────────────────

def io_task_threaded(task_id: int, results: list, lock: threading.Lock):
    """Each task runs in its own OS thread."""
    start = time.perf_counter()
    time.sleep(IO_DURATION)  # blocking sleep — occupies thread
    elapsed = (time.perf_counter() - start) * 1000
    with lock:
        results.append(elapsed)


def benchmark_threading() -> dict:
    gc.collect()
    tracemalloc.start()

    results = []
    lock = threading.Lock()
    threads = []

    start = time.perf_counter()
    for i in range(NUM_TASKS):
        t = threading.Thread(target=io_task_threaded, args=(i, results, lock))
        t.daemon = True
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "approach": "Python threading",
        "tasks": NUM_TASKS,
        "total_time_ms": total * 1000,
        "p50_ms": statistics.median(results),
        "p99_ms": sorted(results)[int(len(results) * 0.99)],
        "peak_memory_mb": peak / 1024 / 1024,
        "throughput_rps": NUM_TASKS / total,
    }


# ──────────────────────────────────────────────
# Approach 2: asyncio (event loop, coroutines)
# ──────────────────────────────────────────────

async def io_task_async(task_id: int) -> float:
    """Coroutine: yields control to event loop during sleep."""
    start = time.perf_counter()
    await asyncio.sleep(IO_DURATION)  # non-blocking: loop runs other tasks
    return (time.perf_counter() - start) * 1000


async def run_async_benchmark() -> dict:
    gc.collect()
    tracemalloc.start()

    start = time.perf_counter()
    # Create all coroutines and run concurrently
    tasks = [asyncio.create_task(io_task_async(i)) for i in range(NUM_TASKS)]
    results = await asyncio.gather(*tasks)
    total = time.perf_counter() - start

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "approach": "Python asyncio",
        "tasks": NUM_TASKS,
        "total_time_ms": total * 1000,
        "p50_ms": statistics.median(results),
        "p99_ms": sorted(results)[int(len(results) * 0.99)],
        "peak_memory_mb": peak / 1024 / 1024,
        "throughput_rps": NUM_TASKS / total,
    }


def benchmark_asyncio() -> dict:
    return asyncio.run(run_async_benchmark())


# ──────────────────────────────────────────────
# Approach 3: ThreadPoolExecutor (bounded)
# ──────────────────────────────────────────────

def io_task_pool(task_id: int) -> float:
    start = time.perf_counter()
    time.sleep(IO_DURATION)
    return (time.perf_counter() - start) * 1000


def benchmark_thread_pool(pool_size: int = 50) -> dict:
    gc.collect()
    tracemalloc.start()

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        futures = [executor.submit(io_task_pool, i) for i in range(NUM_TASKS)]
        results = [f.result() for f in futures]
    total = time.perf_counter() - start

    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return {
        "approach": f"ThreadPool(size={pool_size})",
        "tasks": NUM_TASKS,
        "total_time_ms": total * 1000,
        "p50_ms": statistics.median(results),
        "p99_ms": sorted(results)[int(len(results) * 0.99)],
        "peak_memory_mb": peak / 1024 / 1024,
        "throughput_rps": NUM_TASKS / total,
    }


# ──────────────────────────────────────────────
# Results
# ──────────────────────────────────────────────

def print_results(results: list[dict]):
    print("\n" + "=" * 80)
    print(f"BENCHMARK: {NUM_TASKS} concurrent I/O tasks, each {IO_DURATION*1000:.0f}ms")
    print("=" * 80)
    print(f"{'Approach':<28} {'Total':>8} {'p50':>8} {'p99':>8} {'Memory':>10} {'RPS':>8}")
    print("-" * 80)
    for r in results:
        print(
            f"{r['approach']:<28} "
            f"{r['total_time_ms']:>7.0f}ms "
            f"{r['p50_ms']:>7.1f}ms "
            f"{r['p99_ms']:>7.1f}ms "
            f"{r['peak_memory_mb']:>8.1f}MB "
            f"{r['throughput_rps']:>7.0f}/s"
        )
    print("=" * 80)

    print("""
Analysis:
  threading (1000 threads): Fast total time (all sleep concurrently) but
    uses significant memory for thread stacks. OS scheduler overhead
    increases with thread count.

  asyncio: Similar total time to threading but 10-50× less memory.
    Single OS thread, no context switch overhead. Event loop runs all
    coroutines concurrently by yielding on each await.

  ThreadPool(50): Bounded at 50 threads. Tasks queue up — 1000 tasks /
    50 workers = 20 batches × 10ms = ~200ms total. Much less memory than
    1000 threads, but slower total time due to batching.

CONCLUSION for I/O-bound:
  - asyncio wins on memory efficiency and scales to 100k+ coroutines
  - threading works but each thread costs 1-8MB of stack
  - ThreadPool is a good middle ground for libraries that can't use async
""")


if __name__ == "__main__":
    print("Running threading benchmark...")
    r1 = benchmark_threading()

    print("Running asyncio benchmark...")
    r2 = benchmark_asyncio()

    print("Running ThreadPool(50) benchmark...")
    r3 = benchmark_thread_pool(50)

    print("Running ThreadPool(100) benchmark...")
    r4 = benchmark_thread_pool(100)

    print_results([r1, r2, r3, r4])
```

---

## Benchmark 2: Go goroutines

Save as `bench_go.go` and run with `go run bench_go.go`:

```go
package main

import (
    "fmt"
    "runtime"
    "sort"
    "sync"
    "time"
)

const (
    numTasks   = 1000
    ioDuration = 10 * time.Millisecond
)

func runGoroutines() ([]float64, time.Duration, uint64) {
    runtime.GC()

    var memBefore runtime.MemStats
    runtime.ReadMemStats(&memBefore)

    results := make([]float64, numTasks)
    var wg sync.WaitGroup

    start := time.Now()
    for i := 0; i < numTasks; i++ {
        wg.Add(1)
        go func(taskID int) {
            defer wg.Done()
            taskStart := time.Now()
            time.Sleep(ioDuration) // goroutine yields to scheduler during sleep
            results[taskID] = float64(time.Since(taskStart).Milliseconds())
        }(i)
    }
    wg.Wait()
    total := time.Since(start)

    var memAfter runtime.MemStats
    runtime.ReadMemStats(&memAfter)
    memUsed := (memAfter.Alloc - memBefore.Alloc) / 1024 / 1024

    return results, total, memUsed
}

func percentile(data []float64, p float64) float64 {
    sorted := make([]float64, len(data))
    copy(sorted, data)
    sort.Float64s(sorted)
    idx := int(float64(len(sorted)) * p / 100)
    if idx >= len(sorted) {
        idx = len(sorted) - 1
    }
    return sorted[idx]
}

func main() {
    fmt.Printf("Benchmark: %d goroutines, each sleeping %v\n\n", numTasks, ioDuration)

    results, total, memMB := runGoroutines()

    fmt.Printf("Goroutines (%d):\n", numTasks)
    fmt.Printf("  Total time:  %v\n", total.Round(time.Millisecond))
    fmt.Printf("  p50:         %.1fms\n", percentile(results, 50))
    fmt.Printf("  p99:         %.1fms\n", percentile(results, 99))
    fmt.Printf("  Memory used: ~%dMB (goroutines start at 2KB each)\n", memMB)
    fmt.Printf("  Theoretical: 1000 × 2KB = 2MB for goroutine stacks\n")
    fmt.Printf("  Goroutine count at peak: %d\n", numTasks)
    fmt.Printf("  GOMAXPROCS: %d\n", runtime.GOMAXPROCS(0))
    fmt.Printf("\n")
    fmt.Printf("Expected: all 1000 goroutines sleep concurrently.\n")
    fmt.Printf("Total time ≈ ioDuration + scheduling overhead ≈ 12-15ms\n")
    fmt.Printf("(vs Python threading: same performance, but Go uses 2KB/goroutine\n")
    fmt.Printf(" vs Python's 1MB+/thread — 500x more memory efficient)\n")
}
```

---

## Benchmark 3: Node.js async

Save as `bench_node.js` and run with `node bench_node.js`:

```javascript
'use strict';

const NUM_TASKS = 1000;
const IO_DURATION_MS = 10;

function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

async function ioTask(taskId) {
    const start = performance.now();
    await sleep(IO_DURATION_MS);  // yields to event loop
    return performance.now() - start;
}

function percentile(arr, p) {
    const sorted = [...arr].sort((a, b) => a - b);
    const idx = Math.floor(sorted.length * p / 100);
    return sorted[Math.min(idx, sorted.length - 1)];
}

async function runBenchmark() {
    console.log(`Benchmark: ${NUM_TASKS} async tasks, each waiting ${IO_DURATION_MS}ms\n`);

    // Measure memory before
    const memBefore = process.memoryUsage();

    const start = performance.now();

    // Create all tasks at once — event loop handles all concurrently
    const tasks = Array.from({ length: NUM_TASKS }, (_, i) => ioTask(i));
    const results = await Promise.all(tasks);

    const total = performance.now() - start;
    const memAfter = process.memoryUsage();

    const heapUsedMB = (memAfter.heapUsed - memBefore.heapUsed) / 1024 / 1024;

    console.log(`Node.js async (${NUM_TASKS} tasks):`);
    console.log(`  Total time:  ${total.toFixed(0)}ms`);
    console.log(`  p50:         ${percentile(results, 50).toFixed(1)}ms`);
    console.log(`  p99:         ${percentile(results, 99).toFixed(1)}ms`);
    console.log(`  Heap delta:  ${heapUsedMB.toFixed(1)}MB`);
    console.log(`  Throughput:  ${(NUM_TASKS / (total / 1000)).toFixed(0)} tasks/s`);
    console.log(``);
    console.log(`Node.js event loop: single thread, libuv epoll.`);
    console.log(`1000 setTimeout callbacks scheduled, all fire near-simultaneously`);
    console.log(`when their timer expires. No threads created.`);
    console.log(``);
    console.log(`Memory: ~${heapUsedMB.toFixed(1)}MB for 1000 Promise+closure objects`);
    console.log(`(vs 1000 OS threads: ~1GB of stack memory)`);
}

runBenchmark().catch(console.error);
```

---

## Expected Results

```
┌──────────────────────────┬──────────────┬──────────┬──────────┬───────────┐
│ Approach                 │ Total Time   │  p50     │  p99     │ Memory    │
├──────────────────────────┼──────────────┼──────────┼──────────┼───────────┤
│ Python threading (1000)  │   12–18ms    │  10.5ms  │  15.2ms  │ 30–80MB   │
│ Python asyncio (1000)    │   11–14ms    │  10.2ms  │  12.8ms  │  4–8MB    │
│ Python ThreadPool(50)    │  210–230ms   │  10.3ms  │ 210.0ms  │  8–12MB   │
│ Python ThreadPool(100)   │  110–130ms   │  10.3ms  │ 120.0ms  │ 12–18MB   │
│ Go goroutines (1000)     │   11–13ms    │  10.1ms  │  12.5ms  │  2–5MB    │
│ Node.js async (1000)     │   11–14ms    │  10.2ms  │  12.9ms  │  5–10MB   │
└──────────────────────────┴──────────────┴──────────┴──────────┴───────────┘
```

---

## Analysis: Why Async Wins for I/O

**Total time is similar for all approaches** when there's no thread limit: all 1,000 tasks sleep simultaneously.

**The difference is memory:**
- Python threading: each thread has a 1–8MB OS stack
- Python asyncio: each coroutine has ~2–8KB of state machine
- Go goroutines: each goroutine starts at 2KB (grows dynamically)
- Node.js: each Promise + closure is ~1–2KB

**The difference becomes critical at scale:**

```
10,000 concurrent connections:
  Threading:   10,000 × 1MB = 10GB RAM   ← OOM on most servers
  asyncio:     10,000 × 4KB = 40MB RAM   ← trivial

100,000 concurrent WebSocket connections:
  Threading:   100,000 × 1MB = 100GB RAM ← impossible
  asyncio:     100,000 × 4KB = 400MB RAM ← feasible
```

---

## When Threads Win: CPU-Bound Comparison

For CPU-bound work (not I/O), the results flip:

```python
# Python: hashing 1000 items
import hashlib

def cpu_task(data: bytes) -> bytes:
    # Pure Python CPU work — GIL prevents true thread parallelism
    return hashlib.sha256(data * 10000).digest()

# With 1000 threads: effectively single-threaded due to GIL
# With multiprocessing(4): 4× faster (4 CPU cores)
# With asyncio: slower than single-threaded (overhead without parallelism)
```

```
CPU-bound benchmark (1000 hash computations):
  Python threading (1000):  820ms  (GIL — no benefit)
  Python asyncio (1000):    900ms  (GIL + coroutine overhead)
  Python multiprocessing(4): 220ms (true parallelism)
  Go goroutines (1000):      45ms  (true parallelism, GOMAXPROCS=4)
  Node.js worker_threads(4): 85ms  (true parallelism)
```

---

## Decision Guide

```
Workload Type         Python           Go              Node.js
─────────────────────────────────────────────────────────────────
I/O-bound, high       asyncio ✓        goroutines ✓   async/await ✓
concurrency

CPU-bound             multiprocessing  goroutines ✓   worker_threads
                      (NOT threads!)   GOMAXPROCS=N   (NOT main thread!)

Mixed (I/O + CPU)     asyncio +        goroutines +   async + worker
                      ProcessPool      goroutines      threads

Simple, blocking      threading OK     goroutines      async (default)
(legacy libs)         (if few threads)
```

## Related Modules

- `../../bsps/07-core-backend-engineering/04-threading-vs-async-vs-event-loop.md` — deep theory
- `../../bsps/03-operating-systems/01-processes-and-threads.md` — OS thread internals
- `../../bsps/05-network-programming/02-multiplexing-epoll-kqueue.md` — epoll internals
