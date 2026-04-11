# 03-trees-and-indexing

## Problem

A table with 50 million rows and no index on the filter column is not slow — it is O(N). These are different problems. Slow implies that optimization might help. O(N) means that every doubling of your dataset doubles the query time, regardless of how fast your hardware is, how much you cache, or how many replicas you add. You cannot hardware-purchase your way out of an algorithmic complexity problem.

The inverse failure is symmetric and equally expensive: over-indexing. A table with 11 indexes on it processes every `INSERT` and `UPDATE` as 12 write operations — one to the table, one to each index. For write-heavy workloads, indexes you added "just in case" for queries that run twice a day are silently degrading your write throughput around the clock. The query planner may not even use them.

Most engineers learn that indexes speed up reads. Fewer learn *which* index structure to use for *which* query pattern, *why* the planner sometimes ignores the index you created, and *when* an index makes things worse. This module covers all three.

---

## Why It Matters (Latency, Throughput, Cost)

**The O(log N) vs O(N) cliff in production.** A sequential scan of a 1M-row table on a modern NVMe drive reading 4KB pages takes on the order of 250ms. An index lookup on the same table takes ~0.5ms — 500× faster. At 10M rows, the scan is 2.5 seconds; the index lookup is still ~0.5ms. The index's advantage grows logarithmically with data size, which means the value of correct indexing compounds as your dataset grows. Teams that defer indexing decisions to "we'll add them when we need them" are betting that their data won't grow — which is a bad bet.

**Write amplification from over-indexing.** InnoDB updates every secondary index on every row mutation. For a table with k secondary indexes, each `UPDATE` touching an indexed column causes k+1 write operations plus k B-tree rebalancing operations. At high write throughput, this is not an abstract concern — it manifests as index write amplification that saturates I/O before the table itself becomes the bottleneck. `SHOW ENGINE INNODB STATUS` will tell you. The fix is dropping indexes that aren't earning their write overhead.

**Query planner cardinality errors are a systems failure mode.** The planner uses index statistics to decide whether an index scan or sequential scan is cheaper. These statistics are sampled, not exact. When the planner's cardinality estimate is wrong by an order of magnitude — which happens after bulk inserts, on skewed distributions, or when `ANALYZE` hasn't run recently — it makes the wrong structural decision. A query that should take 2ms via index scan takes 8 seconds via sequential scan, not because the index is missing but because the planner chose not to use it. `EXPLAIN (ANALYZE, BUFFERS)` is how you distinguish these cases.

---

## Mental Model

Every index structure encodes an assumption about access patterns. Before choosing a structure, identify your query's shape:

- **Point lookup**: equality predicate on a single key (`WHERE id = ?`) — hash index wins on pure lookup speed, but B-tree is a practical superset with range capability
- **Range scan**: ordered traversal between bounds (`WHERE created_at BETWEEN ? AND ?`) — requires an ordered structure; hash indexes cannot do this
- **Prefix scan**: all keys sharing a common prefix — trie or composite B-tree index with leftmost-prefix alignment
- **Membership test**: does this key exist? — Bloom filter at O(1) with bounded false positive rate; B-tree for exact answers
- **Nearest neighbor / spatial**: geometric proximity — R-tree or space-filling curve (Z-order) encoding into a B-tree
- **Write-heavy, read-occasionally**: LSM tree — amortize write cost by batching and sorting offline

No single structure dominates all access patterns. A system that uses only B-tree indexes for everything has implicitly made a product decision: reads matter more than writes, and range queries matter more than pure membership tests. For many systems, this is correct. Not all.

---

## Underlying Theory

### B-Trees: Why the Branching Factor Matters More Than the Depth

A B-tree of order `t` (minimum degree) satisfies: every non-root node has between `t-1` and `2t-1` keys; every node has between `t` and `2t` children. The tree height is bounded by `log_t(N)`. For `t = 512` and `N = 10^9`, height ≤ 3.

This is the key insight: the branching factor `t` is chosen to match the disk page size, not for algorithmic elegance. A 16KB page holding 512 8-byte keys and 513 child pointers gives t=512. At t=512, a billion-row table needs at most 3 page reads from root to leaf. InnoDB's default page size is 16KB; PostgreSQL uses 8KB.

