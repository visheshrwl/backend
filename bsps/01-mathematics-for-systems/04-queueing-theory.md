# Queueing Theory

## Problem

How many database connections do you need? How long will requests wait when your API server is at 80% load? What happens to latency as utilization approaches 100%? These questions have exact mathematical answers.

## Why It Matters

```
Without queueing theory:  "I'll set pool_size=100, that seems like a lot"
With queueing theory:     "My DB has 8 cores, avg query is 10ms,
                           target 500 req/s:
                           pool_size = ceil(500 × 0.010) × 1.3 = 7"
```

## Little's Law

**L = λW** — the most useful equation in systems engineering.

- **L** = average number of items in the system (queue + service)
- **λ** (lambda) = arrival rate (items/second)
- **W** = average time each item spends in the system (seconds)

This holds for **any stable queuing system** regardless of arrival or service distributions.

### Applications

**Connection pool sizing:**
```
Given: 200 req/s hitting the DB, avg query = 15ms
L = λ × W = 200 × 0.015 = 3 connections needed on average
Add headroom for bursts: pool_size = L × 2 = 6 connections
```

**Request queue depth:**
```
Given: 1000 req/s, avg latency = 50ms
L = 1000 × 0.050 = 50 requests in flight at any time
If your server handles only 40 concurrent → queue backs up → latency explodes
```

**Cache expiry and refresh:**
```
Given: 10,000 cache lookups/minute, avg item lives 60 seconds
L = (10,000/60) × 60 = 10,000 items in cache at any time
Cache sizing: 10,000 × avg_item_size
```

## The Utilization Saturation Curve

The M/M/1 queue model (Poisson arrivals, exponential service time, 1 server):

```
Average wait time W_q = (ρ / μ) × (1 / (1 - ρ))

Where:
  ρ (rho) = λ/μ = utilization (0 to 1)
  μ = service rate (requests/second)
  λ = arrival rate (requests/second)
```

**The hockey stick:**
```
Utilization (ρ)  |  Wait multiplier (W_q / service_time)
─────────────────────────────────────────────────────────
10%              |  0.11×  (nearly no wait)
50%              |  1.00×  (wait = 1 service time)
70%              |  2.33×
80%              |  4.00×
90%              |  9.00×
95%              |  19.0×
99%              |  99.0×
```

This is why "always keep utilization < 70%" is practical wisdom — above 70%, latency grows faster than load.

## M/M/c: Multi-Server Queue (Connection Pool)

For c parallel servers (connection pool of size c):

```
The Erlang C formula gives P(wait) — probability a request must wait:

For practical purposes, use the approximation:
  avg_wait ≈ (P_wait × service_time) / (c × (1 - ρ/c))

Where ρ/c = per-server utilization
```

In practice: use the formula `pool_size = ceil(λ × avg_service_time × safety_factor)` where safety_factor = 1.2–1.5.

## Benchmark: Queueing vs Non-Queueing

```
Pool size=5, query time=10ms, arrival rate=100 req/s:
  Utilization ρ = 100 × 0.010 / 5 = 0.20  (20% per server)
  Expected wait ≈ 0.05ms  → p99 ≈ 10.5ms

Pool size=5, arrival rate=400 req/s (ρ = 0.80):
  Expected wait ≈ 4 × 10ms = 40ms  → p99 ≈ 65ms  

Pool size=5, arrival rate=450 req/s (ρ = 0.90):
  Expected wait ≈ 9 × 10ms = 90ms  → p99 ≈ 130ms
  (system approaching instability)
```

## Key Takeaways

1. **Little's Law:** L = λW. Memorize this. Use it to size every resource.
2. **The hockey stick:** at >80% utilization, latency grows exponentially. Design for 70% max.
3. **Pool sizing:** `pool_size = ceil(peak_rps × avg_query_seconds × 1.3)`.
4. **Stability requires:** arrival rate < service rate. If λ > μ, the queue grows without bound.
5. **M/M/c > M/M/1:** more servers reduces wait time super-linearly. Doubling pool size more than halves wait time when heavily loaded.
