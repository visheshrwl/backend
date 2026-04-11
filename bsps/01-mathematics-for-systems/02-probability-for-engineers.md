# 02-probability-for-engineers

## Problem

The average backend engineer has a fundamentally broken relationship with numbers. Not because they can't compute — they can. The problem is subtler: they apply deterministic reasoning to inherently stochastic systems.

Consider a routine postmortem. The service had "an average latency of 45ms." The SLA was 100ms. The incident lasted three hours. How did it breach SLA? Because averages are the most dangerous lie in distributed systems. The average hides the shape. A service with a p50 of 45ms and a p99 of 850ms is not a fast service — it's a service that is fast for most users and catastrophic for some. The product team reports the p50. The SLA measures the tail. The postmortem never reconciles the two.

This module is about replacing average-think with distribution-think. It won't make you a statistician. It will make you a better engineer.

---

## Why It Matters (Latency, Throughput, Cost)

**The compounding tail problem.** In a microservice architecture, the end-to-end p99 latency is not the sum of each service's p99. It's worse. If you have 10 services each with independent p99 of 50ms, the probability that at least one of them exceeds 50ms is `1 - (0.99)^10 ≈ 9.6%`. Your end-to-end p99 is now governed by the slowest of 10 independently-misbehaving services — which is a fundamentally different (and worse) distribution than any individual service. This is why teams that instrument each service in isolation and declare "everything looks fine" wake up to a degraded product. The distribution of the maximum of N random variables has a different expectation than any individual variable.

**Averaging averages destroys information.** If your US datacenter serves requests with average latency 30ms and your EU datacenter serves requests with average latency 90ms, and US handles 70% of traffic, the global average is not `(30+90)/2 = 60ms`. It's `0.7×30 + 0.3×90 = 48ms`. More critically, if someone reports a 48ms average, you cannot reverse-engineer the per-region performance from it. Always aggregate at the raw event level, never average of averages. Grafana dashboards built on pre-aggregated metrics have silently lied to engineering teams for years.

**Cost spikes are tail events.** Cloud bills don't grow linearly with load — they spike when you hit autoscaling thresholds, reserved capacity limits, or cold-start cascades. These are tail events in your traffic distribution. If you don't model your traffic as a distribution (Poisson arrivals are the standard first approximation), you cannot reason about when these spikes occur or how to provision against them.

---

## Mental Model

Think of every operation in your system as a random variable with a distribution, not a fixed cost. Your database query doesn't take 5ms — it samples from a distribution that has mean ~5ms, some variance, and a tail shaped by lock contention, buffer pool pressure, and network jitter.

Your job as a backend engineer is to reason about three things:
1. The **central tendency** — what most requests experience
2. The **spread** — how consistent the experience is
3. The **tail** — what the worst-case users experience, and how frequently

When you internalize this, you stop asking "how fast is this?" and start asking "what does the latency distribution look like under what load, and does the tail stay below my SLA budget?"

---

## Underlying Theory

### Percentiles and the Empirical CDF

A percentile is not a point — it's a statement about the cumulative distribution function (CDF). The p99 latency of 200ms means: 99% of requests complete at or below 200ms. Equivalently, 1% of requests exceed 200ms.

In production, you estimate the CDF empirically from samples. The estimator is: sort N observations, the pth percentile is approximately the `⌈p/100 × N⌉`-th value. This is why low-cardinality histograms (five buckets: 0-10ms, 10-100ms, 100-500ms, ...) lose tail information — you're compressing the CDF into coarse approximations and cannot reconstruct the original distribution.

Use HDR Histogram or t-Digest for accurate streaming percentile computation at production scale. Both maintain the CDF approximation incrementally without storing all raw samples.

### Little's Law

The most important equation in backend engineering: **L = λW**

- L = average number of requests in the system (concurrency)
- λ = average arrival rate (throughput, in req/s)
- W = average time a request spends in the system (latency)

This is an identity — it holds for any stable system regardless of the arrival distribution, service time distribution, or scheduling discipline. No assumptions required.

