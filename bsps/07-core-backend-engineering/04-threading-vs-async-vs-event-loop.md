# Threading vs Async vs Event Loop

## Problem

When your server needs to handle 10,000 concurrent connections, you must choose a concurrency model. The wrong choice wastes CPU, memory, and latency budget. The right choice depends on your workload type (CPU-bound vs I/O-bound) and language runtime.

```
Common question: "Should I use threads, async/await, or an event loop?"
Answer:          "It depends on whether you're CPU-bound or I/O-bound,
                  and what your language's concurrency primitives actually do."
```

The three models are not competing alternatives — they are complementary tools with different cost structures.

---

## Why It Matters (Latency, Throughput, Cost)

**The C10K problem (1999, still relevant):**

In 1999, Dan Kegel identified that serving 10,000 concurrent clients with thread-per-connection required 10,000 OS threads. At 1–8MB per thread stack: 10GB–80GB RAM just for stacks. Context switching 10,000 threads: thousands of microseconds per operation.

Modern event-loop and async solutions serve 100,000+ concurrent connections on a single core with megabytes of RAM.

**Cost comparison at 10,000 connections:**

```
Thread-per-connection (10,000 threads):
  Stack memory: 10,000 × 1MB = 10GB
  Context switches: ~50,000/second × 10μs = 500ms CPU/second (50% of a core!)
  
Async/coroutine (10,000 tasks):
  Stack equivalent: 10,000 × 2KB = 20MB (500× less)
  Context switches: ~50,000/second × 0.1μs = 5ms CPU/second (0.5% of a core)
  
Savings: 500× less memory, 100× less CPU overhead for I/O-bound workloads.
```

---

## Mental Model

```
Threading (OS kernel manages):
  ┌─────────────────────────────────────┐
  │  OS Scheduler                       │
  │  ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐  │
  │  │Thr1 │ │Thr2 │ │Thr3 │ │Thr4 │  │
  │  │(run)│ │(wait│ │(wait│ │(run)│  │
  │  │     │ │ I/O)│ │lock)│ │     │  │
  │  └─────┘ └─────┘ └─────┘ └─────┘  │
  │     CPU1           CPU2             │
  └─────────────────────────────────────┘
  Preemptive: OS can interrupt any thread at any time

Async/Event Loop (application manages):
  ┌─────────────────────────────────────┐
  │  Event Loop (single thread)         │
  │                                     │
  │  event_queue: [sock1_readable,      │
  │                sock3_writable,      │
  │                timer_expired]       │
  │                                     │
  │  while True:                        │
  │    event = poll_io()    # epoll()   │
  │    callback = handlers[event]       │
  │    callback()           # runs fast │
  └─────────────────────────────────────┘
  Cooperative: code explicitly yields control
```

---

## Underlying Theory (OS / CN / DSA / Math Linkage)

### OS Threads: Full Stack Machine

An OS thread is a complete CPU execution context:
- **Stack:** 1–8MB per thread (default: Linux 8MB, can be reduced with `ulimit -s` or `pthread_attr_setstacksize`)
- **Registers:** RSP (stack pointer), RIP (instruction pointer), 16 general-purpose registers
- **TLB entries:** Thread's code references virtual addresses; TLB caches virtual→physical mappings
- **Kernel resources:** Thread Control Block (TCB) in kernel space

**Context switch cost:**
```
Context switch steps:
  1. Save current thread's registers to TCB              (~50 ns)
  2. Load next thread's registers from TCB               (~50 ns)
  3. TLB flush (if different process, or sometimes same) (~200 ns)
  4. Cache warmup for new thread's working set           (~500 ns - 2μs)
  ─────────────────────────────────────────────────────────────────
  Total: 1-10μs per context switch

At 10,000 threads switching at 1ms intervals:
  Switches/second = 10,000 × 1,000 = 10,000,000
  CPU time = 10,000,000 × 10μs = 100 seconds of CPU/second
  (impossible — this is why thread-per-connection doesn't scale)
```

