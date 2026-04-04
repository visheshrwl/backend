# Arrays and Memory Layout

## Problem

Arrays are the foundation of all data structures. Understanding how they are laid out in memory explains why some operations are fast and others are not — and why your database's buffer pool, Redis strings, and CPU caches all fundamentally rely on contiguous memory.

## Why It Matters (Latency, Throughput, Cost)

**CPU cache line effects:**
```
Array access (sequential):    ~4 cycles per element  (L1 cache hit)
Array access (random):        ~40 cycles per element (L3 cache miss)
Linked list traversal:        ~200 cycles per node   (RAM access per node)

Sequential array: 1,000,000 elements summed → ~4ms
Linked list:      1,000,000 elements summed → ~200ms   (50× slower)
```

This is why PostgreSQL stores heap pages as fixed-size byte arrays, not linked structures.

## Mental Model

An array is a contiguous block of memory. Element i is at address: `base_address + i × element_size`.

```
Address:  0x1000  0x1008  0x1010  0x1018  0x1020
Value:    [  42  ][  17  ][  99  ][   3  ][  58  ]
Index:       0       1       2       3       4
```

CPU L1 cache fetches 64-byte **cache lines**. A 64-byte cache line holds 8 int64 values. Accessing element 0 loads elements 0–7 into cache "for free". Sequential access = 1 cache miss per 8 elements. Random access = potentially 1 cache miss per element.

## Underlying Theory

**Virtual memory:** Arrays occupy contiguous virtual addresses, but physical frames may be scattered. The MMU + TLB translates virtual to physical. Accessing a new page (4KB) on first touch triggers a page fault (OS intervention, ~1μs). Large arrays touch many pages.

**Row-major vs column-major storage:**
```
C/Python/Go: Row-major (row elements contiguous)
  matrix[i][j] → base + i * col_count * sizeof(T) + j * sizeof(T)

Fortran/MATLAB: Column-major (column elements contiguous)
  matrix[i][j] → base + j * row_count * sizeof(T) + i * sizeof(T)
```

NumPy operations iterate in the memory-contiguous direction for speed.

## Naive Approach

```python
# Cache-unfriendly: accessing column-major pattern in row-major array
matrix = [[random.random() for _ in range(1000)] for _ in range(1000)]

# Column-by-column access (cache miss per element in row-major layout)
total = sum(matrix[i][j] for j in range(1000) for i in range(1000))
```

## Optimized Approach

```python
# Cache-friendly: row-by-row access
total = sum(matrix[i][j] for i in range(1000) for j in range(1000))

# Even better: use NumPy's vectorized operations (SIMD + cache-optimized)
import numpy as np
matrix = np.random.random((1000, 1000))
total = matrix.sum()  # 100× faster than Python loop
```

## Complexity Analysis

| Operation | Time | Space | Notes |
|-----------|------|-------|-------|
| Random access by index | O(1) | O(1) | Direct address computation |
| Sequential scan | O(N) | O(1) | Cache-friendly |
| Insert at end (amortized) | O(1) | O(1) | Dynamic array doubling |
| Insert at front | O(N) | O(1) | Must shift all elements |
| Search (unsorted) | O(N) | O(1) | Linear scan |
| Search (sorted, binary) | O(log N) | O(1) | Binary search |

## Benchmark

```
Operation on N=1,000,000 int64 array:
  Sequential sum:    2ms    (L1/L2 cache hits)
  Random access sum: 100ms  (50× slower — L3/RAM misses)
  Python list sum:   50ms   (object overhead, indirect pointers)
  NumPy array sum:   1ms    (SIMD + contiguous memory)
```

## Key Takeaways

1. Contiguous memory = cache-friendly = fast sequential access.
2. Random access into large arrays → cache misses → 50× slower.
3. Python lists store pointers to objects, not objects themselves — extra indirection.
4. NumPy/C arrays store values directly — fully contiguous, SIMD-friendly.
5. Database pages are fixed-size contiguous byte arrays for this reason.