Implications:
- If your latency doubles and your arrival rate stays constant, your concurrency doubles. Your thread pool, connection pool, and memory usage all double.
- If you want to support 1000 req/s with 50ms latency, you need to sustain L = 1000 × 0.05 = 50 concurrent requests in the system. Your connection pool minimum is 50.
- Conversely, if your connection pool is capped at 20 and your downstream DB latency climbs to 500ms, you can only sustain λ = L/W = 20/0.5 = 40 req/s before requests queue. This is how a slow database cascades into a timeout storm upstream.

### The Inspection Paradox

Why does it feel like you always arrive at the bus stop just after the bus left, even when buses run on a known schedule? Because the probability of arriving during a long interval is proportional to the length of that interval.

For systems: if your background job runs every 10 minutes but occasionally takes 45 minutes, then a random observer (a request, a health check, a monitoring probe) is much more likely to catch the system mid-long-run than mid-short-run. The average runtime you observe via random sampling is longer than the time-average runtime. This is why your monitoring dashboards consistently report higher latencies than your benchmarks — you're sampling with a bias toward longer events.

### Poisson Arrivals and the Memoryless Property

HTTP request arrivals to a web service are commonly modeled as a Poisson process, where the number of arrivals in any time interval t follows:

```
P(N=k) = (λt)^k × e^(-λt) / k!
```

The inter-arrival times are exponentially distributed with mean 1/λ. The key property: the exponential distribution is memoryless — given that you've been waiting 5 seconds for a request, the expected additional wait time is still 1/λ. No "it's been a while, one must be due" reasoning applies.

This has a direct consequence for retry strategies: exponential backoff with jitter is correct not because it "feels right" but because under Poisson arrivals, any fixed-interval retry strategy creates synchronized retry waves (thundering herd). Jitter decorrelates the retries and prevents them from clustering at the same phase of the arrival process.

### The Birthday Paradox in Load Balancing

With simple random load balancing across N backend instances, if you route M requests uniformly, what's the expected maximum load on any single instance?

The birthday paradox tells you: with N = 365 "days" (instances) and M people (requests), collisions (hot spots) happen much sooner than intuition suggests. For N instances, random load balancing achieves maximum load of approximately `(ln N / ln ln N)` times the average load. For N = 100 instances, that's roughly 2.3× more load on the hottest instance than average.

The **Power of Two Choices** algorithm solves this: instead of routing each request to a random instance, pick two random instances and route to the less-loaded one. This reduces maximum load from O(log N / log log N) to O(log log N) — an exponential improvement in tail load balance. This is why Nginx, HAProxy, and Envoy all implement least-connections or P2C variants rather than pure random.

### Reservoir Sampling

You have a stream of N requests and want to sample exactly k uniformly at random without knowing N in advance (and without storing everything). Algorithm R: maintain a reservoir of k items. For each new item at position i > k, include it in the reservoir with probability k/i, replacing a uniformly random existing item.

This gives you a true uniform random sample over the entire stream. Used in distributed tracing systems where you want to sample 1% of traces without a priori knowing how many traces will arrive. The alternative — sampling the first 1% of arrivals — is biased toward early traffic, which is often non-representative (cold cache, JIT warmup, pre-traffic-ramp patterns).

### Probabilistic Data Structures

Three structures every backend engineer should understand:

**Bloom Filter**: A space-efficient probabilistic set. Supports insert and membership test. False positives possible (says "maybe in set" when not), false negatives impossible (never says "not in set" when it is). Uses k hash functions over a bit array of size m. False positive rate ≈ `(1 - e^(-kn/m))^k` where n is the number of inserted elements. Used in Cassandra's SSTable lookups to avoid disk reads for keys that definitely don't exist, in distributed caches to avoid cache penetration attacks, in Chrome's Safe Browsing to locally check URLs before network lookups.

