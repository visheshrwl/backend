# Trees and Indexing

## Problem

Let me start with a sentence that has cost more engineering careers more sleep than any other: *"the query was fast in staging."*

Of course it was. Staging had ten thousand rows. Production has fifty million. And here's the thing nobody tells you clearly enough: a query without the right index isn't *slow* — it's **O(N)**, and those are completely different diagnoses. "Slow" suggests a tuning problem: add a cache, get a faster disk, throw more RAM at it, and you'll claw back some percentage. "O(N)" is a structural verdict: every time your data doubles, the query time doubles, *forever*, no matter how fast your hardware is or how many replicas you bolt on. You cannot buy your way out of a complexity class. The 50-million-row scan that takes 2.5 seconds today takes 5 seconds at 100M and 10 seconds at 200M, and the only thing that changes that trajectory is changing the *shape* of how you find the data. That shape is a tree.

But before you go index-happy, here's the mirror-image mistake, and it's just as expensive: **over-indexing.** Every index you add is a second (and third, and twelfth) data structure that must be kept in sync on *every single write.* A table with eleven indexes turns one `INSERT` into twelve write operations plus eleven B-tree rebalancings. If you sprinkled indexes "just in case" on a write-heavy table, you're paying that tax around the clock — on every insert, every update — to speed up a report that runs twice a day. And the cruelest part: the query planner might not even *use* the index you so carefully created.

So the actual skill here isn't "indexes make reads fast" — every engineer knows that slogan. The skill is the three questions almost nobody can answer cleanly: *which* tree structure fits *which* query shape, *why* the planner sometimes ignores the index you built, and *when* an index makes the whole system worse. That's what this chapter is really about. We're going to build the B-tree from the ground up — from "why isn't a binary search tree good enough?" — until indexing stops being a checklist item and becomes something you can reason about from first principles.

## Why It Matters (Latency, Throughput, Cost)

**The O(log N) vs O(N) cliff is a real cliff with real numbers.** Sequentially scanning a one-million-row table off NVMe — reading 4 KB pages — lands around 250 ms. The same lookup through an index: about 0.5 ms. That's **500×**. Now grow the table to 10M rows: the scan becomes 2.5 *seconds*; the index lookup is still ~0.5 ms, because `log₂(10M) ≈ 23` and `log₂(1M) ≈ 20` — three more steps, not ten times more. This is the whole magic of logarithmic structures: the index's advantage *widens* as your data grows. Which means deferring indexing with "we'll add it when we need it" is a bet that your data won't grow — and if your data isn't growing, why are you worried about performance at all? It's a bet you lose either way.

**Over-indexing shows up as write amplification you can't see until you measure it.** InnoDB touches *every* secondary index on *every* row mutation. With k secondary indexes, one `UPDATE` to an indexed column becomes k+1 writes plus k B-tree rebalances. At high write throughput this isn't abstract — it saturates your disk I/O *before the table itself is the bottleneck*, and your dashboards point at "disk busy" while the real culprit is three indexes nobody queries. `SHOW ENGINE INNODB STATUS` will show you the rebalancing churn. The fix is unglamorous: drop the indexes that aren't earning their keep.

**The planner choosing wrong is its own outage class.** The planner decides "index scan or sequential scan?" using *sampled, approximate* statistics about how many rows your predicate will match. When those statistics are stale — after a bulk load, on a skewed column, when `ANALYZE` hasn't run — its estimate can be off by 100×, and it makes the *structurally* wrong choice. A query that should take 2 ms via the index takes 8 seconds via a full scan, and the index is *right there, unused.* This is why `EXPLAIN (ANALYZE, BUFFERS)` is the single most important diagnostic tool a backend engineer can be fluent in: it's the difference between "the index is missing" and "the index exists but the planner doesn't believe in it."

## Mental Model

Here's the unlock for this entire chapter: **every index structure is a frozen assumption about how you're going to ask questions.** Choose the structure by first naming the *shape* of your query, not the other way around. Six shapes cover almost everything:

- **Point lookup** — "give me the row where `id = 42`." A hash index is technically fastest, but a B-tree does it nearly as well *and* can do everything below, so B-trees win in practice.
- **Range scan** — "everything between these two timestamps." You need an *ordered* structure. A hash index physically cannot do this — its whole point was to destroy order. This single requirement is why databases default to B-trees, not hash tables.
- **Prefix scan** — "all names starting with `foo`." A trie, or a composite B-tree aligned to the prefix.
- **Membership test** — "does this key exist *at all*?" A Bloom filter answers in O(1) with a tunable false-positive rate and a fraction of the memory; a B-tree gives the exact answer.
- **Nearest-neighbor / spatial** — "closest points to here." An R-tree, or a space-filling curve (Z-order) folded into a B-tree.
- **Write-heavy, read-rarely** — logs, events, metrics. An LSM tree, which trades read effort for cheap sequential writes.

No structure wins all six. A system that uses only B-trees for everything has *silently made a product decision*: reads matter more than writes, and ranges matter more than pure membership. For most OLTP systems that's correct. For a metrics pipeline ingesting a million points a second, it's badly wrong — and recognizing *that* is the difference between a senior engineer and a staff one.

## Underlying Theory

We'll build the tree up the way you'd discover it if you kept asking "okay, but why doesn't the simpler thing work?" Each layer fixes a flaw in the one before.

### Layer 1 — Why a binary search tree isn't enough (the balance problem)

Start with the obvious idea: a **binary search tree.** Each node has a key; everything smaller goes left, everything bigger goes right. To find a key, you start at the root and walk down, halving the search space at each step. Beautiful — O(log N), the same logarithmic magic as binary search, but now it supports insertion without shifting an array.

Except it has a fatal flaw, and you can trigger it by accident. Insert keys *in sorted order* — `1, 2, 3, 4, 5...` — which happens constantly in real systems (auto-increment IDs, timestamps). Every new key is bigger than the last, so it always goes right. Your "tree" degenerates into this:

```
   1
    \
     2
      \
       3
        \
         4      ← a linked list wearing a tree costume. Search is O(N), not O(log N).
          \
           5
```

The O(log N) promise of a BST is *conditional on the tree staying balanced*, and nothing in the plain BST enforces that. So the entire history of tree data structures — AVL trees, red-black trees, B-trees — is fundamentally **one war against this degeneration.** Each is a different scheme for guaranteeing the tree stays bushy and shallow no matter what order keys arrive in. Red-black trees (which back `std::map`, Java's `TreeMap`, the Linux kernel's scheduler) do it by recoloring and rotating nodes to keep any root-to-leaf path within 2× of any other. They're the answer for *in-memory* balanced trees.

But databases don't use red-black trees, and the reason why is the most important idea in this whole chapter.

### Layer 2 — B-trees: make the nodes fat because the disk is far away

A balanced binary tree is great in memory. On disk, it's a disaster — and to see why, you have to remember the latency hierarchy from the arrays chapter. Each node you visit in a binary tree is potentially a *separate trip to storage*. A binary tree over a billion rows is ~30 levels deep, so a lookup is up to 30 random disk reads. At ~100 µs each on NVMe, that's 3 ms of pure I/O for *one* lookup. The problem isn't the comparisons — those are free. The problem is that **the tree is deep, and depth means trips.**

So ask the first-principles question: the disk hands you data in *pages* (4 KB, 8 KB, 16 KB) whether you want one byte or all of it — exactly like the cache line from the arrays chapter, just one level down the hierarchy. If a single read drags in 16 KB regardless, why are we storing one key per node? **Why not stuff hundreds of keys into each node, so one page-read makes hundreds of comparisons instead of one?**

That's the B-tree. A B-tree node isn't a single key with two children — it's a *page-sized* block holding hundreds of keys and hundreds-plus-one child pointers.

```
Binary tree node:          B-tree node (one 16 KB page):
   ┌─────┐                 ┌──────────────────────────────────────────────┐
   │ key │                 │ k1 k2 k3 k4 ... k511   (512 keys, 513 ptrs)   │
   └──┬─┬┘                 └──┬──┬──┬──┬───────────┬──┬───────────────────┘
      ▼ ▼                     ▼  ▼  ▼  ▼           ▼  ▼
   2 children              513 children — one disk read decides among 513 paths
```

