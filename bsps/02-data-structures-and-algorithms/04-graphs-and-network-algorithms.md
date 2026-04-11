# 04-graphs-and-network-algorithms

## Problem

Graph algorithms fail silently in backend systems. Not with exceptions — with correct-looking wrong answers, with latency that climbs for no apparent reason, with cascading failures that the incident report attributes to "a network issue" rather than to the Bellman-Ford implementation that accepted a negative cycle and diverged.

The graph is one of the most universal abstractions in computing. A microservice topology is a directed graph. A database transaction dependency graph determines whether deadlock is possible. A CDN's routing table is a shortest-path problem over a weighted graph with time-varying edge weights. A Kubernetes scheduler is solving a constrained graph matching problem. Service mesh circuit breakers implement reachability queries on a dynamic graph that changes with every pod restart.

The engineers who diagnose these systems at the staff level aren't running `ping` and `traceroute` and guessing. They are reasoning from the graph structure: what algorithm governs this behavior, what are its invariants, and which invariant has been violated? This module gives you that vocabulary.

---

## Why It Matters (Latency, Throughput, Cost)

**Routing convergence time is a graph algorithm property.** When a link fails in an OSPF network, every router runs Dijkstra on its local copy of the topology. The time to convergence — from link failure to stable new routing state — is bounded by the time to flood the link-state advertisement (LSA) to all routers and the time to recompute shortest paths. Dijkstra on a graph with V routers and E links takes O((V + E) log V). For a large datacenter network with thousands of routers and tens of thousands of links, this is measurable in milliseconds to seconds. During convergence, some routers have the new topology and some have the old — they will route packets in contradictory directions, causing transient black holes. Understanding convergence time means understanding why your service has a 1-3 second latency spike after every network topology change, and why graceful restarts (OSPF GR, BGP GR) exist to suppress this.

**Deadlock detection is a cycle detection problem.** A database deadlock is a cycle in the transaction wait-for graph: transaction T1 waits for a lock held by T2, T2 waits for a lock held by T3, T3 waits for a lock held by T1. Detecting this cycle and aborting one transaction is the only resolution. InnoDB runs deadlock detection continuously — it maintains a wait-for graph and checks for cycles on every lock request that causes a wait. The algorithm is DFS-based cycle detection, O(V + E) in the size of the wait-for graph. The cost of this detection is why `innodb_deadlock_detect=OFF` is a legitimate performance optimization for high-concurrency OLTP workloads where deadlocks are rare but lock contention is high — you trade correctness for throughput and rely on `innodb_lock_wait_timeout` as a fallback.

**Service dependency graphs determine blast radius.** When service A depends on B, B depends on C, and C depends on D, a failure in D has a blast radius determined by the transitive closure of D in the dependency graph. The transitive closure tells you every service that will be affected — not just direct dependents but all ancestors in the dependency DAG. Computing the transitive closure naively is O(V × (V + E)), which is expensive for large service meshes, but sparse-graph approximations and incremental updates make it tractable. Teams that have this computed know the blast radius before deployment. Teams that don't find out during the incident.

**Maximum flow equals minimum cut — and minimum cut is your redundancy measure.** The max-flow min-cut theorem (Ford-Fulkerson, 1956) states that the maximum amount of flow from a source to a sink in a network equals the minimum capacity of any cut separating source from sink. In a datacenter network, the max-flow from your application servers to your storage tier is bounded by the minimum-capacity link set that, if removed, would disconnect them. This minimum cut is your single point of failure at scale. Network architects use max-flow analysis to find these bottlenecks before traffic does.

---

## Mental Model

Model the problem as a graph, identify the query type, apply the correct algorithm, verify its invariants hold for your specific graph.

The query types:
- **Reachability**: can I get from A to B? — BFS or DFS, O(V + E)
- **Shortest path, single source, non-negative weights**: Dijkstra, O((V + E) log V)
- **Shortest path, single source, negative weights, no negative cycles**: Bellman-Ford, O(VE)
- **Shortest path, all pairs**: Floyd-Warshall, O(V³)
- **Minimum spanning tree**: Prim or Kruskal, O(E log V)
- **Maximum flow**: Dinic's algorithm, O(V² E)
- **Topological ordering**: Kahn's (BFS-based) or DFS-based, O(V + E)
- **Strongly connected components**: Tarjan's or Kosaraju's, O(V + E)
- **Cycle detection**: DFS with coloring, O(V + E)

