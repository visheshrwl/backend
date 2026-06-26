# Graphs and Network Algorithms

## Problem

Here's the unsettling thing about graph bugs: they don't crash. A null pointer throws. A failed assertion fires. An out-of-memory kills the process and leaves a stack trace. But a graph algorithm applied to the wrong kind of graph doesn't *fail* — it returns a confident, plausible, **wrong answer**, and then your system makes decisions on it for months before anyone notices the routing has been subtly suboptimal or the "this deployment is safe" check has been quietly lying.

And the reason this happens so much is that graphs are *everywhere in the backend, usually in disguise.* Your microservice topology is a directed graph. The "who's waiting on whose lock" picture inside your database is a graph, and a cycle in it is a deadlock. Your CDN's routing is a shortest-path problem over a weighted graph whose weights change by the second. A package manager resolving dependencies is doing cycle detection on a graph. A Kubernetes scheduler is solving a constrained matching problem on a graph. Your service mesh's "is this pod reachable?" health check is a reachability query on a graph that mutates every time a pod restarts. You are *surrounded* by graphs; you just don't always call them that.

So when a staff engineer diagnoses one of these systems, they're not running `ping` and `traceroute` and squinting at dashboards hoping for inspiration. They're reasoning from *structure*: "this behavior is governed by a shortest-path algorithm; that algorithm has a precondition; which precondition got violated?" That move — from a symptom to the algorithm to its broken invariant — is the entire skill, and it's a vocabulary, not a talent. This chapter gives you that vocabulary, built from the ground up. By the end you'll look at a flaky routing layer or a mysteriously-hanging dependency resolver and *see the graph*, and seeing the graph is most of the way to fixing it.

## Why It Matters (Latency, Throughput, Cost)

**Routing convergence is a graph-algorithm property you feel as a latency spike.** When a network link dies in an OSPF network, every router floods the news and then re-runs Dijkstra on its local copy of the topology. The window between "link failed" and "everyone agrees on the new routes" is the *convergence time* — and during that window, some routers have the new map and some have the old one, so they hand packets back and forth in contradictory directions, creating transient black holes. Dijkstra over V routers and E links is O((V+E) log V); on a big datacenter fabric that's milliseconds to seconds. *That* is why your service sometimes shows a 1–3 second latency spike right after a network blip, and why "graceful restart" protocols exist specifically to suppress the convergence storm. The spike isn't mysterious once you know which algorithm is running.

**Deadlock detection is cycle detection, and it has a price tag.** A database deadlock *is* a cycle in the "wait-for" graph: T1 waits on T2, T2 waits on T3, T3 waits on T1 — a loop with no exit. InnoDB maintains this graph and runs DFS-based cycle detection on *every lock request that has to wait*, O(V+E) each time. That cost is real enough that `innodb_deadlock_detect=OFF` is a legitimate tuning move for high-contention OLTP: you skip the check, accept that the rare true deadlock will hang until `innodb_lock_wait_timeout` fires, and buy back throughput. You can only make that trade intelligently if you know it's a cycle-detection cost you're turning off.

**The service dependency graph is your blast radius, computed in advance.** If A→B→C→D and D falls over, *everything that transitively depends on D* is affected — its entire set of ancestors in the dependency graph (the "transitive closure"). Teams that have computed this know, before they deploy a change to D, exactly which services are downstream. Teams that haven't computed it find out one service at a time, live, during the incident, reading the cascade off their alerts. Same graph; the only difference is whether you ran the reachability query before or after the outage.

**Max-flow equals min-cut, and the min-cut is your single point of failure.** The max-flow min-cut theorem says the most flow you can push from a source to a sink equals the *smallest-capacity set of links that, if cut, would disconnect them.* Translate to your datacenter: the maximum throughput from your app tier to your storage tier is capped by some minimum set of links — and that set is precisely the bottleneck you should upgrade first and the failure that would hurt most. Architects use max-flow analysis to find these chokepoints *before traffic does.* The theorem isn't abstract math; it's a capacity-planning instrument.

## Mental Model

The discipline is one disciplined sentence: **model the thing as a graph, name the question you're asking, pick the algorithm that answers exactly that question, and — this is the part people skip — check that your graph satisfies the algorithm's preconditions before you trust the answer.**

