# Module 02 — Data Structures and Algorithms

> Not "data structures" the way a coding-interview prep course means it. This module is about the data structures and algorithms *as the backend actually runs them* — on real hardware, with a memory hierarchy, under load, sometimes against an adversary. The thread running through every chapter is the gap between what the asymptotics promise and what the machine does, and how to close it.

## How these chapters are built

Each chapter is written to take you from L1 (new grad) to principal-engineer depth on one topic, building everything from first principles. They share a deliberate anatomy:

- **Problem** — the specific misunderstanding this chapter destroys, usually with a number you can reproduce.
- **Why It Matters** — latency, throughput, and cost consequences in real systems (databases, caches, the cloud bill).
- **Mental Model** — the intuition, built visually, that the rest of the chapter hangs on.
- **Underlying Theory** — layered from the simplest idea to the systems-internals depth, each layer fixing a flaw in the last.
- **A Ladder From L1 to Principal** — the same topic at five altitudes, so you can see where you are and where you're going.
- **Complexity Analysis**, **War Stories**, **Key Takeaways**, **Related Modules**.

## Contents

1. **[Arrays and Memory Layout](01-arrays-and-memory-layout.md)** — why "O(1) access" is the address, not the fetch; cache lines, the latency hierarchy, the prefetcher, false sharing, AoS vs. SoA, and why every fast storage system is an array exploiting sequential access. *The foundation the other four chapters stand on.*
2. **[Hash Tables](02-hash-tables.md)** — direct addressing made affordable; why collisions are inevitable (birthday paradox), open addressing vs. chaining as a cache decision, load factor as the master dial, hash-flooding as a DoS surface, and consistent hashing at datacenter scale.
3. **[Trees and Indexing](03-trees-and-indexing.md)** — why a missing index isn't slow but *O(N)*; B-trees made fat-and-shallow to minimize disk trips, B+ tree linked leaves, clustered vs. secondary indexes, the leftmost-prefix rule, LSM trees, skip lists, and tries.
4. **[Graphs and Network Algorithms](04-graphs-and-network-algorithms.md)** — the graphs hiding inside your backend (deadlocks, dependencies, routing, blast radius); BFS/DFS, Dijkstra and its silent failure mode, Bellman-Ford, SCCs, Union-Find/MST, and max-flow/min-cut.
5. **[Sorting and Searching](05-sorting-and-searching.md)** — the comparison wall and the door through it, quicksort-as-DoS, Timsort's bet on real data, the binary-search bug everyone ships, cache-aware search, order statistics, and external merge sort.

## Cross-Module Links

Concepts here are applied in:
- `../06-databases/` — indexing, query planning, joins, and on-disk storage are these structures in production
- `../07-core-backend-engineering/` — practical patterns built on these foundations
- `../09-performance-engineering/` — the measurement frameworks that turn this theory into observed wins
- `../10-production-systems/` — operational application under real load

## Learning Order

Read in numerical order — each chapter leans on the one before, and chapter 01 (memory layout) is the lens for all the rest.