The critical discipline: verify the algorithm's preconditions before applying it. Dijkstra's correctness proof requires non-negative edge weights. Apply it to a graph with negative weights and it produces wrong answers — not errors, not exceptions, wrong answers that look plausible. This is the failure mode that doesn't announce itself.

---

## Underlying Theory

### Graph Representations and the Hidden Complexity of Your Choice

Before any algorithm, the representation choice determines the constant factors on every operation.

**Adjacency matrix:** V×V matrix where `M[u][v]` = edge weight (0 or ∞ if absent). Edge existence check: O(1). Neighbor enumeration: O(V) regardless of actual degree. Memory: O(V²). Correct for dense graphs (E ≈ V²); catastrophically wasteful for sparse graphs. Most real-world graphs (road networks, service dependencies, social graphs, internet topology) are sparse — average degree much less than V. An adjacency matrix for a 10,000-node sparse graph uses 800MB (10⁸ × 8 bytes) to store a graph that has fewer than 100,000 edges.

**Adjacency list:** Array of V lists, each containing the neighbors of vertex v. Edge existence check: O(degree(v)). Neighbor enumeration: O(degree(v)). Memory: O(V + E). Correct for sparse graphs. The standard representation for any real-world graph.

**Compressed Sparse Row (CSR):** Two arrays: `offsets[V+1]` where `offsets[v]` is the starting index of v's neighbors, and `neighbors[E]` containing all edges. The neighbors of v are `neighbors[offsets[v]..offsets[v+1]]`. Memory: O(V + E). No pointer chasing — cache-friendly linear scans. This is how production graph processing systems (GraphX, DGL, NetworkX's sparse backend) store graphs. For a graph traversal that visits most edges, CSR is 3-5× faster than adjacency lists due to cache locality.

The choice between these is not academic. The difference between O(V) and O(degree(v)) neighbor enumeration determines whether your Dijkstra implementation on a sparse graph is O((V + E) log V) or O(V² log V) — which, for a 100,000-node graph with average degree 10, is the difference between 10⁶ and 10¹⁰ operations.

### BFS and DFS: Primitives, Not Algorithms

BFS and DFS are not algorithms — they are traversal strategies that instantiate into specific algorithms depending on what you compute during traversal.

**BFS on an unweighted graph computes shortest paths.** The BFS frontier at distance d contains exactly the vertices at hop-count d from the source. When you first visit a vertex, you have found its shortest path. This is not a coincidence — it is the structural property of BFS: a FIFO queue processes vertices in non-decreasing distance order, so the first time you reach a vertex is via the shortest route.

Applications: minimum hop routing, social network degree separation, level-synchronous graph processing (used in Pregel and its descendants).

**DFS computes structural properties.** The DFS tree — the tree of edges traversed during DFS — encodes the graph's structure in its edge classifications:
- **Tree edges**: edges in the DFS tree
- **Back edges**: edges from a vertex to its ancestor in the DFS tree — their presence indicates a cycle in a directed graph
- **Forward edges**: edges from ancestor to non-child descendant (directed graphs only)
- **Cross edges**: all other edges

Cycle detection: a directed graph is acyclic (a DAG) if and only if DFS produces no back edges. This is the canonical linear-time DAG detection algorithm. Topological sort: reverse postorder of DFS on a DAG gives a valid topological ordering.

**The three-color DFS for directed cycle detection:**

```python
WHITE, GRAY, BLACK = 0, 1, 2

def has_cycle(graph, V):
    color = [WHITE] * V

    def dfs(u):
        color[u] = GRAY
        for v in graph[u]:
            if color[v] == GRAY:   # back edge found — cycle exists
                return True
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    return any(dfs(u) for u in range(V) if color[u] == WHITE)
```

GRAY means "currently on the DFS stack." A GRAY neighbor means we've found an edge back to an ancestor — a cycle. This is what InnoDB's deadlock detector implements, with transactions as vertices and lock waits as edges.