Almost every backend graph question is one of these shapes:

| Question | Algorithm | Cost |
|---|---|---|
| Can I get from A to B *at all*? | BFS / DFS | O(V+E) |
| Shortest path, **non-negative** weights | Dijkstra | O((V+E) log V) |
| Shortest path, **negative** weights allowed | Bellman-Ford | O(VE) |
| Shortest paths between **all pairs** | Floyd-Warshall | O(V³) |
| Cheapest way to connect everything | MST (Prim / Kruskal) | O(E log V) |
| Maximum throughput source→sink | Max-flow (Dinic) | O(V²E) |
| Valid order respecting dependencies | Topological sort | O(V+E) |
| Which nodes form cycles together | SCC (Tarjan) | O(V+E) |

The non-negotiable habit is the last clause: **verify preconditions.** Dijkstra's correctness *proof* depends on edges being non-negative. Feed it a negative edge and it doesn't throw — it returns a wrong shortest path that looks exactly like a right one. The failure that doesn't announce itself is the whole reason graph bugs are insidious, and the only defense is checking the precondition *before* you trust the output, not after the incident.

## Underlying Theory

We'll build from the most primitive question (can I even reach B?) up to the global ones (what's the bottleneck of the whole network?). Each layer is a new question and the algorithm that earns it.

### Layer 0 — How you store the graph decides everything downstream

Before a single algorithm runs, you choose a *representation*, and that choice silently sets the constant factor — sometimes the *complexity class* — of everything that follows. Three options:

**Adjacency matrix** — a V×V grid where cell `(u,v)` holds the edge weight (or ∞ for "no edge"). "Is there an edge u→v?" is O(1). But listing a node's neighbors is O(V) *no matter how few it actually has*, and storage is O(V²). For a 10,000-node graph that's 800 MB — to store a graph that might have only 100,000 real edges. Matrices are right only for *dense* graphs (where E is close to V²), which almost no real-world graph is.

**Adjacency list** — an array of V little lists, each holding one node's actual neighbors. Listing neighbors is O(degree), storage is O(V+E). This is the default for essentially every real graph, because real graphs (road networks, service meshes, social graphs, the internet) are *sparse* — average degree far below V.

**Compressed Sparse Row (CSR)** — and here the arrays chapter comes roaring back. CSR is two flat arrays: `offsets[V+1]` and `neighbors[E]`. Node v's neighbors are the slice `neighbors[offsets[v] : offsets[v+1]]`.

```
Graph:  0→1, 0→2, 1→2, 2→0

offsets:   [0,    2,    3,    4]      offsets[v]..offsets[v+1] = v's neighbor slice
neighbors: [1, 2, 2, 0]
            └─0─┘ └1┘ └2┘             node 0 → {1,2}, node 1 → {2}, node 2 → {0}
```

No pointers, no per-node allocations — just contiguous arrays you scan linearly. That's the sequential-access free lunch from chapter 01: the prefetcher loves it, every cache line is full of real edges, and a traversal that touches most edges runs **3–5× faster than an adjacency list** purely on cache locality. This is why production graph engines (GraphX, DGL, scientific sparse libraries) store graphs as CSR.

The stakes aren't cosmetic. Use a matrix on a sparse graph and Dijkstra's neighbor enumeration becomes O(V) per node, turning O((V+E) log V) into O(V² log V) — for a 100K-node graph of average degree 10, the difference between ~10⁶ and ~10¹⁰ operations. Same algorithm, same answer, *ten thousand times* the work, decided entirely by a representation choice made before the algorithm started.

### Layer 1 — BFS and DFS aren't algorithms, they're two ways to walk

This is the reframing that makes everything click: **BFS and DFS are not algorithms — they're traversal *strategies*, and they turn into specific algorithms depending on what you compute as you walk.** The only difference between them is the data structure holding "where to go next": BFS uses a **queue** (FIFO), DFS uses a **stack** (LIFO). That one choice produces two completely different superpowers.