The implication: **B-tree performance is bounded by I/O, not computation**. Each node traversal is a page read. In-memory B-trees hit L1/L2 cache (a few nanoseconds); on-disk B-trees hit storage (microseconds for NVMe, milliseconds for HDD). This is why the root node and upper levels are almost always in the buffer pool — they are the most frequently accessed pages in the entire database. Evicting the root from cache doesn't just hurt one query; it hurts every single query that touches this table.

### B+ Trees: The Linked Leaf Layer

Databases don't use vanilla B-trees — they use B+ trees, with one critical structural difference: all data lives in the leaf nodes, and the leaf nodes form a doubly-linked list.

Internal nodes contain only keys and child pointers — no row data. This increases the branching factor (more keys fit per page) and decouples range scan performance from tree height. A range scan reaches the starting leaf via O(log N) traversal, then follows the leaf linked list for O(K) additional reads to retrieve K results. No backtracking through internal nodes is needed.

This linked-leaf structure is why `ORDER BY` on an indexed column is "free" — the index is already sorted, and the query planner can traverse it without a sort step. It is also why covering indexes (see below) eliminate heap fetches entirely: all required columns are in the leaf layer, and the query is answered without ever touching the actual table pages.

### Clustered vs. Secondary Indexes

**Clustered index** (InnoDB term; PostgreSQL calls it a heap with a separate index): the table rows are physically stored sorted by the clustered key. In InnoDB, every table has exactly one clustered index — the primary key, or the first unique non-null key, or an internally generated 6-byte row ID. The clustered index leaf pages *are* the table. There is no separate row store.

Consequences:
- Primary key lookups are single-tree traversals to the leaf, which contains the row data. No heap fetch.
- Secondary index lookups in InnoDB are two-tree traversals: first the secondary index (to find the primary key), then the clustered index (to fetch the row data). Double the I/O.
- Primary key choice matters more than most engineers realize. A random UUID primary key causes random-access writes across the clustered B-tree, destroying spatial locality and causing page splits on nearly every insert. A monotonically increasing key (auto-increment, ULIDs, timestamp-prefixed IDs) appends to the rightmost leaf — sequential writes, minimal fragmentation, high fill factor.

**Covering index**: a secondary index that includes all columns required by a query, so the planner never needs to fetch the actual row. In PostgreSQL: `CREATE INDEX ON orders (customer_id) INCLUDE (total_amount, status)`. In MySQL: any secondary index can be a covering index if all `SELECT`, `WHERE`, and `ORDER BY` columns are present. When the query planner uses a covering index, `EXPLAIN` shows "Index Only Scan" (PostgreSQL) or "Using index" (MySQL). No heap access. Dramatically lower I/O, especially when the heap pages are not in cache.

### LSM Trees: Write-Optimized Indexes

B-trees pay a write overhead: random writes to arbitrary tree nodes cause random I/O, page splits, and rebalancing. For write-heavy workloads (time-series, event logging, message queues, append-heavy analytics), this is the wrong trade-off.

Log-Structured Merge trees (LSM trees) invert it. All writes go to an in-memory structure (memtable, typically a skip list or red-black tree) — sequential writes, O(log N) in-memory. When the memtable reaches a size threshold, it is flushed to disk as an immutable sorted file (SSTable, Sorted String Table). Reads query the memtable first, then progressively older SSTables. Periodically, SSTables are merged and compacted in the background — maintaining sort order and evicting deleted/overwritten keys.

The trade-off table:

| Property | B-tree | LSM tree |
|---|---|---|
| Write latency | O(log N) with random I/O | O(1) amortized, sequential |
| Write amplification | Low (in-place) | High (compaction rewrites data) |
| Read latency | O(log N) single tree | O(log N × number of levels) |
| Space amplification | Low | Moderate (uncompacted data) |
| Range scan | Excellent (linked leaves) | Good (SSTables are sorted) |

