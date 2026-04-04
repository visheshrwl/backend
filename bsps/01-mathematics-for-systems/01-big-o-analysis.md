# Big-O Analysis

## Problem

Backend engineers make decisions every day that implicitly involve complexity analysis: "Should I fetch all users and filter in code, or add a WHERE clause?" "Should I sort in the application or the database?" Without formal complexity analysis, these decisions are guesses.

## Why It Matters

```
O(N) filtering on 100 rows:     100 comparisons  → 0.1ms
O(N) filtering on 10M rows:  10,000,000 comparisons → 10,000ms (10 seconds)
O(log N) index lookup on 10M rows: ~23 comparisons  → 0.023ms
```

The difference between O(N) and O(log N) is the difference between "works" and "doesn't work" at scale.

## Mental Model

Big-O describes how the number of operations grows as input size N grows, ignoring constant factors.

```
O(1):      1 operation regardless of N          → hash map lookup
O(log N):  doubles work when N squares           → binary search, B-tree
O(N):      work scales linearly with N           → linear scan
O(N log N): N × log N                            → merge sort, index build
O(N²):     work squares when N doubles           → nested loops, naive JOIN
O(2^N):    doubles with each added element       → brute-force combinatorics
```

## Backend-Specific Examples

```
Operation                               Complexity      Why
─────────────────────────────────────────────────────────────────
Hash map (dict) lookup                  O(1)            Direct memory address
N+1 query loop                          O(N) queries    One query per row
LIMIT/OFFSET pagination (page 1000)     O(OFFSET)       DB scans skipped rows
Cursor-based pagination                 O(1)            WHERE id > cursor
Unindexed search on table of N rows     O(N)            Full table scan
Indexed search on table of N rows       O(log N)        B-tree traversal
JOIN on N×M rows without index          O(N×M)          Nested loop join
JOIN on N×M rows with index             O(N log M)      Index lookup per left row
Sorting N items                         O(N log N)      Best possible comparison sort
Building an inverted index on N words   O(N)            One pass
Redis LRANGE on list of N items         O(S+N)          S=start offset, N=returned
Redis KEYS pattern match on N keys      O(N)            Full keyspace scan
```

## Complexity Analysis

### Space Complexity

Space complexity measures memory usage as a function of input:

```python
def n_plus_one(users):          # O(1) space (iterates, doesn't store all posts)
    for user in users:
        posts = db.query(user.id)  # fetched, used, discarded
        display(posts)

def load_all_in_memory(users):  # O(N×M) space — stores all posts in memory
    return {user.id: db.query(user.id) for user in users}
```

### Amortized Complexity

Some operations are occasionally expensive but cheap on average. Python's `list.append()`:

```python
# list.append() is O(1) amortized
# When list is full, it doubles in size: O(N) copy
# But doublings happen at 1, 2, 4, 8, ... — total copies for N appends: O(N)
# Amortized per-operation: O(N)/N = O(1)
```

Redis `LPUSH` is O(1). Building an N-element sorted set with `ZADD` N times is O(N log N).

## When to Worry About Complexity

```
N < 1,000:    Algorithm choice rarely matters. Optimize for readability.
N = 10,000:   O(N²) becomes painful (100M operations). Audit hot loops.
N = 100,000:  O(N log N) is fine (1.7M operations). O(N²) is broken.
N = 10M:      O(N) requires care (10M operations). O(log N) preferred.
N = 1B:       O(N) is architecture-level work. Only O(log N) or O(1) is safe.
```

## Benchmark

```
Operation on N=1,000,000 items:
  O(1)      hash lookup:    0.0001ms  (1 operation)
  O(log N)  B-tree lookup:  0.02ms    (20 operations)
  O(N)      linear scan:    100ms     (1M operations @ 100ns each)
  O(N log N) sort:          2,000ms   (20M operations)
  O(N²)     naive distinct: 100,000ms (1T operations — never do this)
```

## Failure Modes

**Accidental O(N²) in loops:**
```python
# O(N²) — creates a new list comprehension inside a loop
for user in users:          # O(N)
    if user.id in [u.id for u in banned]:  # O(N) per iteration
        block(user)

# Fix: convert to O(1) lookup first
banned_ids = {u.id for u in banned}  # O(N) once
for user in users:          # O(N)
    if user.id in banned_ids:  # O(1) per iteration
        block(user)
```

## Key Takeaways

1. O(1) = constant time. O(log N) = index scan. O(N) = full scan. O(N²) = always fix this.
2. N+1 queries are O(N) queries — the algorithm is wrong, not just slow.
3. Always analyze the bottleneck operation, not just the total function.
4. Amortized complexity matters: Python list append is O(1) amortized, not O(N).
5. For N > 10,000, algorithm choice dominates hardware choice.