**BFS explores in rings, so it computes shortest paths in unweighted graphs — for free.** Because a queue processes nodes in the order they were discovered, BFS finishes everything 1 hop away before anything 2 hops away, then everything 2 hops before 3, and so on. The frontier expands like a ripple:

```
        source
          ●
        / | \           ring 0: {source}
      ●   ●   ●          ring 1: 1 hop away
     /|       |\         ring 2: 2 hops away
    ● ●       ● ●        ...the FIRST time BFS reaches a node, it's via the fewest hops
```

So the *moment* BFS first touches a node, you've found its shortest unweighted path — not by accident, but as a structural guarantee of FIFO order. This is minimum-hop routing, social-network degrees-of-separation, and the level-synchronous model behind big graph engines like Pregel.

**DFS plunges deep, so it reveals structure.** By following one path as far as it goes before backtracking, DFS builds a tree whose *edges classify the whole graph*. The classification that matters most: a **back edge** — an edge pointing back to an ancestor still on the current path — *exists if and only if there's a cycle.* That single fact is the canonical linear-time way to answer "is this graph acyclic (a DAG)?" and it's the engine inside your deadlock detector.

The cleanest implementation is the **three-color DFS**, and it's worth burning into memory because you'll meet it in real systems:

```python
WHITE, GRAY, BLACK = 0, 1, 2   # unseen, on-the-current-path, fully-done

def has_cycle(graph, V):
    color = [WHITE] * V

    def dfs(u):
        color[u] = GRAY              # u is now ON the active path
        for v in graph[u]:
            if color[v] == GRAY:     # edge back to something on our path → CYCLE
                return True
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK             # u and its whole subtree are finished
        return False

    return any(dfs(u) for u in range(V) if color[u] == WHITE)
```

GRAY means "currently on the stack, an ancestor of where I am now." Hitting a GRAY node means you've found an edge looping back to your own path — a cycle. This *is* what InnoDB's deadlock detector runs: transactions are nodes, "T1 waits for a lock T2 holds" are edges, and a GRAY hit means a deadlock that must be broken by aborting one transaction. And topological sort — a valid dependency order — is just the reverse of the order DFS finishes nodes (reverse postorder) on a DAG. Same walk, different thing computed on the way.

### Layer 2 — Dijkstra: greedy works, but only if you never get cheaper

Now add weights. Edges cost different amounts (latency, money, distance), and we want the genuinely cheapest path, not the fewest hops. Dijkstra's idea is beautifully greedy: keep a frontier of "tentative best distances," and repeatedly **finalize the closest unfinalized node**, then relax its neighbors (try to improve their tentative distances through it).

```
dist[source] = 0,  everything else = ∞
put all nodes in a min-heap keyed by tentative dist
while heap not empty:
    u = pop the closest unfinalized node     ← greedy choice: u's distance is now FINAL
    for each edge (u → v, weight w):
        if dist[u] + w < dist[v]:
            dist[v] = dist[u] + w            ← "relax": found a cheaper way to v
```

Here is the load-bearing question the textbook rushes past: **why is it safe to declare `dist[u]` final the instant we pop u?** Because u was the closest remaining node, and every *other* unfinalized node is at least as far away — so any alternative path to u would have to detour through one of those farther nodes and then add a **non-negative** edge to come back. That detour can only be *longer*. The greedy choice is safe.

And right there, in the words "non-negative edge," lives Dijkstra's fatal precondition. **If even one edge can be negative, that argument collapses** — a node you finalized "early" could later be improved by some cheaper-than-free edge from a node you finalize "late." Dijkstra doesn't detect this. It hands back a wrong shortest path that's indistinguishable from a right one unless you already know the answer. This is the silent-failure pattern from the intro made concrete: the moment your weights can go negative (refunds, penalties, rebates, score reductions), Dijkstra is not "a little off" — it's *invalid*, and you need Layer 3.

**Dijkstra in the real world is rarely vanilla**, because at geographic scale plain Dijkstra is too slow (it explores blindly in all directions). The optimizations are the feature, not a micro-tweak:

- **Bidirectional Dijkstra** runs two searches at once — forward from the source, backward from the destination — and stops when they meet in the middle, roughly halving the explored area.
- **A\*** adds a *heuristic* `h(v)` estimating the remaining distance to the goal, and orders the frontier by `g(v) + h(v)` (known cost so far + estimated cost to go). With an **admissible** heuristic (one that never *over*estimates — straight-line/Haversine distance for maps), A* provably still finds the optimal path while exploring dramatically less, because it's biased *toward the goal* instead of expanding uniformly.
- **Contraction Hierarchies** preprocess the road network into a layered hierarchy with "shortcut" edges, so a continental route that would take plain Dijkstra *seconds* runs in *microseconds*. This is why Google Maps returns a cross-country route in under 100 ms. The query is the same shortest-path question; the engineering around it is the difference between usable and not.

### Layer 3 — Bellman-Ford: slower, but it handles the negatives Dijkstra can't

When edges *can* be negative, you give up Dijkstra's greedy shortcut and do something almost brutally simple: **relax every edge, V−1 times.**

```python
def bellman_ford(V, edges, source):
    dist = [float('inf')] * V
    dist[source] = 0
    for _ in range(V - 1):                  # V-1 rounds
        for u, v, w in edges:
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
    for u, v, w in edges:                   # one more round...
        if dist[u] + w < dist[v]:
            return None                     # ...still improving? NEGATIVE CYCLE.
    return dist
```

Why V−1 rounds? Because any shortest *simple* path visits at most V nodes, hence at most V−1 edges, and each full relaxation round is guaranteed to extend every shortest path by at least one more correct edge. After V−1 rounds, every shortest path is fully built. It's O(VE) — slower than Dijkstra — but it earns two things Dijkstra can't give you: it works with negative weights, and that *Vth* round is a **negative-cycle detector.** If anything still improves on round V, there's a cycle whose total weight is negative — a loop you could ride forever getting "cheaper," meaning "shortest path" is undefined. Detecting that is often the entire point (think: arbitrage cycles in currency exchange, or a misconfigured cost graph that would make routing diverge).

This isn't academic. The old **RIP** routing protocol is distributed Bellman-Ford, and its infamous "count-to-infinity" slow convergence after a link failure is a *direct* consequence of Bellman-Ford's iterative nature — routers incrementing their distance estimates one round at a time, painfully, toward the truth. **BGP**, the protocol holding the internet together, deliberately uses a *path-vector* design (carry the whole path, reject any route that already contains you) precisely to dodge that loop-formation problem. You can't reason about why these protocols converge the way they do without Bellman-Ford in your head.

### Layer 4 — Strongly connected components: finding the cycles that tangle your services

A **strongly connected component (SCC)** is a maximal group of nodes where *every* node can reach every *other* node in the group. In a DAG, every SCC is a single node (no cycles). An SCC bigger than one node *is* a cycle — a knot of mutual reachability. So "find the SCCs" is the precise, global way to ask "where are the circular dependencies?"

**Tarjan's algorithm** finds all SCCs in a single DFS pass, O(V+E), which is as good as it gets. The trick is a per-node `lowlink`: the earliest-discovered node reachable from this node's subtree. When a node's `lowlink` equals its own discovery index, it's the "root" of an SCC, and everything sitting above it on the stack is its component.

```python
def tarjan_scc(graph, V):
    idx = [None]*V; low = [0]*V; on_stack = [False]*V
    stack = []; counter = [0]; sccs = []

    def connect(v):
        idx[v] = low[v] = counter[0]; counter[0] += 1
        stack.append(v); on_stack[v] = True
        for w in graph[v]:
            if idx[w] is None:                 # unvisited → recurse
                connect(w); low[v] = min(low[v], low[w])
            elif on_stack[w]:                  # w is in the current SCC-in-progress
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:                   # v is an SCC root → pop the component
            comp = []
            while True:
                w = stack.pop(); on_stack[w] = False; comp.append(w)
                if w == v: break
            sccs.append(comp)

    for v in range(V):
        if idx[v] is None: connect(v)
    return sccs
```