### Green Threads / Goroutines: User-Space Scheduler

Go's goroutines are **multiplexed M goroutines onto N OS threads** (M:N threading):

```
Go runtime scheduler:
  ┌─────────────────────────────────────────────┐
  │  M1 (OS thread)    M2 (OS thread)           │
  │  ┌─────────────┐  ┌─────────────┐          │
  │  │  P1 (proc)  │  │  P2 (proc)  │          │
  │  │  run queue: │  │  run queue: │          │
  │  │  [G3,G5,G7] │  │  [G2,G4,G6] │          │
  │  │  currently: │  │  currently: │          │
  │  │     G1      │  │     G8      │          │
  │  └─────────────┘  └─────────────┘          │
  │                                             │
  │  Global run queue: [G9, G10, G11, ...]      │
  │                                             │
  │  Work stealing: P1 steals from P2 when idle │
  └─────────────────────────────────────────────┘
```

- **Goroutine stack:** starts at 2KB, grows on demand up to 1GB (stack copying/segmented stacks)
- **Goroutine switch cost:** ~100–200ns (no TLB flush, no kernel involvement)
- **GOMAXPROCS:** number of OS threads = number of CPU cores by default

### Async/Await: Cooperative Coroutines

Async functions are **state machines** compiled by the language runtime. `await` is a yield point:

```python
# This Python function:
async def fetch_user(user_id):
    conn = await db.acquire()        # yield point 1
    row = await conn.fetchrow(...)   # yield point 2
    await conn.release()             # yield point 3
    return row

# Is approximately equivalent to this state machine:
class FetchUserStateMachine:
    def __init__(self, user_id):
        self.user_id = user_id
        self.state = 0

    def send(self, value):
        if self.state == 0:
            self.conn_future = db.acquire()
            self.state = 1
            return self.conn_future   # suspend, return future to event loop
        elif self.state == 1:
            self.conn = value          # resumed with connection
            self.row_future = self.conn.fetchrow(...)
            self.state = 2
            return self.row_future    # suspend again
        elif self.state == 2:
            self.row = value
            # ... continue
```

No new OS thread is created. The coroutine is resumed by the event loop when the awaited I/O completes.

### Event Loop: epoll/kqueue Under the Hood

The event loop uses OS-level I/O multiplexing to monitor thousands of sockets with a single syscall:

```
┌─────────────────────────────────────────────────────────────────┐
│  epoll (Linux) / kqueue (macOS/BSD)                             │
│                                                                 │
│  epoll_create():  create epoll interest list                    │
│  epoll_ctl(fd, EPOLL_CTL_ADD, events):  register socket        │
│  epoll_wait(timeout):  block until any registered fd is ready   │
│      → returns list of ready file descriptors in O(ready_fds)  │
│        NOT O(total_fds) — this is the key advantage over select │
└─────────────────────────────────────────────────────────────────┘

Node.js libuv event loop phases:
  ┌──────────────────────────────────────────┐
  │    ┌─────────────────────────────────┐   │
  │    │         timers                  │   │  setTimeout, setInterval callbacks
  │    └─────────────────┬───────────────┘   │
  │                      │                   │
  │    ┌─────────────────▼───────────────┐   │
  │    │     pending callbacks           │   │  I/O callbacks deferred to next loop
  │    └─────────────────┬───────────────┘   │
  │                      │                   │
  │    ┌─────────────────▼───────────────┐   │
  │    │      poll (epoll_wait)          │   │  Wait for I/O events, execute callbacks
  │    └─────────────────┬───────────────┘   │
  │                      │                   │
  │    ┌─────────────────▼───────────────┐   │
  │    │           check                 │   │  setImmediate callbacks
  │    └─────────────────┬───────────────┘   │
  │                      │                   │
  │    ┌─────────────────▼───────────────┐   │
  │    │    close callbacks              │   │  socket.on('close', ...)
  │    └─────────────────────────────────┘   │
  └──────────────────────────────────────────┘

  Between each phase: process ALL microtasks (Promise.then, queueMicrotask)
```