Used by: RocksDB (and everything built on it: TiKV, CockroachDB's storage layer, MyRocks, Pebble), Apache Cassandra, LevelDB, InfluxDB, ScyllaDB.

The read overhead from querying multiple SSTables is mitigated by Bloom filters at each SSTable level — before reading an SSTable for a key, check the Bloom filter. If negative, skip the file entirely. This reduces LSM read I/O to typically 1-2 SSTable reads even across many levels, at the cost of ~10 bits per key of Bloom filter memory.

### Skip Lists: Concurrent Ordered Structures

A skip list maintains a sorted linked list with express lanes — additional layers of pointers that skip over `p` fraction of elements. With each higher layer skipping 1/p nodes on average, search time is O(log_{1/p} N). For p=1/2, this is O(log N) with high probability.

Skip lists achieve the same asymptotic complexity as balanced BSTs with a critical advantage: **lock-free concurrent implementations are feasible**. A concurrent B-tree requires node locking during rebalancing — a global coordination point. A concurrent skip list can be implemented with compare-and-swap operations on individual pointers, enabling fine-grained lock-free concurrency. Java's `ConcurrentSkipListMap` uses this. Redis sorted sets use a skip list internally for their O(log N) rank operations.

The downside: skip lists use O(N log N) expected memory (versus O(N) for B-trees) due to the express-lane pointers. For in-memory indexes where concurrency matters more than memory, skip lists are frequently the correct choice. For on-disk structures, B-trees win because page-aligned access patterns don't benefit from skip list's pointer topology.

### Tries: Prefix-Indexed Structures

A trie (prefix tree) stores keys character by character, sharing prefixes among keys. Lookup time is O(L) where L is the key length — independent of N, the number of keys. This is asymptotically better than O(log N) for long keys over large key sets.

Production uses:

- **IP routing tables (longest prefix match)**: a 32-bit IPv4 address is a 32-bit binary trie. For each incoming packet, find the longest matching prefix in the routing table. Hardware implementations (TCAM) can do this in a single clock cycle. Software implementations use compressed tries (Patricia/radix trees) to reduce memory and cache footprint.

- **Autocomplete and prefix search**: all keys sharing a prefix `p` are descendants of the trie node for `p`. Prefix queries that require a table scan or a range query in a B-tree (`WHERE name LIKE 'foo%'`) are O(prefix_length) in a trie. PostgreSQL's `pg_trgm` extension approximates this for arbitrary substrings using trigram indexes — a trie generalization.

- **DNS resolution**: the DNS hierarchy is a distributed trie partitioned by label. `api.example.com` is resolved right-to-left: `.com` → `example.com` → `api.example.com`. Each level is delegated to a different authoritative server — a distributed trie lookup with caching at each node.

A **radix tree** (compressed trie) merges single-child chains into single edges, reducing memory from O(N × alphabet_size) to O(N). The Linux kernel's routing table is a radix tree. Go's `net/http` router (from 1.22's `ServeMux` upgrade) uses a radix tree for O(L) route matching.

### Composite Index Column Ordering: The Leftmost Prefix Rule

A composite index `(a, b, c)` can satisfy queries that filter on `(a)`, `(a, b)`, or `(a, b, c)` — but not queries that filter only on `(b)` or `(c)` or `(b, c)`. This is the leftmost prefix rule: the index is sorted by `a` first, then `b` within each `a` group, then `c` within each `a, b` group. A filter on `b` alone requires a full scan because `b` values are not globally ordered.

Column ordering rules:
1. **Equality predicates first.** `WHERE a = ? AND b > ?` — put `a` before `b`. The index narrows by `a` first, then scans the `b` range within that partition.
2. **Range predicate last.** A range on column `b` stops the index from filtering on `c` — all columns after the range predicate are not usable. `WHERE a = ? AND b BETWEEN ? AND ? AND c = ?` — the index `(a, b, c)` uses `a` and `b` but not `c`.
3. **Selectivity guides column order when all predicates are equality.** Put the most selective column (highest cardinality) first to prune the most rows at the first level. This doesn't affect correctness but does affect how much of the B-tree is traversed before the result is isolated.

### Graph Algorithms: Routing and Dependency Resolution

Graphs appear in backend systems wherever entities have directed relationships:

**Network routing (Dijkstra's algorithm):** Given a weighted directed graph where nodes are routers/datacenters and edge weights are latency or cost, find the shortest path from source to destination. Dijkstra runs in O((V + E) log V) with a binary heap priority queue. OSPF (link-state routing protocol) runs Dijkstra locally at each router after propagating the full topology via flooding. BGP (path-vector routing between ASes) uses a different mechanism — path selection based on policy, not shortest path — but the underlying data structure is still a directed graph.

**Dependency resolution (topological sort):** A package dependency graph, a Makefile, a DAG of database migrations, a service startup order — all are directed acyclic graphs that require topological ordering. Kahn's algorithm: start with nodes that have no incoming edges, process them, remove their outgoing edges, repeat. O(V + E). A cycle in the dependency graph means topological sort fails — which is the correct detection mechanism for circular dependencies. If your build system, service initializer, or migration runner silently hangs on startup, check for dependency cycles.

**Service mesh and load balancing (BFS/DFS):** Health-checking a cluster topology, propagating configuration changes, detecting unreachable nodes — all are graph reachability problems. BFS finds the shortest path in hop count (unweighted graph), useful for finding the minimum-latency route when all hops have equal latency. DFS is used in garbage collection (mark phase: DFS from root objects, anything not visited is unreachable) and in Tarjan's algorithm for strongly connected component detection.

---

## Complexity Analysis

| Structure | Point Lookup | Insert | Range Scan | Delete | Space |
|---|---|---|---|---|---|
| Unsorted array | O(N) | O(1) amortized | O(N) | O(N) | O(N) |
| Sorted array | O(log N) | O(N) | O(log N + K) | O(N) | O(N) |
| Hash table | O(1) avg | O(1) avg | O(N) | O(1) avg | O(N) |
| B+ tree | O(log N) | O(log N) | O(log N + K) | O(log N) | O(N) |
| Skip list | O(log N) | O(log N) | O(log N + K) | O(log N) | O(N log N) |
| LSM tree | O(log N) levels | O(1) amortized | O(log N + K) | Tombstone O(1) | O(N) + amplification |
| Trie | O(L) | O(L) | O(L + K) | O(L) | O(N × Σ) |
| Bloom filter | O(k) — false positives | O(k) | No | No | O(N) bits |

K = number of results returned. L = key length. k = number of hash functions. Σ = alphabet size.

Note that the LSM tree insert is O(1) amortized because compaction is background work — individual writes are fast, but total I/O is higher than B-trees due to write amplification from compaction. RocksDB's write amplification factor is typically 10-30× depending on compaction strategy.

---

## Key Takeaways

1. B+ tree leaf nodes form a linked list. Range scans traverse this list without revisiting internal nodes — this is why ordered indexes support `ORDER BY` without a sort step and why covering indexes can answer queries entirely from index pages without touching heap pages.

2. InnoDB secondary index lookups are two-tree operations: secondary index → primary key, then clustered index → row data. A covering index eliminates the second traversal. For read-heavy workloads with known query shapes, covering indexes are among the highest-leverage optimizations available.

3. Random primary keys (UUIDs v4) destroy write performance on clustered indexes by causing random B-tree insertions and page splits. Use monotonically increasing keys (auto-increment, ULIDs, UUIDs v7) for append-heavy tables.

4. LSM trees are the correct choice when write throughput is the primary constraint. Every major write-optimized storage engine — RocksDB, Cassandra, LevelDB — is an LSM tree. The trade-off is read amplification (multiple SSTable levels) and write amplification (compaction). Bloom filters at each SSTable level mitigate read amplification to typically 1-2 file reads.

5. The leftmost prefix rule is not a database quirk — it is a consequence of B+ tree sort order. A composite index `(a, b, c)` is sorted by `a`, then `b`, then `c`. Queries that don't filter on `a` cannot use this sort order as a starting point. Column order in composite indexes is a design decision with correctness implications, not just a performance preference.

6. The query planner may choose a sequential scan over an index when it estimates that the index selectivity is too low — i.e., it would fetch so many rows via the index that random I/O to the heap would cost more than a sequential scan. This is often correct. When it's wrong, it's because the cardinality statistics are stale. `ANALYZE` the table; force a replan.

7. Topological sort fails on cyclic graphs — use this as a correctness check for dependency resolution, migration ordering, and service startup sequences. If Kahn's algorithm terminates with unprocessed nodes, there is a cycle.

8. Graph traversal (Dijkstra, BFS, topological sort) is not just for network routing — it underlies package managers, build systems, service meshes, GC mark phases, and migration runners. Recognizing the underlying graph problem lets you apply known-correct algorithms instead of inventing ad-hoc solutions.

---

## Related Modules

- `../../06-databases/02-indexing.md` — B+ tree internals in InnoDB and PostgreSQL, covering indexes, index-only scans, VACUUM and index bloat
- `../../04-computer-networks/04-dns-and-load-balancing.md` — trie-based routing tables, Dijkstra in OSPF, BGP path selection
- `../04-queueing-theory.md` — skip lists in Redis sorted sets; priority queues in scheduler implementations
- `../../09-performance-engineering/02-latency-analysis.md` — using EXPLAIN ANALYZE to diagnose planner cardinality errors and missed index opportunities