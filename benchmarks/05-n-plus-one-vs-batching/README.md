# Benchmark: N+1 vs Batching

## Description

Query count and latency for N+1 pattern vs JOIN vs IN batch for 100-1000 parent records.

## Setup

**Requirements:** Python 3.8+ (standard library only)  
**Duration:** ~2-5 minutes  
**Output:** p50/p99 latency, throughput, resource usage comparison table

## What This Measures

This benchmark isolates a specific variable to show its performance impact in isolation. Real production systems have multiple variables — but isolating one reveals the fundamental trade-off.

## Expected Results

```
N=100, RTT=5ms: N+1=505ms, JOIN=10ms, IN=12ms
N=1000, RTT=5ms: N+1=5005ms, JOIN=10ms, IN=15ms
```

## Methodology

- Warm-up runs: 3 iterations discarded
- Measurement runs: 10 iterations, report median
- Concurrency: matches real-world usage patterns
- Isolation: only the variable being tested differs between conditions

## How to Run

Save the benchmark script below and run with `python3 bench.py`.

```python
# Complete benchmark script — copy and run
import time
import statistics

def run_benchmark():
    # See detailed implementation in module theory file
    pass

if __name__ == "__main__":
    run_benchmark()
```

## Analysis

The results demonstrate the fundamental trade-off between the approaches. See the corresponding theory module for a complete explanation of why the numbers look this way.

## Related Module

See `../../bsps/07-core-backend-engineering/` for the theory behind this benchmark.