The number of children per node is the **branching factor** (or order), and it's chosen *to match the page size*, not for mathematical tidiness. A 16 KB page holding 8-byte keys and child pointers gives a branching factor around 512. And here is the payoff that makes B-trees the foundation of every database on earth: with branching factor 512, a tree over a *billion* rows is only **three levels deep** (`512³ ≈ 134 million`, `512⁴ ≈ 68 billion`). Three. A billion-row point lookup is at most three page reads — root, internal, leaf — and the root and upper levels live permanently in the buffer pool, so it's really *one* read that touches disk.

Internalize the reframing: **B-tree performance is bounded by I/O, not computation.** Picking among 512 keys in a node is trivially fast; the cost is fetching the node. This is the same lesson as the arrays chapter — *the trip dominates, so minimize trips* — applied to indexing. It's also why the root page is the most precious page in your entire database: evict it from cache and you've added a disk read to *every query that touches the table.* A wide, shallow tree is a tree that respects the memory hierarchy.

### Layer 3 — B+ trees: put all the data in the leaves and link them

Real databases don't use plain B-trees; they use **B+ trees**, with one deceptively small change that unlocks two huge capabilities. In a B+ tree, the internal nodes hold *only keys and child pointers — no row data at all.* All the actual data lives in the **leaf** level, and the leaves are **linked together** like a doubly-linked list.

```
                    [ 50 | 100 ]                ← internal: just signposts
                   /     |      \
            [10|30]   [60|80]   [120|150]        ← internal
            /  |  \    ...
        leaves:  [rows 1..29] ⇄ [rows 30..49] ⇄ [rows 50..59] ⇄ ...   ← linked!
                   ▲ all data down here, leaves chained left-to-right in sorted order
```

Why does moving data out of internal nodes matter? Because now internal nodes hold *only* keys, so even more of them fit per page, so the branching factor goes *up*, so the tree gets even shallower. Free win.

But the linked leaf layer is the real prize, and it explains a pile of things that otherwise seem like database magic:

- **Range scans are cheap.** "All orders between March and June" → traverse O(log N) from root to the *first* matching leaf, then just *walk the leaf linked list* sideways, reading consecutive sorted leaves, until you pass the upper bound. No going back up through the tree, no re-traversal — O(log N + K) for K results, and the walk is *sequential* (arrays-chapter free lunch again). Hash indexes simply cannot do this; their order is destroyed by design.
- **`ORDER BY` on an indexed column is "free."** The index *is* already sorted — the leaves are in key order — so the planner reads them in order and skips the sort step entirely. When you see a query plan with no "Sort" node despite an `ORDER BY`, this is why.
- **Covering indexes can answer a query without touching the table.** If a secondary index's leaves already contain every column the query needs, the database never has to go fetch the actual row. More on this next.

### Layer 4 — Clustered vs. secondary indexes, and why your primary key choice is a performance decision

Here's a distinction that quietly determines half your database's write performance. A **clustered index** means *the table rows are physically stored, sorted, inside the index's own leaves.* The index leaves *are* the table — there's no separate row storage. InnoDB does this: every table has exactly one clustered index (your primary key), and the primary key's B+ tree leaves hold the full rows.

A **secondary index** is a separate B+ tree, sorted by some other column, whose leaves hold... not the row, but the *primary key* of the row. So in InnoDB, a lookup by a secondary index is a **two-tree journey**: walk the secondary index to find the primary key, then walk the *clustered* index to fetch the actual row. Double the traversals, double the I/O. This is why a **covering index** — one that includes every column the query needs right in its own leaves (`CREATE INDEX ... INCLUDE (...)` in Postgres) — is one of the highest-leverage optimizations there is: it eliminates the second journey entirely. `EXPLAIN` rewards you with "Index Only Scan" / "Using index," and the heap pages never get touched.

Now the part that surprises people: **your choice of primary key is a write-performance decision, because of how the clustered tree grows.** Watch what happens with two different key strategies as rows arrive:

```
Monotonic key (auto-increment, ULID, UUIDv7):    Random key (UUIDv4):
  every new row > all existing keys                each new row lands at a random leaf
  → always appends to the RIGHTMOST leaf           → splits arbitrary leaves all over the tree
  → sequential writes, pages fill ~100%            → random writes, page splits everywhere,
  → minimal fragmentation                            half-empty pages, cache thrash
       [...][...][...][NEW]  ◄── tidy                  [.N.][...][N..][...][..N]  ◄── chaos
```

A random UUIDv4 primary key forces *random insertions* across the entire clustered B+ tree. Each insert may land in a full leaf, forcing a **page split** (allocate a new page, move half the keys over) — and because the inserts are scattered, you're splitting pages everywhere, fragmenting the tree, leaving pages half-full, and blowing out your buffer-pool cache because every insert touches a different region. A monotonically increasing key (auto-increment, ULID, timestamp-prefixed, UUIDv7) always appends to the rightmost leaf: sequential writes, tightly packed pages, almost no splits. This is why "just use a random UUID for the primary key" is one of the most common and most expensive schema mistakes in the field. Use UUIDv7 or ULIDs if you want UUID's properties without torching your write path.

### Layer 5 — Composite indexes and the leftmost-prefix rule (it's just sort order)

People memorize the "leftmost prefix rule" as a database quirk. It's not a quirk — it falls straight out of what a sorted tree *is*, and once you see it you'll never need to memorize it again.

A composite index on `(a, b, c)` sorts rows by `a` first; *within* equal `a` values, by `b`; *within* equal `(a, b)`, by `c`. Picture a phone book sorted by (last name, first name). You can instantly find "everyone named Smith" and "every Smith named John" — but you *cannot* efficiently find "everyone named John," because the Johns are scattered across every last name. The phone book's sort order only helps you if you constrain from the *left*.

```
Index (a, b, c) is sorted like this:
   a=1,b=1,c=1
   a=1,b=1,c=2     query on (a)      → ✓ yes — a is the leftmost sort key
   a=1,b=2,c=1     query on (a,b)    → ✓ yes
   a=2,b=1,c=1     query on (b) only → ✗ no — b values are scattered across every a
   a=2,b=3,c=1     query on (b,c)    → ✗ no
```

From that one fact, the real-world rules drop out:
1. **Equality columns before range columns.** `WHERE a = ? AND b > ?` wants index `(a, b)`: the tree jumps to the `a` partition, then the `b` range is one contiguous sweep within it.
2. **A range predicate is a dead end for everything after it.** With `(a, b, c)` and `WHERE a = ? AND b BETWEEN ? AND ? AND c = ?`, the index uses `a` and `b` — but *not* `c`, because once you're sweeping a range of `b`, the `c` values within that range aren't globally ordered (same reason the Johns are scattered). Put the columns you do equality on first, the one range last.
3. **When all predicates are equality, lead with the most selective column** to prune the most rows at the first step.

This is why composite-index column *order* is a correctness-adjacent design decision, not a cosmetic preference — the same three columns in a different order serve a completely different set of queries.

### Layer 6 — LSM trees: when writes are the thing you're optimizing

Everything so far optimizes reads and pays a write tax: B-tree inserts are *random* writes into arbitrary tree nodes, causing random I/O, page splits, and rebalancing. For a metrics pipeline, an event log, a message queue — workloads that are 95% writes — that trade-off is exactly backwards. So we invert it.

The **Log-Structured Merge tree (LSM tree)** is built on one observation: *sequential writes are orders of magnitude faster than random writes*, on both SSD and disk (arrays-chapter free lunch, the storage-level version). So an LSM tree refuses to do random writes at all:

```
            writes (fast, in-memory, sorted)
               │
               ▼
        ┌─────────────┐  memtable  (a skip list / RB-tree in RAM)
        └──────┬──────┘
               │ when full, flush sequentially to disk as an immutable sorted file
               ▼
   SSTable L0   SSTable L0   ...        ← newest, small
        └────────┬───────────┐ compaction merges & sorts in the background
                 ▼           ▼
   ───── SSTable L1 ───────────────     ← older, bigger, fewer
   ──────── SSTable L2 ──────────────   ← oldest
```