### The GIL (CPython Global Interpreter Lock)

CPython's GIL is a mutex that allows only one thread to execute Python bytecode at a time:

```
Thread 1: ──[acquire GIL]──[execute]──[release GIL]──[wait]──────────────
Thread 2: ──[wait]─────────────────────────────────[acquire GIL][execute]─
Thread 3: ──[wait]──────────────────────────────────────────────[wait]────

Result: Python threads do NOT parallelize CPU-bound code.
        Two Python threads on a quad-core CPU = 1 core effectively used.

Exception: I/O-bound work DOES release the GIL:
  Thread calls recv() → releases GIL → OS performs I/O → thread reacquires GIL
  Multiple threads CAN overlap on I/O, just not CPU.
```

**Escape hatches:**
- `multiprocessing` module: separate processes, each with their own GIL
- C extensions (NumPy, etc.) can release GIL during computation
- PyPy, GraalPy: alternative runtimes without GIL (PEP 703 for no-GIL CPython in progress)

---

## When Threads Win

1. **CPU-bound work:** image processing, compression, cryptography. Use `multiprocessing` in Python (bypasses GIL). Use OS threads in Go and Node.js worker threads.
2. **Blocking syscalls you can't avoid:** some legacy libraries only offer blocking APIs. Wrap in a thread pool.
3. **Simple synchronization requirements:** a fixed pool of 4 workers processing a queue is simpler with threads than async.
4. **Shared mutable state:** locks are simpler to reason about than async coordination (though both are hard to get right).

## When Async Wins

1. **I/O-bound, high concurrency:** HTTP servers, microservices, API gateways.
2. **Many idle connections:** WebSocket servers, long-polling, chat applications.
3. **Low memory budget:** embedded systems, resource-constrained environments.
4. **Low tail latency:** no context switch overhead means more predictable p99.

---

## Naive Approach — Thread per Connection

```python
import threading
import socket
import time

def handle_client(conn, addr):
    """Each client gets its own OS thread."""
    data = conn.recv(1024)
    time.sleep(0.01)           # simulate I/O work
    conn.send(b"HTTP/1.1 200 OK\r\n\r\nHello")
    conn.close()

def run_threaded_server(port=8080):
    sock = socket.socket()
    sock.bind(('', port))
    sock.listen(128)
    while True:
        conn, addr = sock.accept()
        # NEW THREAD PER CONNECTION — does not scale
        t = threading.Thread(target=handle_client, args=(conn, addr))
        t.daemon = True
        t.start()
```

At 10,000 concurrent clients: 10,000 threads × 8MB stack = 80GB RAM. Crash.

---

## Optimized Approach

### Python — asyncio (event loop, single thread)

```python
import asyncio
import time

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Single thread, async I/O — handles thousands of connections."""
    data = await reader.read(1024)       # yields to event loop (non-blocking)
    await asyncio.sleep(0.01)            # yields to event loop (not thread sleep!)
    writer.write(b"HTTP/1.1 200 OK\r\n\r\nHello")
    await writer.drain()
    writer.close()

async def main():
    server = await asyncio.start_server(handle_client, '0.0.0.0', 8080)
    async with server:
        await server.serve_forever()

# 10,000 concurrent connections:
#   Memory: 10,000 × ~2KB coroutine state = 20MB
#   CPU: near zero (blocked in epoll_wait 99% of the time)
asyncio.run(main())
```

**asyncio with thread pool for CPU work:**

```python
import asyncio
from concurrent.futures import ProcessPoolExecutor

executor = ProcessPoolExecutor(max_workers=4)  # one per CPU core

def cpu_intensive(data: bytes) -> bytes:
    """CPU-bound work: runs in a process, not blocking the event loop."""
    import hashlib
    return hashlib.sha256(data).digest()

async def handle_request(data: bytes) -> bytes:
    loop = asyncio.get_event_loop()
    # run_in_executor: submits to process pool, awaits result without blocking loop
    result = await loop.run_in_executor(executor, cpu_intensive, data)
    return result
```

