# 04-graphs-and-network-algorithms

## Problem

Understanding the data structures that underpin database indexes, network routing, and query planning is essential for diagnosing performance problems at the systems level.

## Why It Matters (Latency, Throughput, Cost)

The choice of data structure determines query complexity. B-tree indexes reduce O(N) table scans to O(log N) index lookups — a 1000× improvement for N=1M rows.

## Mental Model

Data structures encode different assumptions about access patterns. Choose based on your query type (point lookup, range scan, membership test, traversal).

## Underlying Theory

This module connects directly to: database indexing (06), query planning (06), and systems design (08).

## Complexity Analysis

| Structure | Lookup | Insert | Range | Memory |
|-----------|--------|--------|-------|--------|
| Array | O(N) | O(N) | O(N) | O(N) |
| Hash table | O(1) avg | O(1) | O(N) | O(N) |
| B-tree | O(log N) | O(log N) | O(log N + K) | O(N) |
| Skip list | O(log N) | O(log N) | O(log N + K) | O(N log N) |

## Key Takeaways

1. B-trees are why database indexes have O(log N) lookup.
2. Hash indexes are O(1) for point lookups but cannot do range queries.
3. Graph algorithms (Dijkstra, BFS) underlie network routing and dependency resolution.
4. Sorting enables binary search: O(N log N) sort once → O(log N) repeated lookups.

## Related Modules

- `../../06-databases/02-indexing.md` — B-tree applied to database indexes
- `../../04-computer-networks/04-dns-and-load-balancing.md` — graph traversal in routing
