# 05-sorting-and-searching

## Problem

Sorting is the most studied problem in computer science, and also the most misunderstood in production. Not because engineers don't know what sorting is — they do. Because they treat it as solved. They call `.sort()`, the array gets sorted, and they move on. The algorithm beneath that call makes decisions they never examined, under assumptions they never verified, with performance characteristics that will surprise them at scale.

The misunderstanding has a specific shape. Engineers know that quicksort is O(N log N) average and O(N²) worst case. What they don't know is that "worst case" is not a theoretical curiosity — it is a security vulnerability. An attacker who can control your input can force O(N²) behavior in a naïve quicksort, turning your API endpoint into a CPU denial-of-service vector. CVE-2011-4885 (PHP hash collision DoS), CVE-2003-0364 (various web frameworks) — these are sorting and hashing assumptions violated by adversarial input.

They know binary search is O(log N). They don't know that the textbook implementation has a bug that manifests only for arrays larger than 2³⁰ elements — a bug that existed in Java's standard library for nearly a decade (Bloch, 2006, same author as the binary search bug from module 05-numerical-stability). They don't know that binary search on modern hardware is often slower than linear search for N < 64 due to branch misprediction, and that the SIMD-based linear scan in their database engine is beating their O(log N) algorithm on the inputs that actually appear in production.

The gap between asymptotic analysis and production performance is widest for sorting and searching. This module closes it.

---

## Why It Matters (Latency, Throughput, Cost)

**Database query plans pivot on sort cost.** When PostgreSQL plans `SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT 10`, it chooses between: (a) index scan on `(customer_id, created_at DESC)` — O(log N + 10) — or (b) index scan on `customer_id` followed by an in-memory sort — O(K log K) where K is the number of rows for that customer. The decision depends on K, on whether the composite index exists, on the `work_mem` setting that governs whether the sort spills to disk, and on the query planner's estimate of K. A stale statistics estimate of K = 100 when reality is K = 500,000 causes the planner to choose the in-memory sort path, which spills to disk, which turns a 2ms query into a 4-second query. The fix requires understanding both the sort algorithm the planner uses (external merge sort for disk-spilling workloads) and why its cardinality estimate was wrong.

**Merge sort is the only correct algorithm for external sorting.** When your dataset doesn't fit in memory — and at scale, it won't — the sorting algorithm is constrained by I/O, not CPU. External merge sort divides the input into chunks that fit in memory, sorts each chunk, then merges. The merge phase reads each record exactly once and writes each record exactly once: O(N/B × log(N/B)) I/Os where B is the block size. This is optimal. Any algorithm that requires random access to disk (quicksort, heapsort) is catastrophically worse on spinning disk and significantly worse on NVMe. Every database engine, every Hadoop/Spark shuffle, every external sort utility uses merge sort. This is not a coincidence.

**Sort stability determines correctness in multi-key sorting.** A stable sort preserves the relative order of equal elements. Python's Timsort is stable; C's `qsort` is not guaranteed stable; Java's `Arrays.sort` for objects is stable (merge sort-based), for primitives is not (dual-pivot quicksort). When you sort a list of orders by amount, then sort by customer ID, you expect the final result to be sorted by customer ID with ties broken by amount. This is only guaranteed if the second sort is stable — it must preserve the ordering established by the first. Teams that sort by multiple keys in sequence and don't use a stable sort are relying on undefined behavior. The correct approach is a single sort with a composite comparator, but if you must chain sorts, verify stability.

**Searching in cache is a different problem than searching in memory.** Binary search on an array of N integers has O(log N) comparisons and O(log N) cache misses — each comparison jumps to a distant memory location, causing a cache miss with high probability for large N. A B-tree with branching factor 16 has the same O(log N) comparisons but each comparison fetches a 64-byte cache line containing 16 keys — dramatically better cache behavior. For in-memory search over large arrays, a B-tree layout (van Emde Boas layout, Eytzinger layout) can outperform binary search by 3-5× purely through cache locality. This is why InnoDB's in-memory adaptive hash index coexists with the B-tree — hash lookup for point queries that hit the AHI, B-tree for everything else.

---

## Mental Model

Sorting and searching are not single problems — they are families of problems parameterized by:

- **Data location**: in memory, on disk, distributed across nodes
- **Data size**: fits in L1/L2/L3 cache, fits in RAM, doesn't fit in RAM
- **Input distribution**: random, nearly sorted, reverse sorted, many duplicates, adversarially constructed
- **Query type**: single lookup, repeated lookup, range lookup, approximate lookup, order statistics (kth smallest)
- **Stability requirement**: must preserve order of equals, or not
- **Comparison availability**: total order with comparator, only equality, only hash

The optimal algorithm changes with every dimension. The discipline is identifying which point in this space your problem occupies, then selecting the algorithm whose design assumptions match.

---

## Underlying Theory

### The Comparison Sort Lower Bound and What It Actually Means

The Ω(N log N) lower bound for comparison-based sorting is one of the few information-theoretic lower bounds that applies unconditionally to a class of algorithms. The proof: N! possible orderings of N elements, each comparison eliminates at most half the remaining possibilities, so at least ⌈log₂(N!)⌉ comparisons are necessary. By Stirling's approximation, log₂(N!) ≈ N log₂ N − N log₂ e ≈ N log₂ N. Therefore any comparison sort requires Ω(N log N) comparisons in the worst case.

This bound applies to the number of *comparisons*. It says nothing about:
- Cache misses (which often dominate wall time)
- Branch mispredictions (critical for in-memory sorting)
- Memory writes (relevant for cache bandwidth)
- Parallel work (merge sort parallelizes; insertion sort doesn't)

Algorithms that achieve O(N log N) comparisons can still differ by 5-10× in wall time due to these factors. This is why the "optimal" comparison sort — merge sort, achieving exactly N log N comparisons — is not always the fastest in practice.

**Beating the lower bound with non-comparison sorts.** If your keys have additional structure beyond a total order — they're integers, they're strings over a finite alphabet, they're fixed-length binary encodings — you can sort in O(N) or O(N + K) where K is the key range.

- **Counting sort**: O(N + K) time and space. For each of K possible key values, count occurrences, then reconstruct. Optimal for integer keys with small K. Used in: pixel sorting by color channel (K = 256), age sorting, grade sorting.
- **Radix sort**: O(d × (N + K)) where d is the number of digits and K is the digit range. Process digit by digit from least significant to most significant (LSD radix sort), using a stable counting sort at each digit. For 32-bit integers: d = 4 passes of base-256 counting sort, K = 256, total O(4N) — linear in N. Used in: GPU sorting (extremely parallelizable), network packet classification, any workload with fixed-width integer or binary keys.
- **Bucket sort**: O(N + K) average when input is uniformly distributed. Divide key range into K buckets, scatter elements, sort each bucket. If input is not uniform, degenerates to O(N²). Used in: numerical simulation outputs (often uniformly distributed by construction), hash-based distributed sorting.

The decision rule: if your keys are integers with bounded range, radix sort is likely faster than any comparison sort. If your keys are 64-bit timestamps and N > 10⁷, the 4-pass LSD radix sort will beat TimSort by 2-3× on modern hardware.

### Quicksort: Partition Strategies, Adversarial Inputs, and Introsort

Quicksort's expected O(N log N) with O(log N) stack space makes it the standard in-memory sort for most workloads. Its O(N²) worst case on sorted, reverse-sorted, or many-duplicates inputs is the practical concern.

**Lomuto vs Hoare partition.** Lomuto partition (the one taught in most textbooks) puts the pivot at its final position and requires N-1 comparisons for N elements. Hoare partition (Quicksort's original) requires at most N/3 comparisons on average and does not put the pivot at its final position — it only guarantees elements to the left are ≤ pivot and elements to the right are ≥ pivot. Hoare is faster in practice (fewer swaps, better cache behavior) but harder to implement correctly and less commonly shown in textbook code.

**Pivot selection.** Naïve pivot selection (first element, last element, or random element) is O(N²) on adversarially constructed inputs if the strategy is predictable. "Median-of-three" (median of first, middle, and last elements) defeats many degenerate cases. "Ninther" (median of three medians of three) is more robust. None of these are immune to adversarial input if the attacker can observe your pivot strategy.

**The adversarial input problem.** McIlroy (1999) published an algorithm that constructs a "killer" input for any comparison-based sort that reveals its pivot through comparisons. Applied to any deterministic quicksort, it produces O(N²) behavior. Web servers that sort query parameters or JSON keys using such a sort are vulnerable to DoS by sending requests with crafted key orderings. The mitigations:
1. **Randomized pivot selection**: pivot = random element. Expected O(N log N) regardless of input. Adversary cannot construct a killer sequence without observing random choices.
2. **Introsort**: the algorithm used by C++ `std::sort`, Rust's `slice::sort_unstable`, and most production standard libraries. Start with quicksort; if recursion depth exceeds 2 × log₂ N (indicating we may be hitting O(N²) behavior), switch to heapsort. Heapsort is O(N log N) worst case but slower in practice — introsort uses it as a fallback, not a primary algorithm. Combined with insertion sort for small subarrays (N ≤ 16), this is O(N log N) worst case with excellent constants.
3. **Pattern-defeating quicksort (pdqsort)**: the state of the art, used in Rust's standard library, Python (for small arrays inside Timsort), and recent C++ implementations. Detects sorted and reverse-sorted runs, uses block-based partition for better branch prediction, and falls back to heapsort. In practice, faster than introsort on real-world data distributions.

### Timsort: Exploiting Natural Order in Real Data

Timsort (Peters, 2002) is not merely a sorting algorithm — it is an algorithm designed for the specific distribution of real-world data: data that has natural runs of already-sorted elements. It exploits this structure to dramatically outperform general-purpose algorithms on typical inputs.

**Core mechanism.** Timsort scans the input for "runs" — maximal already-sorted (or reverse-sorted, which it reverses) subsequences. Runs shorter than `minrun` (computed as 32-64 based on N) are extended using insertion sort. Runs are pushed onto a stack. When the stack's top runs violate the invariant that `len(Z) > len(Y) + len(X)` and `len(Y) > len(X)` (where X, Y, Z are the top three runs), the two smallest runs are merged. This invariant ensures the number of runs on the stack stays O(log N) and that merge passes are balanced.

**Galloping mode.** When merging two runs A and B, if one run consistently contributes many consecutive elements (run A's elements are all smaller than B[0] for k consecutive elements), Timsort switches to "galloping mode" — exponential search to find how far into A the merge can advance before switching. This reduces comparisons from O(N) to O(log N) for inputs with long ordered subsequences. On already-sorted data, Timsort is O(N). On random data, it degrades to O(N log N) with constants comparable to merge sort.

Used by: Python, Java (for objects), Android, GNU Octave. When you call `.sort()` in Python on a list of real-world records, Timsort's run-exploitation means it frequently runs in O(N) or O(N log N) with very small constants — often faster than theoretical lower bound analysis would suggest because it matches the distribution.

### Binary Search: The Correct Implementation and Its Failure Modes

The textbook binary search:

```python
def binary_search(arr, target):
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2      # BUG: integer overflow for lo + hi > 2^63
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
```

The overflow bug: `(lo + hi)` overflows when `lo + hi > INT_MAX`. For Python this doesn't matter (arbitrary precision integers). For C, C++, Java, Go, Rust with checked arithmetic — it matters for arrays larger than 2³⁰ elements. The fix: `mid = lo + (hi - lo) // 2`. This is not theoretical — Java's `Arrays.binarySearch` had this bug for nine years.

**The boundary condition variants.** Binary search has multiple correct implementations depending on what you want to return when the target appears multiple times:

```python
def lower_bound(arr, target):
    """First index where arr[i] >= target. Equivalent to C++ std::lower_bound."""
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = lo + (hi - lo) // 2
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return lo

def upper_bound(arr, target):
    """First index where arr[i] > target. Equivalent to C++ std::upper_bound."""
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = lo + (hi - lo) // 2
        if arr[mid] <= target:
            lo = mid + 1
        else:
            hi = mid
    return lo
```

`lower_bound` and `upper_bound` together give you `[lo, hi)` — the half-open range of all occurrences of target. `upper_bound(arr, target) - lower_bound(arr, target)` is the count of target occurrences in O(log N). This is how databases implement index range scans: binary search to the lower bound of the range predicate, then scan forward to the upper bound.

**Branch misprediction and branchless binary search.** The conditional jump in the binary search inner loop is unpredictable — the branch predictor has ~50% accuracy on random data. On modern CPUs, a mispredicted branch costs ~15-20 cycles. For N = 10⁶, binary search takes ~20 iterations, each with ~10 cycles of misprediction cost on average = 200 cycles of branch misprediction waste per lookup.

Branchless binary search eliminates the conditional jump using conditional moves (CMOV on x86):

```c
// Branchless binary search in C
size_t branchless_lower_bound(int *arr, size_t n, int target) {
    size_t lo = 0;
    while (n > 1) {
        size_t half = n / 2;
        lo += (arr[lo + half - 1] < target) ? half : 0;  // compiled to CMOV
        n -= half;
    }
    return lo;
}
```

This compiles to a CMOV instruction — a conditional move with no branch. On random data, this is 1.5-3× faster than the branching version for large N. For small N (≤ 64), linear SIMD scan is often faster still due to vectorization.

**Eytzinger layout for cache-optimal binary search.** Standard binary search on a sorted array has poor cache behavior: the first comparison accesses index N/2 (likely in cache), the second accesses N/4 or 3N/4 (possible cache miss), and so on — each level of the search tree accesses a different cache line. The Eytzinger layout reindexes the array to match the binary search tree's access pattern, placing the root at index 1, its children at 2 and 3, their children at 4-7, and so on (identical to a BFS ordering of a complete binary tree). Now binary search accesses elements in indices 1, 2 or 3, 4-7... which are all in the first few cache lines. For N = 10⁶ integers, Eytzinger-layout binary search is ~2× faster than sorted-array binary search due to prefetching. This is the layout used in InnoDB's adaptive hash index and in several research database systems.

### Order Statistics and the kth Element Problem

Finding the kth smallest element in an unsorted array without fully sorting it. Naïve approach: sort in O(N log N), return element at index k. Optimal: Quickselect in O(N) average, O(N²) worst case. Optimal worst-case: Median-of-Medians in O(N) worst case.

**Quickselect:**

```python
import random

def quickselect(arr, k):
    """Returns the kth smallest element (0-indexed) in O(N) expected time."""
    if len(arr) == 1:
        return arr[0]
    pivot = random.choice(arr)
    lows  = [x for x in arr if x < pivot]
    highs = [x for x in arr if x > pivot]
    pivots = [x for x in arr if x == pivot]
    if k < len(lows):
        return quickselect(lows, k)
    elif k < len(lows) + len(pivots):
        return pivots[0]
    else:
        return quickselect(highs, k - len(lows) - len(pivots))
```

**Median-of-Medians** guarantees O(N) worst case by choosing a pivot that is guaranteed to be between the 30th and 70th percentile: divide into groups of 5, find the median of each group (O(1) per group, O(N/5) total), recursively find the median of those medians, use that as the pivot. The pivot eliminates at least 30% of elements per recursive call, giving T(N) = T(N/5) + T(7N/10) + O(N) which solves to O(N). In practice, Median-of-Medians has large constants and is rarely used in production — random pivot Quickselect with O(N) expected time is preferred.

**Production uses of order statistics:**
- **Database percentile queries**: `SELECT PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY latency) FROM requests`. PostgreSQL implements this with an in-memory sort followed by index access for online queries, or with external merge sort for large datasets.
- **Reservoir sampling for streaming percentiles**: see module 02. Quickselect applies to the reservoir to find percentiles in O(k) per query.
- **Median finding in monitoring**: computing the median of a metric across 10,000 host samples. Quickselect in O(N) rather than O(N log N) sort.

### External Sorting: When Your Data Doesn't Fit in RAM

External merge sort is the algorithm for sorting datasets larger than available memory. It operates in two phases:

**Phase 1 — Run generation.** Read M bytes of data (where M = available RAM), sort in memory using an internal sort (typically replacement selection or quicksort), write as a sorted "run" to disk. Repeat until all data has been processed. This produces ⌈N/M⌉ sorted runs.

**Replacement selection** generates runs approximately twice as long as M on average for random input, halving the number of runs. It maintains a min-heap of M elements; always output the minimum that is ≥ the last output value, replacing it with the next input element. When no such element exists, close the current run and start a new one. Expected run length: 2M.

**Phase 2 — Merge.** Merge all runs simultaneously using a min-heap of size equal to the number of runs. Each heap operation is O(log(number of runs)); total I/Os are O(N/B × log(N/M)) where B is the disk block size.

**Multi-way merge** with a heap of fan-in F allows merging F runs at once. Instead of binary merge (F=2), use F = available_memory / (2 × block_size) — maximize fan-in limited by memory. For F-way merge, the number of passes needed is ⌈log_F(N/M)⌉. For N = 1TB, M = 16GB, B = 4MB, F = 2048: one merge pass suffices. This is why large-scale sort-merge joins in databases complete in two I/O passes for datasets that fit in a few thousand disk blocks.

**Sort-merge join.** The fundamental join algorithm in databases and MapReduce:
1. Sort both relations by the join key (external merge sort if necessary)
2. Merge the two sorted sequences, emitting matching pairs

Total cost: O(N log N + M log M + N + M) I/Os where N and M are the sizes of the two relations. For large relations that don't fit in memory, this is optimal — no hash join can do better asymptotically, and sort-merge join produces sorted output as a side effect.

**Spark's shuffle.** A distributed sort. Each map task partitions its output by hash of the sort key into R partitions (one per reducer). Each reduce task fetches its partition from all map tasks (the "shuffle read") and sorts them — external merge sort if the partition doesn't fit in memory. The total data volume is O(N) regardless of the number of partitions; the challenge is coordinating the many-to-many network transfer efficiently. Spark's ExternalSorter uses spill-and-merge: when memory fills, sort the current buffer and spill to disk, then merge all spills at the end. This is external merge sort with the disk replaced by a mix of local disk and network.

### Approximate Sorting and Partial Ordering

Not all production sorting problems require a total order over all elements. Several important problems have more efficient solutions when approximate or partial answers suffice.

**Partial sort (top-K):** Return the K smallest elements, not necessarily sorted. Use a max-heap of size K: iterate through all N elements, push each onto the heap, pop the max whenever size exceeds K. After processing all elements, the heap contains the K smallest. O(N log K) time — for K << N, dramatically faster than full sort. This is `ORDER BY score DESC LIMIT 10` in a database: if the planner cannot use an index, it uses a top-K heap scan rather than sorting all rows.

**Approximate sorting with quantiles:** For large streaming datasets, maintaining an exact sort order is infeasible. Maintaining approximate quantiles (within ε relative error) is feasible using Greenwald-Khanna or KLL (Karnin, Lang, Liberty) sketches in O(1/ε × log(1/ε)) space. Used in distributed monitoring systems (Prometheus histograms, DataDog DDSketch, Netflix's Percentile Streams) to answer "what is the p99 latency?" across billions of events without storing them all.

**Cache-oblivious sorting (Funnelsort):** An algorithm that achieves optimal cache behavior — O(N/B × log_M/B(N/B)) cache misses — without knowing the cache parameters M (cache size) or B (cache line size) in advance. Funnelsort uses a recursive "funnel" structure where K-way merges are performed by K/2 recursively nested funnels. The result is optimal for any cache hierarchy — L1, L2, L3, RAM, disk — simultaneously. Used in research systems; approaching production use in columnar databases where cache behavior is the dominant cost.

### String Sorting: Radix vs Comparison vs Burst Sort

Sorting strings requires special treatment because string comparison is O(L) where L is the string length — the O(N log N) comparison count becomes O(N L log N) character comparisons.

**MSD (most-significant digit) radix sort for strings.** Sort by the first character into 256 buckets (for ASCII), then recursively sort each bucket by subsequent characters. Total work: O(N L) in the worst case (all strings share a long common prefix) but O(N + N × average_distinguishing_prefix_length) in practice. For strings with short distinguishing prefixes (URLs sorted by domain, IPs, UUIDs), MSD radix sort is dramatically faster than comparison sort.

**Burst sort.** Cache-aware string sorting that avoids cache thrashing from MSD radix sort's pointer-chasing. Maintains a trie of prefixes; strings are stored in "buckets" at the trie leaves. When a bucket exceeds a threshold, it "bursts" into a new trie level. The trie fits in cache; strings are sorted within each bucket using a fast in-memory sort. Used in production string sorting for large English-language datasets.

**Suffix arrays.** For sorting all suffixes of a string (the core operation in full-text indexing), the DC3/Skew algorithm sorts all N suffixes in O(N) time. Suffix arrays with LCP (longest common prefix) arrays enable O(log N) substring search in a text of length N — comparable to a B-tree index but for arbitrary substring queries, not just prefix queries. PostgreSQL's `pg_trgm` extension approximates this for trigram-based substring search.

---

## Complexity Analysis

| Algorithm | Best | Average | Worst | Space | Stable | Cache |
|---|---|---|---|---|---|---|
| Insertion sort | O(N) | O(N²) | O(N²) | O(1) | Yes | Excellent |
| Merge sort | O(N log N) | O(N log N) | O(N log N) | O(N) | Yes | Good |
| Quicksort (random) | O(N log N) | O(N log N) | O(N²) w.p. negligible | O(log N) | No | Excellent |
| Heapsort | O(N log N) | O(N log N) | O(N log N) | O(1) | No | Poor |
| Timsort | O(N) | O(N log N) | O(N log N) | O(N) | Yes | Excellent |
| Introsort / pdqsort | O(N log N) | O(N log N) | O(N log N) | O(log N) | No | Excellent |
| Counting sort | O(N + K) | O(N + K) | O(N + K) | O(K) | Yes | Good |
| LSD radix sort | O(dN) | O(dN) | O(dN) | O(N + K) | Yes | Good |
| Binary search | O(1) | O(log N) | O(log N) | O(1) | — | Poor (random) |
| Branchless binary search | O(1) | O(log N) | O(log N) | O(1) | — | Better |
| Quickselect (k-th element) | O(N) | O(N) | O(N²) | O(log N) | — | Good |
| External merge sort | — | O(N/B × log_F(N/M)) I/Os | same | O(M) RAM | Yes | Optimal for disk |

Note that "cache" column refers to practical performance on modern hardware, not asymptotic complexity. Poor cache behavior turns O(N log N) into a constant-factor loss of 2-5× versus an equivalent algorithm with better locality.

---

## Key Takeaways

1. The Ω(N log N) lower bound applies to *comparisons*, not wall time. Algorithms achieving identical comparison counts can differ by 5× in wall time due to cache misses and branch mispredictions. Profile on your actual hardware and data distribution — don't optimize comparison counts in isolation.

2. Timsort is optimal for real-world data because real-world data has structure. On uniformly random data, it behaves like merge sort. On nearly sorted data, it approaches O(N). If you're sorting records that arrive roughly in order (event logs, timestamped records, database rows inserted in sequence), Timsort will consistently outperform its O(N log N) bound.

3. Quicksort with naïve pivot selection is a security vulnerability on public-facing APIs that sort user-controlled input. Use introsort (C++ `std::sort`) or pdqsort (Rust's `slice::sort_unstable`) which are O(N log N) worst case. Never use textbook quicksort with first-element or last-element pivot on adversarially controllable data.

4. The binary search overflow bug (`(lo + hi) / 2`) is wrong in every language with fixed-width integers. The correct form is `lo + (hi - lo) / 2`. This matters for arrays larger than half INT_MAX — which exist in production systems today.

5. For integer keys with bounded range, LSD radix sort achieves O(N) time with excellent constants. For 32-bit integers, 4-pass base-256 radix sort processes ~500M integers/second on modern hardware — competitive with the best comparison sorts and without any comparison overhead.

6. External merge sort is the only viable algorithm when data exceeds RAM. Maximize fan-in (F = memory / (2 × block_size)) to minimize merge passes. For most production datasets on servers with 16-256GB RAM, two I/O passes (one for run generation, one for merging) are sufficient. Understand what algorithm your database uses for sort-merge joins and when it spills to disk (`work_mem` in PostgreSQL, `sort_buffer_size` in MySQL).

7. `ORDER BY ... LIMIT K` queries should use a top-K heap (O(N log K)) not a full sort (O(N log N)). Every major database engine implements this. If your query planner is doing a full sort followed by a limit, it has lost the ability to use the top-K optimization — usually because the sort column is not the leftmost column in a composite index, or because the planner's cost estimate is wrong.

8. String sorting is O(N L log N) with comparison sorts. For strings with short distinguishing prefixes (UUIDs, hashes, IPs), MSD radix sort in O(N L) is a significant improvement. For full-text substring search, suffix arrays enable O(log N) arbitrary substring lookups — a qualitatively different capability than B-tree prefix indexing.

---

## Related Modules

- `../../06-databases/02-indexing.md` — B+ tree sorted order as the foundation of range scans; external merge sort in sort-merge joins; the query planner's top-K heap optimization
- `../../06-databases/03-query-planning.md` — how the planner chooses between index scan, sort-then-limit, and sequential scan based on K estimate and work_mem
- `../03-trees-and-indexing.md` — Eytzinger layout binary search as the theoretical basis for B-tree cache behavior; skip lists as sorted structures with concurrent insertion
- `../../09-performance-engineering/02-latency-analysis.md` — Timsort on nearly-sorted latency samples; quickselect for streaming percentile computation
- `../02-probability-for-engineers.md` — reservoir sampling for approximate quantiles; the inspection paradox in profiler-observed sort behavior