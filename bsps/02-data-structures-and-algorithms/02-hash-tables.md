# Hash Tables

## Problem

Hash tables provide O(1) average-case get, put, and delete. They underpin Redis, Python dicts, Go maps, JavaScript objects, and database hash indexes. Understanding their internals explains why they can degrade to O(N) and how to prevent it.

## Why It Matters

```
Linear search in list:  O(N) → 10ms for N=100,000
Hash table lookup:      O(1) → 0.001ms regardless of N
```

Python dict, Go map, Redis hash, HTTP header lookup — all O(1). Your application's performance often depends on how many O(N) list lookups you are accidentally doing instead.

## Mental Model

```
key → hash_function(key) → bucket_index → linked list of entries

┌──────────────────────────────────────────────────┐
│  Hash Table (capacity = 8 buckets)               │
│                                                  │
│  [0]: "apple" → 42  ──────────────────────       │
│  [1]: (empty)                                    │
│  [2]: "banana" → 7 ─► "cherry" → 15 (collision) │
│  [3]: (empty)                                    │
│  [4]: "date" → 99  ──────────────────────        │
│  ...                                             │
└──────────────────────────────────────────────────┘

get("banana"):
  1. bucket = hash("banana") % 8 = 2        O(1)
  2. Walk list at bucket[2] for "banana"     O(1) if short list
```

## Hash Functions

A good hash function distributes keys uniformly across buckets. Python uses SipHash-1-3 (secure, resistant to hash flooding attacks). Go uses AES-based hashing on architectures that support AES-NI.

```python
# Python's hash() function (simplified concept, not actual implementation)
hash("hello")   # → some large integer, e.g., -3550055125485641917
bucket_index = hash("hello") % table_capacity
```

## Collision Resolution

**Chaining:** Each bucket holds a linked list. Worst case: all N keys hash to one bucket → O(N).

**Open addressing:** On collision, probe to next bucket. Better cache locality than chaining.

**Python 3.6+ dict:** Compact array layout + hash table. Maintains insertion order. Load factor target: 2/3.

## Load Factor and Rehashing

```
load_factor = entries_count / bucket_count

When load_factor > threshold (Python: 2/3, Go: ~6.5):
  → Allocate new array with 2× capacity
  → Re-hash all entries into new array   O(N) operation
  → Amortized O(1) per insertion

Rehashing example:
  dict has 4 buckets, 3 entries (load 0.75 > 0.67)
  → allocate 8 buckets
  → re-insert all 3 entries (rehash each key)
  → future inserts fit without rehashing for a while
```

## Complexity Analysis

| Operation | Average | Worst Case | Worst Case Cause |
|-----------|---------|------------|-----------------|
| get(key) | O(1) | O(N) | All keys in one bucket |
| put(key) | O(1) amortized | O(N) | Rehashing |
| delete(key) | O(1) | O(N) | All keys in one bucket |
| Iteration | O(N) | O(N) | Must visit all buckets |

## Benchmark

```
N=1,000,000 string keys:
  dict[key] lookup:    ~0.05μs/lookup
  list.index(key):     ~5ms     (100,000× slower)
  set membership test: ~0.04μs  (hash set, no value stored)

Hash collision attack (Python 2, before SipHash):
  Adversarial keys all hash to same bucket → O(N) per lookup
  Mitigated in Python 3 with randomized hash seed (PYTHONHASHSEED)
```

## Failure Modes

**Memory overhead:** Each hash table entry has overhead (hash value, key, value, next pointer). A hash table with 1M entries of 8-byte ints uses significantly more memory than an 8MB array of ints.

**Hash flooding attack:** Without a randomized seed, an attacker can craft keys that all collide → O(N) server-side processing per request (DoS). Python 3, Go, and Node.js all use randomized seeds.

**Map iteration order:** Go maps iterate in random order by design (to prevent relying on implementation details). Python dicts maintain insertion order since Python 3.7.

## Key Takeaways

1. O(1) average, O(N) worst case. Good hash function + bounded load factor ensures O(1) in practice.
2. Load factor drives rehashing. At 2/3 capacity, Python rehashes → O(N) cost, O(1) amortized.
3. Always use hash sets/maps for membership testing. Never `if x in list`.
4. Hash flooding is a real security concern — use PYTHONHASHSEED or equivalent.
5. Redis hashes, Python dicts, Go maps, HTTP header maps — all the same data structure.