### Dijkstra: Correctness Proof, Failure Modes, and Production Variants

Dijkstra's algorithm maintains a set S of vertices whose shortest distance from source s is finalized, and a priority queue Q of vertices with tentative distances.

```python

dist[s] = 0; dist[v] = ∞ for all v ≠ s
Q = min-heap containing all vertices, keyed by dist
while Q not empty:
u = extract-min(Q)
for each neighbor v of u with edge weight w(u,v):
if dist[u] + w(u,v) < dist[v]:
dist[v] = dist[u] + w(u,v)
decrease-key(Q, v, dist[v])

```

**Why it requires non-negative weights.** The correctness proof relies on the invariant: when u is extracted from Q, `dist[u]` is final. This holds because any remaining path to u would go through a vertex in Q with distance ≥ dist[u] (by the min-heap invariant) plus a non-negative edge — so it cannot improve dist[u]. A negative edge breaks this: a vertex extracted "early" might later be improved via a negative edge from a later-extracted vertex. Dijkstra on a graph with negative edges produces results that are wrong in a way that is indistinguishable from correct results without knowing the true shortest paths.

**Implementation complexity depends on the priority queue:**
- Binary heap: O((V + E) log V) — standard implementation, correct for sparse graphs
- Fibonacci heap: O(E + V log V) amortized — theoretically optimal, constant-factor overhead makes it slower in practice for most real graphs
- Dial's algorithm (bucket queue): O(E + V × W) where W is the max edge weight — optimal for integer weights, used in network simulators

**Bidirectional Dijkstra:** Run two simultaneous Dijkstra searches — one from source s, one from destination t — and terminate when they meet. In the best case this halves the number of vertices processed. Used in Google Maps and similar systems where single-source single-destination is the dominant query pattern. A-star (A*) extends this with a heuristic: instead of `dist[u]`, the priority key is `dist[u] + h(u, t)` where `h` is an admissible heuristic (never overestimates true distance). For road networks, Euclidean distance is admissible and reduces the search space dramatically.

**Contraction Hierarchies (CH):** The state of the art for road network routing. Preprocess the graph by contracting vertices in order of "importance" — when a vertex is contracted, add shortcut edges between its neighbors to preserve shortest path distances. The result is a hierarchical graph where queries run bidirectional Dijkstra only on the contracted hierarchy — milliseconds for continental-scale graphs with hundreds of millions of nodes. OSRM (Open Source Routing Machine) uses CH. Valhalla uses time-dependent CH. This is why Google Maps computes routes in under 100ms for cross-country trips.

### Bellman-Ford: Negative Weights and Negative Cycle Detection

Bellman-Ford relaxes all edges V-1 times. After iteration k, `dist[v]` is the shortest path from source using at most k edges. V-1 iterations suffice for any simple path (which has at most V-1 edges). A Vth iteration that finds further improvements indicates a negative cycle.

```python
def bellman_ford(graph, V, E, source):
    dist = [float('inf')] * V
    dist[source] = 0

    for _ in range(V - 1):
        for u, v, w in E:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w

    # Negative cycle detection
    for u, v, w in E:
        if dist[u] + w < dist[v]:
            return None  # negative cycle reachable from source

    return dist
```

O(VE) time — slower than Dijkstra but handles negative weights and detects negative cycles.

**Where this matters in production:** BGP does not use Dijkstra — it uses a path-vector protocol where loop prevention is the primary constraint, not shortest path. RIP (an older routing protocol) uses Bellman-Ford distributed across routers, with the "count to infinity" problem (slow convergence after link failure) being a direct consequence of distributed Bellman-Ford's convergence properties. Understanding why BGP route selection has the convergence characteristics it does requires understanding Bellman-Ford.

### Strongly Connected Components: Microservice Circular Dependencies

A strongly connected component (SCC) of a directed graph is a maximal set of vertices such that every vertex is reachable from every other vertex. In an acyclic graph (a DAG), every SCC has size 1. An SCC of size > 1 indicates a cycle.