Where this earns its keep: **circular dependency detection** in package managers, build systems, and service registries — an SCC of size > 1 is a dependency cycle that can't be resolved without breaking it (the reason `pip`'s resolver can grind). **Dead-code elimination** in compilers — find the SCC containing `main`, prune every SCC it can't reach. **Database transaction scheduling** — an SCC > 1 in the dependency graph is a mutual-dependency knot, a deadlock waiting to happen. Whenever the question is "which things are tangled together in a cycle," reach for SCCs, not a hand-rolled pile of repeated BFS passes.

### Layer 5 — Union-Find and MST: connect everything for the least cost

Sometimes you don't want paths — you want *connectivity*. "Cheapest set of links that connects all N datacenters." "Are u and v in the same cluster yet?" These are **minimum spanning tree** and **connectivity** questions, and they're powered by one of the most magical little data structures in all of computing.

**Union-Find (Disjoint Set Union)** answers a stream of "merge these two groups" and "are these two in the same group?" operations in **effectively O(1) each** — formally O(α(N)), the inverse Ackermann function, which is ≤ 4 for any N you will ever encounter in this universe. Two ideas make it work: *path compression* (when you look up a node's group, flatten its path to the root so next time is instant) and *union by rank* (always hang the shorter tree under the taller one).

```python
class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n)); self.rank = [0]*n

    def find(self, x):
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])   # path compression
        return self.parent[x]

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry: return False        # already connected → this edge would make a cycle
        if self.rank[rx] < self.rank[ry]: rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]: self.rank[rx] += 1
        return True
```

That `return False` on "already connected" is the whole engine of **Kruskal's MST algorithm**: sort all edges cheapest-first, then add each edge *only if* it joins two currently-separate components (Union-Find tells you in near-constant time); skip it if it would form a cycle. When you've added V−1 edges, you have the minimum-cost tree connecting everything. (**Prim's algorithm** grows the tree from one node using a priority queue — structurally *identical* to Dijkstra, just with `key[v] = edge weight` instead of `dist[u] + weight`. Kruskal suits sparse graphs; Prim suits dense ones.)

Where it lands in the backend: minimum-cost network/fiber provisioning between datacenters, cluster formation and rack-topology grouping, online connectivity monitoring ("is this partition healed yet?"), and single-linkage clustering (build a Euclidean MST, cut the longest edges to get clusters). Union-Find is one of those structures that, once you have it, you start seeing connectivity problems everywhere.

### Layer 6 — Max-flow / min-cut: the throughput and the bottleneck are the same number

Now give every edge a **capacity** and ask: how much can flow from source `s` to sink `t` at once, respecting capacities and conserving flow at every node? This is **maximum flow**, and it comes with one of the most quietly profound theorems in CS.

The **Ford-Fulkerson** method is intuitive: find any path from `s` to `t` with spare capacity (an "augmenting path"), push as much as it allows, repeat until no such path exists. The one subtle ingredient is the *residual graph* — when you push flow along an edge, you add a phantom *back* edge representing "the ability to undo this later," which lets the algorithm reroute around early greedy mistakes. (**Dinic's algorithm** is the fast version, O(V²E), and O(E√V) on unit-capacity graphs — the one you'd actually use.)

The payoff is the **max-flow min-cut theorem**: *the maximum flow from s to t equals the minimum-capacity cut separating s from t.* A "cut" is a way to split the nodes into s's side and t's side; its capacity is the total of the edges crossing from one side to the other.

```
   s ──10──► A ──10──► t
   │                   ▲
   └───5────► B ───5───┘

   Max flow s→t = 15.   The min cut (the edges that, summed, bottleneck it) = 15.
   Find the max throughput and you've simultaneously found the weakest link set.
```

Read what that *means*: the most you can push through equals the cheapest set of links to sever to disconnect the two sides. So one computation hands you both your **throughput ceiling** *and* your **single point of failure** — the exact links to upgrade first and the cut whose loss would hurt most. Network capacity planning lives here. So does **bipartite matching** (assigning tasks to compatible workers, requests to servers) — it's a special case of max-flow, the optimal assignment falling out of the maximum matching.

### Layer 7 — All-pairs and transitive closure: precompute the blast radius

The final altitude: not "shortest path from *one* source" but "shortest path between *every* pair," and its boolean cousin "*can* every pair reach each other." **Floyd-Warshall** does all-pairs shortest paths in a startlingly tiny amount of code:

```python
def floyd_warshall(W, V):           # W = weight matrix, ∞ where no edge
    dist = [row[:] for row in W]
    for k in range(V):              # "allow paths that may route through node k"
        for i in range(V):
            for j in range(V):
                dist[i][j] = min(dist[i][j], dist[i][k] + dist[k][j])
    return dist
```

The idea is pure dynamic programming: after the outer loop has considered `k = 0..K`, `dist[i][j]` is the best path from i to j *allowed to route through any of those K nodes as intermediates.* Each new `k` asks "does detouring through k beat what I had?" It's O(V³) — too much past ~10K nodes, but perfect for the small, important graphs backends actually have.

And the most useful backend variant is the boolean one: **transitive closure.** Replace `min(dist[i][j], dist[i][k]+dist[k][j])` with `reach[i][j] = reach[i][j] or (reach[i][k] and reach[k][j])`, and you precompute, for a static service-dependency graph, *exactly which services can reach which others.* Spend O(V³) once, store O(V²), and now "if D dies, who's affected?" is an **O(1) lookup** instead of a frantic live traversal during the incident. For a topology of a few hundred services, that's a clearly correct engineering trade — and it's the difference between knowing your blast radius before you deploy versus discovering it from your pager. (Floyd-Warshall also detects negative cycles for free: if any `dist[i][i]` goes negative, node i sits on one.)

## A Ladder From L1 to Principal

- **L1 / new grad:** You can model a problem as nodes and edges, run BFS/DFS, and detect a cycle. You know unweighted-shortest-path is BFS and weighted is Dijkstra.
- **L3–L4 / solid engineer:** You pick the right algorithm per question, know Dijkstra needs non-negative weights, choose adjacency list over matrix for sparse graphs, and recognize topological sort behind build/migration ordering.
- **Senior:** You see the graph hiding inside production systems — deadlock = cycle detection, dependency resolution = SCC, blast radius = transitive closure — and you reason about representation (CSR for cache locality) and convergence behavior.
- **Staff:** You make the engineering trades: `innodb_deadlock_detect` on/off, precomputed transitive closure vs. live traversal, bidirectional Dijkstra/A*/CH for routing scale, max-flow analysis for capacity planning. You diagnose by asking which invariant broke.
- **Principal:** You treat the system's graph structure as a first-class design artifact — you architect dependency graphs to *avoid* cycles and bound blast radius, choose routing/convergence strategies against their failure modes, and use min-cut analysis to design for the failures you haven't had yet. "Where's the graph and what's its weakest invariant?" is reflex.

The whole ladder is one move repeated at higher and higher altitude: *find the graph, name the question, respect the precondition.*

## Complexity Analysis

| Algorithm | Time | Space | Precondition | What it answers |
|---|---|---|---|---|
| BFS | O(V+E) | O(V) | Unweighted | Fewest hops, level structure |
| DFS | O(V+E) | O(V) | None | Cycle detection, SCC, topo sort |
| Dijkstra (binary heap) | O((V+E) log V) | O(V) | **Non-negative weights** | Single-source shortest path |
| Dijkstra (Fibonacci heap) | O(E + V log V) | O(V) | Non-negative weights | Dense-graph SSSP |
| Bellman-Ford | O(VE) | O(V) | No negative *cycle* | Negative weights + cycle detection |
| Floyd-Warshall | O(V³) | O(V²) | No negative cycle | All-pairs SP, transitive closure |
| Prim | O(E log V) | O(V) | Connected, undirected | MST |
| Kruskal | O(E log E) | O(V) | Connected, undirected | MST (sparse), via Union-Find |
| Tarjan SCC | O(V+E) | O(V) | Directed | SCCs, circular dependencies |
| Dinic max-flow | O(V²E) | O(V+E) | Directed, capacitated | Throughput + min-cut |
| A* | O(E log V) | O(V) | Admissible heuristic | Single-pair SP with guidance |
| Topological sort | O(V+E) | O(V) | DAG (acyclic!) | Dependency ordering |
| Union-Find op | O(α(N)) ≈ O(1) | O(N) | — | Online connectivity |

The single most consequential number here isn't a complexity — it's the **precondition column**, because that's where the silent wrong answers come from.

## War Stories (the shape of the bug in the wild)

- **The negative edge that lied.** A cost-routing system added "rebate" edges (negative weights) and kept using Dijkstra. No crash, no error — just subtly suboptimal routes for months, until someone cross-checked against Bellman-Ford and found Dijkstra had been finalizing nodes too early the whole time. The fix was the algorithm, not the data.
- **The deadlock detector you turned off.** A high-contention OLTP service set `innodb_deadlock_detect=OFF` for throughput, then started seeing occasional multi-second hangs. Working as designed: with detection off, true deadlocks wait for `innodb_lock_wait_timeout` instead of being broken instantly. The "bug" was an informed trade-off whose downside nobody had written down.
- **The dependency cycle that hung the deploy.** A new service edge quietly created a cycle (A→B→C→A) in the startup-order graph; the orchestrator's topological sort couldn't produce an order and the rollout stalled with no obvious error. Tarjan's SCC over the dependency graph pinpointed the three-node knot in seconds.
- **The blast radius nobody had computed.** A change to a low-level auth service took down a dozen "unrelated" services. They weren't unrelated — they were the transitive closure of the auth node, which no one had computed. After the incident the team precomputed reachability and turned future "who's downstream of X?" into an O(1) lookup.

## Key Takeaways

1. **Graph algorithms fail silently.** The dangerous bug isn't a crash — it's a confident wrong answer from running the right algorithm on a graph that violates its precondition. Verify preconditions (especially Dijkstra's non-negativity) *before* trusting output.
2. **Representation sets the constant factor — sometimes the complexity class.** Use adjacency lists or CSR for sparse graphs; a matrix turns O(V+E) traversals into O(V²). CSR's contiguous arrays give the chapter-01 cache free lunch, 3–5× over adjacency lists.
3. **BFS and DFS are traversal strategies, not algorithms.** Queue (BFS) → shortest unweighted paths in rings; stack (DFS) → structure, with a back edge meaning a cycle. The three-color DFS *is* your database's deadlock detector.
4. **Dijkstra is greedy and that's exactly why it needs non-negative weights.** When weights can go negative, switch to Bellman-Ford — slower (O(VE)) but correct, and its Vth round detects negative cycles. RIP, BGP, and "count-to-infinity" all live in this distinction.
5. **SCCs (Tarjan, one DFS pass) are the right tool for circular dependencies** — package resolution, build order, transaction knots — not ad-hoc repeated traversals.
6. **Union-Find gives near-O(1) connectivity**, powering Kruskal's MST and online "are these connected?" queries. MST is your minimum-cost way to connect datacenters, form clusters, or provision links.
7. **Max-flow equals min-cut**, so one computation yields both your throughput ceiling and your single-point-of-failure link set — a capacity-planning primitive, and bipartite matching is a special case.
8. **Precompute transitive closure for static dependency graphs** (O(V³) once, O(V²) stored) to turn "what's the blast radius of X?" into an O(1) lookup you run *before* deploying, not during the outage.

## Related Modules

- `01-arrays-and-memory-layout.md` — CSR is the arrays-chapter cache argument applied to graph storage; why contiguous beats pointer-chasing for traversal
- `02-hash-tables.md` — adjacency structures and visited-sets rely on hashing; consistent hashing (there) is itself a graph-on-a-ring construction
- `03-trees-and-indexing.md` — trees are acyclic connected graphs; Union-Find underlies clustering, and DFS cycle detection backs deadlock checks in the index/lock layer
- `05-sorting-and-searching.md` — Kruskal sorts edges first; priority queues (heaps) power Dijkstra and Prim
- `../04-computer-networks/04-dns-and-load-balancing.md` — Dijkstra in OSPF, BGP path-vector selection, Contraction Hierarchies in geo-routing
- `../06-databases/02-indexing.md` — DFS cycle detection in InnoDB deadlock detection; topological ordering of query-plan DAGs
- `../08-systems-design/` — max-flow for capacity planning; SCCs and blast-radius/transitive-closure analysis in distributed architectures