### Go — Goroutines + Channels

```go
package main

import (
    "fmt"
    "net"
    "sync"
    "time"
)

func handleConn(conn net.Conn, wg *sync.WaitGroup) {
    defer wg.Done()
    defer conn.Close()
    // Goroutine: 2KB stack, cheap to create
    buf := make([]byte, 1024)
    conn.Read(buf)
    time.Sleep(10 * time.Millisecond) // simulate I/O
    conn.Write([]byte("HTTP/1.1 200 OK\r\n\r\nHello"))
}

func main() {
    ln, _ := net.Listen("tcp", ":8080")
    var wg sync.WaitGroup

    for {
        conn, _ := ln.Accept()
        wg.Add(1)
        go handleConn(conn, &wg) // one goroutine per connection — cheap!
    }
    // 10,000 connections: 10,000 × 2KB = 20MB
    // Go's runtime schedules them on GOMAXPROCS OS threads
}
```

**Go is unique:** goroutines give you the "one goroutine per connection" simplicity of thread-per-connection, with async-level memory efficiency. The runtime does the multiplexing.

**Worker pool with goroutines:**

```go
func main() {
    jobs := make(chan Job, 1000)
    results := make(chan Result, 1000)

    // Start fixed worker pool (bounded goroutines)
    for w := 0; w < 10; w++ {
        go func() {
            for job := range jobs {
                results <- process(job)
            }
        }()
    }

    // Submit jobs
    for _, job := range allJobs {
        jobs <- job
    }
    close(jobs)
}
```

### Node.js — async/await + EventEmitter

```javascript
const net = require('net');

// Single-threaded event loop handles all connections
const server = net.createServer((socket) => {
    socket.on('data', async (data) => {
        // Non-blocking: returns to event loop while waiting
        await new Promise(resolve => setTimeout(resolve, 10)); // simulate I/O
        socket.write('HTTP/1.1 200 OK\r\n\r\nHello');
        socket.end();
    });
});

server.listen(8080);

// Worker threads for CPU-bound work (Node.js 10.5+)
const { Worker, isMainThread, parentPort, workerData } = require('worker_threads');

if (isMainThread) {
    // Main event loop thread
    async function runCpuWork(data) {
        return new Promise((resolve, reject) => {
            const worker = new Worker(__filename, { workerData: data });
            worker.on('message', resolve);
            worker.on('error', reject);
        });
    }
} else {
    // Worker thread — can do blocking CPU work safely
    const result = heavyComputation(workerData);
    parentPort.postMessage(result);
}
```

**Never block the Node.js event loop:**

```javascript
// BAD: synchronous computation blocks all connections
app.get('/hash', (req, res) => {
    const hash = require('crypto')
        .createHash('sha256')
        .update(req.body.data.repeat(100000)) // blocks for 500ms!
        .digest('hex');
    res.json({ hash });
});

// GOOD: offload to worker thread
const { Worker } = require('worker_threads');
app.get('/hash', async (req, res) => {
    const hash = await runInWorker(req.body.data); // event loop free
    res.json({ hash });
});
```

---

## Complexity Analysis

| Model | Memory per connection | Context switch | Max concurrent (practical) |
|-------|----------------------|----------------|---------------------------|
| OS Thread | 1–8MB stack | 1–10μs + TLB flush | ~10,000 |
| Goroutine | 2KB–grow | ~100–200ns | ~1,000,000 |
| async/await | ~2KB coroutine state | ~100ns | ~100,000 |
| Event loop (callbacks) | ~1KB | ~50ns | ~100,000 |

**Time complexity** for N concurrent I/O tasks:
- Thread-per-connection: O(N) OS threads, O(N × switch_cost) scheduling overhead
- Goroutine: O(N/GOMAXPROCS) switches per second — scales with CPU count
- Async: O(1) OS threads (approximately), O(N) task state, O(ready_events) per loop iteration

---

## Benchmark (p50, p99, CPU, Memory)

