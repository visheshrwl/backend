# Module 01: Mathematics for Systems

## Why Math for Backend Engineers?

Backend performance analysis requires math. Not advanced math — but specific applied math that most engineers never learned formally:

- **Big-O analysis** tells you whether your algorithm will work at 100 rows or 100 million rows
- **Probability** explains cache hit rates, failure probabilities, and expected values
- **Statistics** is how you interpret benchmark results without being fooled
- **Queueing theory** is the formal model behind connection pool sizing and load balancing
- **Numerical stability** matters when you compute money, time deltas, or coordinates

## Contents

| File | What you learn |
|------|---------------|
| `01-big-o-analysis.md` | Time and space complexity with real backend examples |
| `02-probability-for-engineers.md` | Expected values, distributions, birthday problem |
| `03-statistics-for-performance.md` | p50/p99/p999, histograms, outlier analysis |
| `04-queueing-theory.md` | M/M/1 queue, Little's Law, Erlang C formula |
| `05-numerical-stability.md` | Floating point, integer overflow, decimal arithmetic |

## Key Insight

Every performance equation in later modules traces back to this module.

```
N+1 query cost  = O(N) queries × RTT      ← Big-O + networking
Pool wait time  = L / λ = W               ← Little's Law (queueing)
Cache hit rate  = Zipf distribution       ← Probability
p99 vs p50 gap = heavy-tailed distribution ← Statistics
```
