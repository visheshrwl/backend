# Lab 02 Solution

## Expected Results

```
Config                      p50      p99   Throughput   Conn Created
------------------------------------------------------------------------
No Pool                   22-28ms  35-45ms      70-85/s            100
Pool size=1              500-600ms 900-1100ms    8-12/s               1
Pool size=10             100-130ms 200-220ms    42-55/s              10
Pool size=100             20-25ms   35-42ms     55-70/s             100
```

## Analysis

**No Pool:** Each of the 100 concurrent threads creates its own connection (15ms) then executes the query (10ms). The 15ms overhead shows up clearly in p50 vs Pool=10.

**Pool=1:** 100 requests competing for 1 connection. They execute serially in the connection. Queue wait time dominates latency. This demonstrates the catastrophic effect of an undersized pool.

**Pool=10:** 10 connections serve 100 requests in batches of 10. Approximately: ceil(100/10) × (15ms wait for conn warmup + 10ms query) ≈ 10 × 25ms = 250ms total. Individual p50 reflects median queue position.

**Pool=100:** All 100 requests get a connection immediately (no queueing). But 100 simultaneous DB queries are simulated to be slightly slower (15ms instead of 10ms) due to server-side scheduling overhead.

## Key Lesson

Pool size=10 achieves the best throughput per connection. Pool size=100 uses 10× more DB server resources for marginally better latency but lower throughput. Optimal pool size matches the DB server's capacity to process queries concurrently.