Setup: 10,000 concurrent clients, each holding an HTTP connection with 10ms simulated I/O latency.

```
┌─────────────────────────┬────────┬────────┬──────────┬───────────┐
│ Model                   │  p50   │  p99   │ Memory   │ CPU (1req)│
├─────────────────────────┼────────┼────────┼──────────┼───────────┤
│ Python threads (GIL)    │  14ms  │  95ms  │ 10GB+    │ 2ms       │
│ Python asyncio          │  11ms  │  18ms  │ 25MB     │ 0.3ms     │
│ Go goroutines           │  11ms  │  15ms  │ 22MB     │ 0.2ms     │
│ Node.js async           │  11ms  │  17ms  │ 80MB     │ 0.5ms     │
└─────────────────────────┴────────┴────────┴──────────┴───────────┘

For CPU-bound work (hash computation) on 10,000 requests:
┌─────────────────────────┬────────┬────────┬──────────┬───────────┐
│ Model                   │  p50   │  p99   │ Memory   │ CPU cores │
├─────────────────────────┼────────┼────────┼──────────┼───────────┤
│ Python asyncio (single) │ 120ms  │ 200ms  │ 25MB     │ 1 (GIL)   │
│ Python multiprocessing  │  35ms  │  55ms  │ 400MB    │ 4         │
│ Go goroutines           │  28ms  │  45ms  │ 22MB     │ 4         │
│ Node.js worker_threads  │  32ms  │  50ms  │ 200MB    │ 4         │
└─────────────────────────┴────────┴────────┴──────────┴───────────┘
```

---

## Observability

### Thread pool metrics

```python
from concurrent.futures import ThreadPoolExecutor
from prometheus_client import Gauge, Counter

thread_pool_active = Gauge('thread_pool_active_threads', 'Active threads')
thread_pool_queue = Gauge('thread_pool_queued_tasks', 'Queued tasks')

class InstrumentedExecutor(ThreadPoolExecutor):
    def submit(self, fn, *args, **kwargs):
        thread_pool_queue.inc()
        future = super().submit(fn, *args, **kwargs)
        future.add_done_callback(lambda _: thread_pool_queue.dec())
        return future
```

### Event loop lag (Node.js)

```javascript
// Event loop lag: time between when a callback was scheduled and when it ran
let lastTick = Date.now();
setInterval(() => {
    const now = Date.now();
    const lag = now - lastTick - 100; // expected 100ms interval
    prometheus.gauge('event_loop_lag_ms').set(lag);
    lastTick = now;
}, 100);

// ALERT: event loop lag > 50ms means the loop is being blocked
```

### Goroutine leak detection (Go)

```go
import (
    "net/http"
    _ "net/http/pprof"  // registers /debug/pprof/goroutine endpoint
    "runtime"
)

// Expose goroutine count as metric
func goroutineCount() int {
    return runtime.NumGoroutine()
}

// Alert: goroutine count grows without bound → leak
// Normal: stable count proportional to active connections
```

---

## Failure Modes

**1. Blocking the event loop (Node.js / Python asyncio):**

```javascript
// BAD: This blocks Node.js for 5 seconds — ALL other requests stall
app.get('/slow', (req, res) => {
    const start = Date.now();
    while (Date.now() - start < 5000) {}  // CPU spin — NEVER do this
    res.send('done');
});

// How to detect: event loop lag metric spikes
// How to fix: Worker threads, process.nextTick decomposition, or restructure algorithm
```

**2. Goroutine leak (Go):**

```go
// BAD: goroutine blocked forever on channel nobody will send to
func leaky() {
    ch := make(chan int)
    go func() {
        val := <-ch  // blocks forever if nobody sends
        fmt.Println(val)
    }()
    // Function returns, ch goes out of scope, goroutine is stuck
}

// GOOD: use context for cancellation
func safe(ctx context.Context) {
    ch := make(chan int)
    go func() {
        select {
        case val := <-ch:
            fmt.Println(val)
        case <-ctx.Done():
            return  // goroutine exits cleanly
        }
    }()
}
```

