# Backend & Systems Performance System (BSPS)

## What Is BSPS?

BSPS is a structured, depth-first learning system for backend engineers who want to understand **why systems behave the way they do** — not just how to use frameworks.

Every module connects theory to practice: operating systems → networking → databases → backend patterns → production operations. The cross-domain connections are the point. Understanding TCP state machines makes connection pooling obvious. Understanding CPU cache hierarchies makes data structure choices intuitive. Understanding queueing theory makes pool sizing precise.

```
Mathematics ──────────────────────────────────────────────────────────────┐
  Big-O, probability, statistics, queueing theory                         │
                                                                           │
Operating Systems ─────────────────────────────────────────────────────── │
  Processes, threads, memory, I/O, scheduling, virtual memory             │
  └─► explains: why connection pools exist, why async beats threads       │
                                                                           │
Computer Networks ──────────────────────────────────────────────────────── │
  OSI, TCP, HTTP, DNS, congestion control                                 │
  └─► explains: why RTT matters, why N+1 is catastrophic                 │
                                                                           ▼
Data Structures & Algorithms ───────────────────────────────────────────────
  Arrays, hash tables, trees, graphs, sorting
  └─► explains: why LRU is O(1), why indexes use B-trees
                          │
                          ▼
          Core Backend Engineering  ◄── This is where it all connects
          N+1 queries, connection pooling, caching, async/threading
                          │
                          ▼
          Systems Design ── Performance Engineering ── Production
```

---

## Learning Paths

### Path 1: Junior → Mid-level (Backend foundations)

1. `00-orientation/` — Start here, understand the system
2. `01-mathematics-for-systems/` — Big-O, probability basics
3. `02-data-structures-and-algorithms/` — Arrays, hash tables, trees
4. `03-operating-systems/` — Processes, memory, I/O
5. `04-computer-networks/` — TCP, HTTP fundamentals
6. `07-core-backend-engineering/01-n-plus-one-query-problem.md` — Most impactful practical module
7. `07-core-backend-engineering/02-connection-pooling.md`
8. `07-core-backend-engineering/03-caching-strategy.md`
9. `labs/lab-01-n-plus-one-profiling/` — Apply the theory

**Estimated time:** 40–60 hours

### Path 2: Mid-level → Senior

Complete Path 1, then:

1. `05-network-programming/` — Sockets, epoll, non-blocking I/O
2. `06-databases/` — Storage engines, indexing, transactions, MVCC
3. `07-core-backend-engineering/04-threading-vs-async-vs-event-loop.md`
4. `07-core-backend-engineering/05-rate-limiting.md`
5. `07-core-backend-engineering/06-api-design.md`
6. `08-systems-design/` — Scalability, consistency, distributed systems
7. `09-performance-engineering/` — Profiling, latency analysis
8. `labs/lab-02-connection-pool-tuning/` — Hands-on pool tuning
9. `benchmarks/` — Run and analyze all benchmarks

**Estimated time:** 60–100 hours additional

### Path 3: Senior → Staff

Complete Paths 1 and 2, then:

1. `10-production-systems/` — Observability, incidents, capacity
2. `11-real-world-systems/` — Redis, Nginx, PostgreSQL internals
3. `12-staff-engineer-playbook/` — Technical leadership, ADRs
4. `case-studies/` — Real-world failure analysis
5. `enterprise-kit/` — Audit and migration tools

**Estimated time:** 40–60 hours additional

---

## Module Index

| Module | Description | Key Topics |
|--------|-------------|-----------|
| `00-orientation` | System overview and navigation | How to use BSPS, prerequisites |
| `01-mathematics-for-systems` | Math foundations for engineers | Big-O, probability, statistics, queueing theory |
| `02-data-structures-and-algorithms` | CS fundamentals with systems context | Arrays, hash tables, trees, graphs, sorting |
| `03-operating-systems` | OS internals relevant to backends | Processes, memory, I/O, scheduling, virtual memory |
| `04-computer-networks` | Network stack from TCP to HTTP/2 | OSI model, TCP deep dive, DNS, congestion control |
| `05-network-programming` | Writing networked code | Sockets, epoll/kqueue, non-blocking I/O, protocols |
| `06-databases` | Database internals | Storage engines, indexing, query planning, MVCC |
| `07-core-backend-engineering` | High-impact practical patterns | N+1, pooling, caching, async, rate limiting, API design |
| `08-systems-design` | Distributed systems architecture | Scalability, consistency models, distributed fundamentals |
| `09-performance-engineering` | Measurement and optimization | Profiling, latency analysis, throughput optimization |
| `10-production-systems` | Running systems in production | Observability, incident response, capacity planning |
| `11-real-world-systems` | Deep dives into real software | Redis internals, Nginx internals, PostgreSQL internals |
| `12-staff-engineer-playbook` | Engineering leadership | Technical leadership, ADRs, system reviews |

---

## Most Important Files (Start Here)

If you only have time for five files:

1. **`bsps/07-core-backend-engineering/01-n-plus-one-query-problem.md`** — The most common performance bug.
2. **`bsps/07-core-backend-engineering/02-connection-pooling.md`** — Every backend service needs a pool. Most teams configure it wrong.
3. **`bsps/07-core-backend-engineering/03-caching-strategy.md`** — The difference between 50ms and 0.5ms response time.
4. **`bsps/07-core-backend-engineering/04-threading-vs-async-vs-event-loop.md`** — Foundation of any high-concurrency server.
5. **`labs/lab-01-n-plus-one-profiling/README.md`** — Run it. See the numbers.

---

## The Cross-Domain Philosophy

Every performance problem has a root cause in one of four domains:

```
SYMPTOM                  ROOT CAUSE DOMAIN
──────────────────────────────────────────────────────────────────────
Slow API responses     → Network (RTT × query_count)
High memory usage      → OS (thread stacks, buffer sizes)
DB CPU spikes          → Algorithms (O(N) queries vs O(1))
Connection timeouts    → Queueing theory (pool undersized)
Cache thundering herd  → Probability (Poisson arrivals at expiry)
Goroutine leak         → OS (resource lifecycle management)
Slow joins             → Data structures (B-tree index scan vs seq)
```

BSPS is organized so that when you encounter a production problem, you can trace it to its domain and find the relevant theory.

---

## Repository Structure

```
bsps/                   Core curriculum (13 modules)
labs/                   Hands-on exercises with runnable code
benchmarks/             Reproducible performance benchmarks
simulations/            System behavior simulators
case-studies/           Real-world failure analysis
audit/                  Scoring system for backend systems
playbooks/              Operational runbooks
enterprise-kit/         Onboarding and audit tools
reference/              Quick-reference cheat sheets
```

See `CONTRIBUTING.md` for content standards. See `STYLE_GUIDE.md` for formatting conventions.