**Tarjan's algorithm** computes all SCCs in O(V + E) using a single DFS. It assigns each vertex a `lowlink` — the smallest DFS discovery time reachable from the vertex's subtree via at most one back edge. When a vertex's `lowlink` equals its own discovery time, it is the root of an SCC; pop the stack to recover the component.

```python
def tarjan_scc(graph, V):
    index_counter = [0]
    stack = []
    lowlink = [0] * V
    index = [0] * V
    on_stack = [False] * V
    index_set = [False] * V
    sccs = []

    def strongconnect(v):
        index[v] = lowlink[v] = index_counter[0]
        index_counter[0] += 1
        index_set[v] = True
        stack.append(v)
        on_stack[v] = True

        for w in graph[v]:
            if not index_set[w]:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif on_stack[w]:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                scc.append(w)
                if w == v:
                    break
            sccs.append(scc)

    for v in range(V):
        if not index_set[v]:
            strongconnect(v)

    return sccs
```

**Production applications:**

- **Circular dependency detection** in service registries, package managers, and build systems. If `pip install` hangs resolving dependencies, it may be computing SCCs. An SCC of size > 1 in a dependency graph is a circular dependency — unresolvable without breaking a cycle.
- **Dead code elimination** in compilers: compute the call graph's SCCs, mark the SCC containing `main` as reachable, prune all unreachable SCCs.
- **Database transaction scheduling**: if the transaction dependency graph has an SCC of size > 1, there is a cycle of mutual dependencies — a potential deadlock. SCCs of size 1 with no self-loops are deadlock-free.

### Maximum Flow and Minimum Cut: Network Capacity Planning

Given a directed graph where each edge has a capacity `c(u, v)`, find the maximum flow from source s to sink t subject to:
- Flow conservation: inflow = outflow at every non-source, non-sink vertex
- Capacity constraint: flow on each edge ≤ capacity

**Ford-Fulkerson method:** Repeatedly find an augmenting path (a path from s to t with available capacity) in the residual graph and push flow along it. Terminates when no augmenting path exists. The residual graph includes "back edges" representing the ability to cancel previously routed flow — this is the key insight that makes Ford-Fulkerson correct.

**Dinic's algorithm:** O(V² E), significantly faster than Ford-Fulkerson (O(VE × max_flow)) for general graphs. Uses BFS to build a level graph, then finds all blocking flows in the level graph via DFS. For unit-capacity graphs (edges have capacity 0 or 1), Dinic runs in O(E √V).

**The max-flow min-cut theorem:** The maximum flow from s to t equals the minimum capacity of any s-t cut. An s-t cut partitions vertices into two sets S (containing s) and T (containing t); its capacity is the sum of capacities of edges from S to T. The theorem equates two seemingly different optimization problems — one about flow, one about edge removal.

**Backend systems applications:**

- **Network capacity planning:** The max flow from application tier to database tier tells you the throughput ceiling before any additional bandwidth investment. The min cut tells you which links to upgrade first.
- **Load balancing as max flow:** Model servers as vertices with capacity equal to their max RPS; model client groups as sources; model the load balancer as a flow network. Maximum flow gives the optimal routing assignment. This is too expensive to compute in real time but useful for offline planning.
- **Task scheduling on heterogeneous workers:** Model tasks as sources, worker types as sinks, and compatibility as edges. Maximum bipartite matching (a special case of max flow) gives the optimal task-to-worker assignment.

### Minimum Spanning Trees: Cluster Formation and Network Topology

A minimum spanning tree (MST) of a connected weighted undirected graph is the spanning tree with minimum total edge weight. It connects all V vertices with V-1 edges and minimum total cost.

**Prim's algorithm:** O(E log V) with a binary heap. Grows the MST one edge at a time, always adding the minimum-weight edge that crosses the cut between the current tree and remaining vertices. Identical structure to Dijkstra — replace `dist[v] = dist[u] + w(u,v)` with `key[v] = w(u,v)`.

**Kruskal's algorithm:** O(E log E) via sorting + nearly O(E) Union-Find. Sort all edges by weight; add each edge if it doesn't create a cycle (detected by Union-Find). Kruskal is better for sparse graphs; Prim with a Fibonacci heap is better for dense graphs.

