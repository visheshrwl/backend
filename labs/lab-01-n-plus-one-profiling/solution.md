# Lab 01 Solution

## Expected Results

After running `lab01.py`, you should see:

```
Approach              Queries     p50 (ms)    p99 (ms)    Min (ms)
----------------------------------------------------------------------
N+1 (naive)               101         3-8          8-20       2-5
JOIN (eager)                1        0.3-1        0.5-2      0.2-0.8
IN batch                    2        0.4-1        0.6-2      0.3-0.9
```

SQLite in-memory has negligible I/O overhead, so the speedup is 5–20× rather than the 50–100× you'd see over a real network.

## Why These Numbers

The N+1 approach fires 101 SQL statements through the SQLite C library. Even without network overhead, parsing and executing 101 statements takes ~4–8ms.

The JOIN fires one statement that the SQLite query planner executes with a single hash join, returning 500 rows in one pass.

## What to Do if Your Numbers Are Different

- Significantly faster: SQLite is being cached aggressively. Run 10 iterations and take the median.
- Significantly slower: Machine is under load. Close other applications.
- All times are 0ms: Run 100 iterations instead of 5.

## Key Insight

The query count reduction (101 → 1) is the mechanism. The timing improvement is the result. In production with 5ms RTT: 101 × 5ms = 505ms vs 1 × 5ms = 5ms — a 100× improvement.