**3. Thread starvation:**

When all threads in a pool are waiting for I/O, and no threads are available for new requests:

```python
# Starvation scenario:
executor = ThreadPoolExecutor(max_workers=10)

async def handler():
    # Submits synchronous DB call to thread pool
    result = await loop.run_in_executor(executor, blocking_db_call)
    # If blocking_db_call takes 30s and there are 10 concurrent requests:
    # all 10 executor threads are occupied → 11th request waits forever
```

Fix: Separate thread pools for I/O-bound and CPU-bound work. Set timeouts.

**4. Priority inversion:**

A high-priority goroutine waiting on a mutex held by a low-priority goroutine. The low-priority goroutine can't run because the high-priority one is consuming CPU time.

Go 1.14+ uses asynchronous preemption to avoid full priority inversion.

**5. Python GIL + threads CPU-bound misuse:**

```python
# TRAP: looks parallel, isn't
import threading

def compute(n):
    for i in range(n):
        i ** 2  # pure Python — GIL prevents true parallelism

threads = [threading.Thread(target=compute, args=(10_000_000,)) for _ in range(4)]
for t in threads: t.start()
for t in threads: t.join()

# This takes ~4× LONGER than single-threaded due to GIL contention!
# Use multiprocessing.Pool instead.
```

---

## When NOT to Use Async

1. **CPU-bound Python code:** async doesn't help (still GIL-bound). Use `multiprocessing`.
2. **Simple scripts with 1–5 operations:** async adds complexity without benefit.
3. **Blocking-only libraries:** some Python libraries (e.g., certain DB drivers, boto3) are synchronous only. Running them in `asyncio` without `run_in_executor` will block the event loop.
4. **Debugging:** async stack traces are harder to read. Callbacks and coroutines fragment the call stack.
5. **Teams unfamiliar with async:** async bugs (forgotten `await`, accidental blocking) are subtle and production-unsafe without team experience.

---

## Decision Matrix

```
                    I/O-bound         CPU-bound
                 ┌──────────────┬──────────────────┐
Python           │ asyncio ✓    │ multiprocessing ✓│
                 │ (threads OK  │ (NOT threads —   │
                 │  for legacy) │  GIL kills you)  │
                 ├──────────────┼──────────────────┤
Go               │ goroutines ✓ │ goroutines ✓     │
                 │              │ (GOMAXPROCS=ncpu) │
                 ├──────────────┼──────────────────┤
Node.js          │ async/await ✓│ worker_threads ✓ │
                 │              │ (NOT main thread) │
                 └──────────────┴──────────────────┘
```

---

## Lab

See `../../benchmarks/01-thread-vs-async-vs-event-loop/README.md` for a complete benchmark comparing:
- Python threading vs asyncio for 1,000 concurrent I/O tasks
- Go goroutines for the same workload
- Node.js async for the same workload

The benchmark measures p50, p99, memory usage, and CPU utilization.

---

## Key Takeaways

1. **OS threads:** 1–8MB each, 1–10μs context switch. Good for CPU work and legacy blocking code.
2. **Goroutines:** 2KB each, ~100ns switch, M:N scheduling. Go's sweet spot — simple code, async performance.
3. **async/await:** ~2KB state machine, no context switch for scheduling. Best for I/O-bound, high-concurrency Python and Node.js.
4. **Python GIL:** threads cannot parallelize CPU work in CPython. Use `multiprocessing` for CPU. Use `asyncio` for I/O.
5. **Go's goroutines** give you both: one goroutine per connection (like thread-per-connection simplicity) at async memory costs.
6. **Never block the event loop:** any synchronous work > 1ms in Node.js or Python asyncio stalls all other connections.
7. **Goroutine leaks** grow linearly; detect via `runtime.NumGoroutine()` metric. Always pass `context.Context` for cancellation.
8. **The decision tree:** I/O-bound → async or goroutines. CPU-bound → OS threads (Go/Node) or processes (Python).