**Union-Find (Disjoint Set Union):** The data structure that makes Kruskal efficient. Supports two operations: `find(v)` returns the representative of v's component; `union(u, v)` merges the components of u and v. With path compression and union by rank, both operations are O(α(N)) amortized — essentially O(1) for any practical N. α is the inverse Ackermann function, which is ≤ 4 for N ≤ 2⁶⁵⁵³⁶.

```python
class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])  # path compression
        return self.parent[x]

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return False  # already in same component — adding this edge creates a cycle
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return True
```

**Backend applications:**
- **Cluster formation in distributed systems:** Kubernetes node grouping, datacenter rack topology, network zone segmentation — MST gives the minimum-cost connectivity structure.
- **Cable/link provisioning:** Given N datacenters and the cost of laying a direct fiber link between each pair, MST gives the minimum-cost network that connects all of them.
- **Image segmentation and spatial clustering:** Euclidean MST on 2D points, then remove the longest k-1 edges to produce k clusters. This is single-linkage hierarchical clustering.

### Floyd-Warshall: All-Pairs Shortest Paths and Transitive Closure

Floyd-Warshall computes shortest paths between all pairs of vertices in O(V³) time and O(V²) space using dynamic programming.

```python
def floyd_warshall(W, V):
    dist = [row[:] for row in W]  # copy the weight matrix
    for k in range(V):
        for i in range(V):
            for j in range(V):
                dist[i][j] = min(dist[i][j], dist[i][k] + dist[k][j])
    return dist
```

The recurrence: `dist[i][j]` after considering intermediate vertices `{0, ..., k}` is the minimum of: (a) the shortest path using only `{0, ..., k-1}` as intermediates, or (b) the path through k, which is `dist[i][k] + dist[k][j]`. O(V³) is prohibitive for V > 10,000 but tractable for small-to-medium graphs.

**Transitive closure** (reachability for all pairs) is a Boolean variant: `T[i][j] = 1` if j is reachable from i. Replace `min(dist[i][j], dist[i][k]+dist[k][j])` with `T[i][j] || (T[i][k] && T[k][j])`. This is used to precompute "which services can reach which other services" in a static service topology — enabling O(1) reachability queries at runtime.

**Negative cycle detection:** If `dist[i][i] < 0` after Floyd-Warshall, there is a negative cycle reachable from i. This is the all-pairs analog of Bellman-Ford's cycle detection.

### A* and Heuristic Search: When the Graph Is Too Large to Explore

Dijkstra explores vertices in order of distance from source — it has no information about which direction leads toward the destination. For implicit graphs (graphs too large to store explicitly, or graphs generated on the fly), this is wasteful.

A* adds a heuristic `h(v)` estimating the cost from v to the destination. The priority key is `f(v) = g(v) + h(v)` where `g(v)` is the known cost from source to v and `h(v)` is the estimated remaining cost.

**Admissibility:** `h(v)` must never overestimate the true cost to destination. If it does, A* may skip the optimal path. Euclidean distance is admissible for road networks (roads cannot be shorter than straight-line distance). Haversine distance is admissible for geographic routing.

**Consistency (monotonicity):** `h(u) ≤ w(u,v) + h(v)` for all edges (u,v). A consistent heuristic is admissible. A consistent heuristic guarantees that A* never re-expands a vertex — it processes each vertex at most once, exactly like Dijkstra. Most practical heuristics satisfy consistency.

**Where A* appears in backend systems:**
- **Game AI pathfinding:** Every real-time game that has units navigating terrain uses A* or a variant. Grid-based A* with Manhattan distance heuristic is the standard.
- **Kubernetes scheduler:** Scheduling a pod to a node is a form of constraint satisfaction search. The scheduler doesn't use A* directly, but heuristic scoring functions play the same role — guiding the search toward good assignments without exhaustive exploration.
- **Robot motion planning:** A* on configuration space graphs underlies most robot planning systems.

---

## Complexity Analysis