**HyperLogLog**: Estimates the cardinality of a set (number of distinct elements) using O(log log N) space. Based on the observation that for uniformly hashed values, the position of the first `1` bit follows a geometric distribution related to cardinality. Redis's `PFCOUNT` uses this. Accurate to ±1.04/√m where m is the number of registers. With 1.5KB of memory you can count distinct elements in a set of billions with <1% error.

**Count-Min Sketch**: Estimates frequency of arbitrary elements in a stream using a 2D array of counters with d hash functions. Each query returns an overestimate — never underestimates. Used for rate limiting (count requests per IP without storing every IP), heavy hitter detection (find the top-K most frequent API callers), and stream analytics. Error bounded by ε with probability 1 - δ where the sketch dimensions are O(1/ε × log(1/δ)).

### Confidence Intervals and A/B Testing

When you run a load test and see p99 = 180ms, that's a sample statistic, not a population parameter. The true p99 has uncertainty bounded by your sample size. Reporting "our p99 is 180ms" without a confidence interval is epistemically incomplete.

For A/B tests: two services with different measured p99s are not necessarily different. Use a Mann-Whitney U test (non-parametric, doesn't assume normality) to determine if the latency distributions are statistically distinguishable. Never compare means for latency data — the distributions are right-skewed (bounded at 0, with a long tail), violating the normality assumption underlying t-tests.

The minimum detectable effect for a latency test: if you want to detect a 10ms change in p99 with 80% power and 5% significance level, you need sample sizes in the thousands, not dozens. Most ad-hoc performance comparisons — "I ran it 10 times and it was faster" — have insufficient power to detect anything except large effects.

---

## Complexity Analysis

- Percentile computation from sorted array: O(1) lookup, O(N log N) sort
- Streaming percentile with t-Digest: O(log N) insert, O(1) query, O(compression_factor) space
- Bloom filter insert/query: O(k) where k is the number of hash functions (typically 7–15), effectively O(1)
- HyperLogLog cardinality estimate: O(1) amortized
- Count-Min Sketch update/query: O(d) where d is the number of hash functions (typically 3–5), effectively O(1)
- Reservoir sampling: O(1) per element, O(k) space

The algorithmic complexity of these operations is uniformly negligible. The complexity is in choosing which structure preserves the information you actually need.

---

## Benchmark

Bloom filter lookups at 100M QPS per core. HyperLogLog counts billions of distinct elements with 1.5KB memory and 0.8% error. Count-Min Sketch rate-limits at line speed.

The performance cost of probabilistic reasoning is near-zero. The correctness cost of ignoring it is measured in outages.

---

## Key Takeaways

1. Latency is a distribution, not a number. Report percentiles. The mean is almost never the metric you care about for user-facing SLAs.

2. Little's Law (L = λW) is an identity. Use it to sanity-check your capacity planning, thread pool sizing, and connection pool limits. If the numbers don't add up, something in your model is wrong.

3. The p99 of a composed system is worse than the p99 of any individual component. Tail latencies compound; they don't average.

4. Exponential backoff with jitter isn't folklore — it's the correct response to Poisson arrivals. Coordinated retries create correlated load spikes.

5. Power of Two Choices eliminates hot spots from random load balancing at minimal implementation cost. Prefer P2C over round-robin when backend instance performance varies.

6. Bloom filters, HyperLogLog, and Count-Min Sketch solve specific problems (membership, cardinality, frequency) with O(1) overhead and bounded error. Know which one fits before reaching for a Redis SET or a SQL COUNT(DISTINCT).

7. Don't compare performance results without considering statistical significance. Mann-Whitney U test for latency distributions. Account for sample size before claiming anything is "faster."

---

## Related Modules

- `../04-queueing-theory.md` — M/M/1 and M/M/c queues, Erlang C formula for thread pool sizing, utilization bounds
- `../../09-performance-engineering/02-latency-analysis.md` — latency profiling methodology, HDR Histogram, flame graphs
- `../01-big-o-and-system-reasoning.md` — algorithmic complexity as the foundation for capacity math