Writes land in an in-memory **memtable** (a sorted structure — skip list or red-black tree), which is a cheap O(log N) in-RAM insert. When it fills, it's flushed to disk *sequentially* as an immutable sorted file called an **SSTable** (Sorted String Table). Reads check the memtable, then newer SSTables, then older ones. In the background, **compaction** merges SSTables together, keeping them sorted and discarding overwritten/deleted keys.

The trade-offs, stated honestly:

| Property | B-tree | LSM tree |
|---|---|---|
| Write | O(log N), random I/O | O(1) amortized, *sequential* I/O |
| Write amplification | Low (update in place) | **High** (compaction rewrites data 10–30×) |
| Read | O(log N), one tree | O(log N) × number of levels |
| Range scan | Excellent (linked leaves) | Good (SSTables are sorted) |
| Space | Low | Moderate (stale data until compacted) |

The obvious worry — "reads have to check many SSTables, isn't that slow?" — is solved by a friend from the hash-tables neighborhood: a **Bloom filter** on each SSTable. Before reading a file to look for a key, ask its Bloom filter "could this key be in here?" If it says no (and it's never wrong about no), skip the whole file. This collapses LSM reads to typically 1–2 actual file reads even across many levels, for ~10 bits of memory per key. Everything that needs to swallow writes fast is an LSM tree: RocksDB (and its children TiKV, CockroachDB, MyRocks, Pebble), Cassandra, ScyllaDB, LevelDB, InfluxDB. When you pick Cassandra over Postgres for a write-firehose workload, *this* is the structural reason.

### Layer 7 — Skip lists: ordered structure that loves concurrency

There's one more ordered structure worth knowing, because it shows up inside Redis and Java's concurrent collections for a specific reason: the **skip list.** Take a sorted linked list and add "express lanes" — extra layers of forward pointers that skip over half the nodes, then half of those, and so on. Search starts in the top express lane, dropping down a level whenever the next node would overshoot. With each lane skipping ~half the nodes, search is O(log N) — same as a balanced tree, but achieved through *randomization* instead of rotations.

```
L3: ●─────────────────────────────────────────► (skips ~8)
L2: ●───────────────►●───────────────────────► (skips ~4)
L1: ●───────►●───────►●───────►●─────────────► (skips ~2)
L0: ●─►●─►●─►●─►●─►●─►●─►●─►●─►●─►●─►●─►●─►●─►  (all nodes, sorted)
```

So why bother, when B-trees and red-black trees hit the same O(log N)? **Concurrency.** A balanced tree rebalances on insert — rotations that restructure multiple nodes at once — which forces coarse locking and a global coordination point under contention. A skip list inserts by splicing a node into a few linked lists, and that splice can be done with atomic compare-and-swap on individual pointers — enabling **lock-free** concurrent implementations. That's exactly why Java's `ConcurrentSkipListMap` exists, and why **Redis sorted sets** (`ZADD`/`ZRANGE`/`ZRANK`) are backed by a skip list: it gives O(log N) ranked operations with simple, fast concurrent updates. The cost is O(N log N) expected memory for all those express-lane pointers — fine for in-memory structures where concurrency beats memory, wrong for on-disk where page-aligned B-trees win.

### Layer 8 — Tries: when the key has structure you can walk

Every structure so far compares whole keys. A **trie** (prefix tree) does something different: it stores keys *character by character*, branching on each symbol, with shared prefixes collapsed into shared paths. Lookup is O(L) where L is the key *length* — and crucially, **independent of N**, the number of keys. For long keys over a huge key set, walking L characters beats walking log N nodes.

```
Trie storing  "go", "gone", "good", "cat":

        (root)
        /    \
       c      g
       |      |
       a      o ──────► "go" ✓
       |     / \
       t    n   o
       ✓    |   |
            e   d
            ✓   ✓
   "good", "gone", "go" all share the "go" path — prefixes stored once
```

The trie's superpower is that **all keys sharing a prefix live under one node** — so prefix queries are O(prefix length), not a scan. That makes tries the natural fit for:

- **IP routing / longest-prefix match.** A 32-bit IPv4 address is a path in a binary trie; routers find the longest matching prefix to pick the next hop. Hardware (TCAM) does it in one clock; software uses compressed tries.
- **Autocomplete and prefix search.** `WHERE name LIKE 'foo%'` is a prefix walk in a trie — O(prefix), versus a range gymnastics in a B-tree. (Postgres's `pg_trgm` generalizes this to arbitrary substrings via trigram indexes.)
- **DNS.** The whole DNS hierarchy *is* a distributed trie, resolved right-to-left: `.com` → `example.com` → `api.example.com`, each label delegated to a different server.

A **radix tree** (compressed trie) merges any single-child chain into one edge, cutting memory dramatically. The Linux kernel's routing table is a radix tree; Go's `net/http` router (since 1.22) uses one for O(L) route matching. When you need to index by *structured* keys and ask *prefix* questions, a trie is the structure the B-tree can only awkwardly imitate.

> **A note on graph algorithms:** earlier versions of this chapter also covered Dijkstra, topological sort, and BFS/DFS. Those belong with their family — see **[04-graphs-and-network-algorithms.md](04-graphs-and-network-algorithms.md)**, which treats routing, dependency resolution, and reachability in full. Trees are a special case of graphs (acyclic, connected, N−1 edges); the indexing structures above are where trees earn their own chapter.

## A Ladder From L1 to Principal

- **L1 / new grad:** A balanced tree gives O(log N) lookup/insert/range; an index makes `WHERE`/`ORDER BY` fast; too many indexes slow down writes. You add an index when a query filters on a column.
- **L3–L4 / solid engineer:** You know *why* databases use B+ trees (shallow = few disk trips), what clustered vs. secondary indexes cost, and the leftmost-prefix rule. You read `EXPLAIN` and recognize a missing index vs. an unused one.
- **Senior:** You design composite indexes deliberately, choose primary keys for write locality (monotonic vs. random), build covering indexes for hot read paths, and know when a sequential scan is genuinely the right plan.
- **Staff:** You choose the *storage engine* by workload — B-tree vs. LSM — understand write/read/space amplification trade-offs, diagnose planner cardinality errors, and reason about index bloat, page splits, and buffer-pool pressure as system properties.
- **Principal:** You pick (or build) the right structure for each access pattern across the system — B+ trees for OLTP ranges, LSM for write firehoses, tries for prefix/routing, skip lists for concurrent in-memory ordering, Bloom filters to skip I/O — and you treat "what's our dominant query shape?" as the question that drives storage architecture. The latency hierarchy and the I/O-bound nature of trees are how you think, not facts you look up.

The whole ladder is one idea climbing: *find data without looking at all of it, by keeping it in a shape that matches how you'll ask.*

## Complexity Analysis

| Structure | Point Lookup | Insert | Range Scan | Delete | Space |
|---|---|---|---|---|---|
| Unsorted array | O(N) | O(1) amortized | O(N) | O(N) | O(N) |
| Sorted array | O(log N) | O(N) | O(log N + K) | O(N) | O(N) |
| Hash table | O(1) avg | O(1) avg | **O(N)** — no order | O(1) avg | O(N) |
| B+ tree | O(log N) | O(log N) | O(log N + K) | O(log N) | O(N) |
| Skip list | O(log N) | O(log N) | O(log N + K) | O(log N) | O(N log N) |
| LSM tree | O(log N) × levels | O(1) amortized | O(log N + K) | tombstone O(1) | O(N) + amplification |
| Trie | O(L) | O(L) | O(L + K) | O(L) | O(N × Σ) |
| Bloom filter | O(k), false positives | O(k) | — | — | O(N) bits |

K = results returned, L = key length, k = hash functions, Σ = alphabet size. The LSM insert is O(1) *amortized* because compaction is deferred background work — individual writes are cheap, but *total* I/O is higher (RocksDB write amplification is typically 10–30×). The table tells you complexity; the constant factor is set by where the structure lives in the memory hierarchy — re-read the arrays chapter's latency table next to this one.

## War Stories (the shape of the bug in the wild)

- **"It was fast in staging."** A 50M-row table, a query filtering on an unindexed column, a 2.5-second full scan that was 12 ms on staging's 10K rows. The bug wasn't slowness — it was an O(N) query that staging's tiny data hid. Add the index, done.
- **The UUID that wrecked write throughput.** A table keyed on UUIDv4 saw insert latency climb as it grew — random clustered-index insertions, constant page splits, half-empty pages, cache thrash. Switching to UUIDv7 (time-ordered) restored sequential appends and recovered the write path without changing a line of application logic.
- **The index the planner refused to use.** A query suddenly went from 2 ms to 8 seconds after a bulk import. The index was fine; the *statistics* were stale, so the planner mis-estimated selectivity and chose a sequential scan. `ANALYZE` fixed it in seconds. Lesson: an unused index and a missing index look identical in latency and opposite in `EXPLAIN`.
- **Eleven indexes, two used.** A write-heavy table had accumulated indexes over years of "just in case." Inserts were doing 12× the necessary writes. Dropping the nine unused indexes roughly halved write latency. Indexes are not free; they're a standing tax on every write.

## Key Takeaways

1. **A query without the right index isn't slow, it's O(N)** — a complexity class you cannot buy your way out of. The index changes the *shape* of the search to O(log N), and that advantage widens as data grows.
2. **B-trees are fat and shallow on purpose.** The branching factor is matched to the page size so a billion rows fit in ~3 levels — because performance is bounded by *trips to storage*, not comparisons. This is the arrays-chapter "minimize trips" lesson, one level down the hierarchy.
3. **B+ trees put all data in linked leaves**, which is *why* range scans, free `ORDER BY`, and covering (index-only) scans work. Internal nodes are just signposts.
4. **Your primary key is a write-performance decision.** Monotonic keys (auto-increment, ULID, UUIDv7) append sequentially; random keys (UUIDv4) cause random insertions, page splits, and fragmentation across the clustered tree.
5. **The leftmost-prefix rule is just sort order.** A composite index `(a,b,c)` helps queries constrained from the left; put equality columns first and the single range column last, because a range predicate ends the index's usefulness for everything after it.
6. **LSM trees invert the B-tree trade-off** to make writes sequential and cheap, paying in read and write *amplification* (mitigated by per-SSTable Bloom filters). Choose LSM for write-firehose workloads, B-tree for read- and range-heavy ones — this is the real reason behind "Cassandra vs. Postgres."
7. **Skip lists buy easy concurrency** (lock-free via CAS, no rotations) at the cost of memory — which is why Redis sorted sets and Java's `ConcurrentSkipListMap` use them. **Tries** index *structured* keys with O(L) prefix queries — the backbone of IP routing, autocomplete, and DNS.
8. **`EXPLAIN (ANALYZE, BUFFERS)` is the instrument that tells you which world you're in** — missing index, unused index, stale stats, or a genuinely correct sequential scan. Learn to read it before you add another index.

## Related Modules

- `01-arrays-and-memory-layout.md` — the page/cache-line "minimize trips" argument that makes wide B-tree nodes fast, and the sequential-vs-random write distinction behind LSM trees
- `02-hash-tables.md` — the other great lookup structure: O(1) point lookups but **no ordering**; this chapter is why you'd pick a B-tree (ranges) over a hash index, and Bloom filters reappear here to skip LSM I/O
- `04-graphs-and-network-algorithms.md` — trees are acyclic connected graphs; Dijkstra, topological sort, and BFS/DFS live here
- `05-sorting-and-searching.md` — B+ tree leaves are sorted order made persistent; external merge sort and the Eytzinger layout share the cache/I-O reasoning
- `../06-databases/02-indexing.md` — InnoDB/PostgreSQL B+ tree internals, covering indexes, index-only scans, VACUUM and index bloat in production
- `../09-performance-engineering/02-latency-analysis.md` — using `EXPLAIN ANALYZE` to diagnose planner cardinality errors and missed-index opportunities