| Algorithm | Time | Space | Preconditions | Use Case |
|---|---|---|---|---|
| BFS | O(V + E) | O(V) | Unweighted | Shortest hops, level structure |
| DFS | O(V + E) | O(V) | None | Cycle detection, SCC, topo sort |
| Dijkstra (binary heap) | O((V+E) log V) | O(V) | Non-negative weights | SSSP, routing |
| Dijkstra (Fibonacci heap) | O(E + V log V) | O(V) | Non-negative weights | Dense graph SSSP |
| Bellman-Ford | O(VE) | O(V) | No negative cycles | Negative weights, cycle detection |
| Floyd-Warshall | O(V³) | O(V²) | No negative cycles | All-pairs SP, transitive closure |
| Prim | O(E log V) | O(V) | Connected, undirected | MST |
| Kruskal | O(E log E) | O(V) | Connected, undirected | MST, sparse graphs |
| Tarjan SCC | O(V + E) | O(V) | Directed graph | SCCs, cycle detection |
| Dinic max flow | O(V²E) | O(V + E) | Directed, capacitated | Network flow, matching |
| A* | O(E log V) | O(V) | Admissible heuristic | Single-pair SP with guidance |
| Topological sort | O(V + E) | O(V) | DAG | Dependency ordering |

---

## Key Takeaways

1. Dijkstra fails silently on negative weights — it produces wrong answers, not errors. Before applying Dijkstra to any weighted graph, verify non-negativity. If weights can be negative (penalties, refunds, cost reductions), use Bellman-Ford or re-weight the graph using Johnson's algorithm (which runs Bellman-Ford once to reweight, then Dijkstra from every source).

2. InnoDB deadlock detection is DFS cycle detection on the transaction wait-for graph. The three-color DFS is the canonical O(V + E) implementation. `innodb_deadlock_detect=OFF` is a deliberate trade-off: skip the O(V + E) check on every lock wait, accept that deadlocks will hang until timeout. Correct for workloads with high contention but rare actual deadlocks.

3. Tarjan's SCC algorithm in a single DFS pass is the correct tool for circular dependency detection, not multi-pass DFS or repeated BFS. Any build system, package manager, or migration runner that does not use SCC-based cycle detection is reinventing the wheel badly.

4. The max-flow min-cut theorem equates two optimization problems. The min cut tells you the weakest link in any flow network — the set of edges whose removal would most damage throughput. This is a capacity planning primitive, not just a graph theory result.

5. Union-Find with path compression and union by rank achieves O(α(N)) per operation — for all practical purposes O(1). It is the correct data structure for online connectivity queries: given a stream of edge additions, answer "are u and v connected?" in near-constant time per query. This appears in Kruskal's MST, in network connectivity monitoring, and in cluster formation algorithms.

6. Bidirectional Dijkstra and A* are not micro-optimizations — they are the difference between routing working at geographic scale and not. Single-source Dijkstra on a continental road graph (10⁸ nodes) takes seconds; bidirectional Dijkstra takes milliseconds; Contraction Hierarchies takes microseconds. The algorithm choice is the feature.

7. The adjacency representation choice (matrix vs list vs CSR) changes the constant factor on every graph algorithm. For sparse graphs with E << V², adjacency lists or CSR are mandatory — an adjacency matrix turns O(V + E) algorithms into O(V²) by forcing O(V) neighbor enumeration.

8. Transitive closure precomputed via Floyd-Warshall trades O(V³) preprocessing and O(V²) storage for O(1) runtime reachability queries. For a static service dependency graph with V < 1000 services, this is the correct engineering decision — compute once, query constantly.

---

## Related Modules

- `../../04-computer-networks/04-dns-and-load-balancing.md` — Dijkstra in OSPF, BGP path selection, Contraction Hierarchies in geo-routing
- `../../06-databases/02-indexing.md` — DFS cycle detection in InnoDB deadlock detection; topological sort in query plan DAGs
- `../../08-systems-design/03-distributed-coordination.md` — max-flow for network capacity planning; SCCs in distributed deadlock detection
- `../03-trees-and-indexing.md` — Union-Find as the connectivity primitive underlying MST and cluster formation
- `../../09-performance-engineering/02-latency-analysis.md` — graph traversal in distributed trace reconstruction and critical path analysis