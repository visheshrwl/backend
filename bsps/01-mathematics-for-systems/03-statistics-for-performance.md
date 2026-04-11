# 03-statistics-for-performance

## Problem

Every benchmark you have ever run is wrong. Not imprecise — wrong. The question is whether it is wrong in a way that changes the decision you are making.

The canonical performance engineering workflow: write code, run a benchmark, observe a number, compare it to a previous number, declare victory or investigate. This workflow has a flaw at every step. The benchmark excludes the latency its own measurement introduces. The number is a point estimate of a distribution you haven't characterized. The comparison ignores whether the difference is statistically distinguishable from noise. The decision is made on evidence that, in any other quantitative discipline, would not be considered evidence at all.

This isn't pedantry. Benchmark-driven regressions ship to production every week. A/B tests declare winners on effects that are within the noise floor. Capacity plans are built on p50 latency when SLAs are defined on p99. Teams spend weeks optimizing a hot path that Amdahl's Law would have told them in five minutes could not meaningfully improve end-to-end latency.

The discipline of statistics for performance is not about making benchmarking harder. It is about making the conclusions you draw from benchmarks trustworthy.

---

## Why It Matters (Latency, Throughput, Cost)

**Coordinated omission invalidates most benchmark results.** This is the most important concept in performance measurement that most engineers have never heard of. When a benchmarking tool sends the next request only after the previous one completes, it systematically excludes the latency of requests that would have arrived during a slow response. Under load, a service that occasionally takes 2 seconds to respond will cause subsequent arrivals to queue. A correct benchmark measures the latency those queued requests experienced. Most benchmarks — wrk, ab, locust in its default mode — don't. They measure only the requests they actually sent, which is a biased sample that excludes the worst tail behavior by construction.

Gil Tene (Azul Systems) named this problem. The fix is to send requests on a fixed schedule regardless of whether the previous request has returned, and to attribute the latency of a response to the time since it was *scheduled*, not since it was *sent*. HDR Histogram's `HdrHistogram_bench` and Gatling's injection profiles implement this correctly. `wrk2` implements this correctly. Most others do not.

If your benchmark does not implement coordinated-omission correction, its p99 and p999 numbers are fiction — consistently and systematically lower than reality under any load where the tail matters.

**The regression detection problem.** Your CI pipeline runs a benchmark before and after a change and compares the results. How different is different enough to call a regression? If you use a fixed threshold ("flag if p99 increases by more than 5%"), you will have false positives on noisy benchmarks and false negatives on stealthy degradations. The correct approach is to treat regression detection as a hypothesis test: given two samples from a latency distribution, is there sufficient evidence that the distributions are different? This requires knowing the sample size required to detect the effect size you care about, and using the right test for non-normal data.

**Capacity planning on the wrong percentile.** SLAs are written on tail latency. Capacity plans are built on average throughput. These two facts are in permanent tension. A service can comfortably handle its average load at p50 latency while simultaneously violating its SLA on p99 — because p99 latency under load grows faster than linearly with utilization (see Queueing Theory, module 04). Building capacity plans from average-load benchmarks at comfortable utilization will underprovision you for the load spikes that trigger your on-call rotation.

---

## Mental Model

Measure distributions, not numbers. Compare distributions, not numbers. Make decisions from distributions, not numbers.

Every performance measurement is a sample from a distribution. The sample statistic (mean, p99, max) is an estimator of the true population parameter. Estimators have uncertainty. That uncertainty is quantifiable. Ignoring it doesn't make it go away — it just means your decisions are made on false confidence.

A benchmark that doesn't characterize its variance is not a benchmark. It is an anecdote.

---

## Underlying Theory

### Latency Distributions Are Not Normal

The Gaussian distribution is the right model for measurements that are the sum of many independent random variables with finite variance (Central Limit Theorem). Network latency is not this. Latency is bounded below by physics (the speed of light, memory access times) and unbounded above by queuing, GC pauses, OS scheduling preemption, lock contention, and page faults. The distribution is right-skewed with a heavy tail.

The **log-normal distribution** is a better first approximation for latency: if `X` is log-normally distributed, then `log(X)` is normally distributed. This arises naturally when the total latency is a product of many multiplicative factors — each hop adds a percentage overhead rather than a fixed overhead. Log-normal distributions have the property that the mean is substantially larger than the median, and the tail is much heavier than a Gaussian tail.

The practical implication: do not use statistical tests that assume normality (t-tests, ANOVA) for latency data. They will give you wrong p-values on the wrong test statistic. Use non-parametric tests instead (Mann-Whitney U, Kolmogorov-Smirnov). These tests make no distributional assumptions — they work on ranks and empirical CDFs respectively.

More dangerously, latency distributions are frequently **bimodal** — fast path executions in one mode, slow path executions (cache miss, lock contention, GC pause) in another. A bimodal distribution with modes at 5ms and 200ms has a mean around 50ms that occurs in neither mode. Reporting the mean of a bimodal distribution is not just unhelpful — it is actively misleading.

Always plot the full distribution. HDR Histogram outputs a log-linear plot that makes bimodality visible. If you're not visualizing the distribution, you are probably missing structure in your data.

### The Coordinated Omission Problem — Formally

Let requests arrive according to a Poisson process with rate λ. Let service time S have distribution F_S. Under load ρ = λ × E[S] near 1, the queue builds during high-service-time periods.

A traditional benchmark that sends the next request immediately after the previous completes observes service time S directly — not the sojourn time W = S + queue wait. The difference is:

E[W] = E[S] + ρ × E[S] / (1 - ρ)       [M/M/1 mean sojourn]

At ρ = 0.9, E[W] = 10 × E[S]. The benchmark is underreporting mean latency by a factor of 10 at 90% utilization. At the tail, the discrepancy is worse — the p99 of W is dominated by queueing delay, which the traditional benchmark never observes because it never generates a queue.

The correct benchmark: send requests at fixed intervals of `1/λ` regardless of completion, record the timestamp each request was *scheduled*, compute latency as `completion_time - scheduled_time`. Now a 2-second response that blocked 50 subsequent arrivals correctly reports 2+ seconds of latency for each of those arrivals.

### Bootstrapping for Confidence Intervals

You have N latency samples and want a 95% confidence interval for the p99. The p99 is a quantile — its sampling distribution is not Gaussian, and the usual formula (mean ± 1.96 × SE) doesn't apply.

Bootstrapping: resample your N observations with replacement B times (B = 10,000 is standard), compute the p99 of each bootstrap sample, take the 2.5th and 97.5th percentiles of those B values as your confidence interval. No distributional assumptions. Correct for any statistic, any sample size.

```python
import numpy as np

def bootstrap_ci(data, statistic, B=10000, ci=0.95):
    n = len(data)
    bootstrap_stats = [
        statistic(np.random.choice(data, size=n, replace=True))
        for _ in range(B)
    ]
    alpha = (1 - ci) / 2
    return np.quantile(bootstrap_stats, [alpha, 1 - alpha])

samples = np.array([...])  # your latency measurements in ms
lo, hi = bootstrap_ci(samples, lambda x: np.percentile(x, 99))
print(f"p99: {np.percentile(samples, 99):.1f}ms  95% CI: [{lo:.1f}, {hi:.1f}]ms")
```

If your p99 is 180ms with a 95% CI of [120ms, 290ms], you cannot claim with any confidence that a change that moved p99 from 180ms to 160ms is a real improvement. The confidence intervals overlap completely. You need more samples or a larger effect.

The minimum sample size for p99 estimation with ±10% relative error at 95% confidence is approximately `4750 / (1 - 0.99) × (1/0.99)` ≈ 476,000 samples, by the formula for quantile estimation. Most microbenchmarks run far fewer iterations than this and report p99 as if it were a precise measurement.

### Amdahl's Law and Its Discontents

Amdahl's Law: if a fraction `p` of a system is parallelizable and `(1-p)` is serial, the maximum speedup from N processors is:

S(N) = 1 / ((1 - p) + p/N)

As N → ∞, S(N) → 1/(1-p). The serial fraction is the ceiling on speedup regardless of how much parallelism you throw at it. If 5% of your workload is serial (database writes, global locks, single-threaded dispatch), you cannot achieve more than 20× speedup no matter how many cores you add.

The practical use: before spending engineering time parallelizing a workload, profile the serial fraction. If your system is 10% serial, your theoretical maximum speedup is 10×. If you currently run on 4 cores and want to scale to 32, your serial bottleneck will cap you at roughly 6.3× speedup, not 8×. Amdahl's Law gives you the ceiling. You will not exceed it.

### The Universal Scalability Law

Amdahl's Law has a known deficiency: it models parallelism but not coherency overhead. In real distributed systems, scaling adds not just parallelism but also coordination cost. The **Universal Scalability Law (Gunther, 1993)** adds two terms:

S(N) = N / (1 + α(N-1) + βN(N-1))

Where:
- `α` captures contention (serialization, lock waiting) — the Amdahl term
- `β` captures coherency penalty (cache invalidation, distributed consensus, synchronization overhead)

When β > 0, throughput eventually *decreases* with added capacity. This is retrograde scalability — you have seen it when adding more Kafka consumers degraded throughput due to rebalancing overhead, or when adding more shards to a Redis cluster increased cross-shard coordination latency, or when adding more nodes to a Paxos cluster slowed consensus.

Measuring α and β requires fitting the USL to throughput vs. concurrency data — a straightforward nonlinear regression. Once you have them, you can predict the optimal concurrency level (the N that maximizes S(N)) before you over-provision and enter the retrograde regime.

```python
from scipy.optimize import curve_fit
import numpy as np

def usl(N, alpha, beta):
    return N / (1 + alpha * (N - 1) + beta * N * (N - 1))

# Measure throughput at several concurrency levels
N_vals = np.array([1, 2, 4, 8, 16, 32, 64])
throughput = np.array([...])  # measured req/s at each level

# Normalize to throughput at N=1
throughput_normalized = throughput / throughput[0]

(alpha, beta), _ = curve_fit(usl, N_vals, throughput_normalized,
                             bounds=([0, 0], [1, 1]))
print(f"Contention α={alpha:.4f}, Coherency β={beta:.4f}")
N_opt = int(np.sqrt((1 - alpha) / beta))
print(f"Optimal concurrency: {N_opt}")
```

If your system shows β > 0 with non-trivial magnitude, adding capacity is not your solution — reducing coordination overhead is.

### Change Detection: The Right Statistical Test

You have two sets of latency samples — before and after a deployment. How do you determine if the change caused a regression?

**Mann-Whitney U test (Wilcoxon rank-sum):** Tests whether samples from distribution A tend to be smaller or larger than samples from distribution B, with no distributional assumptions. Null hypothesis: the two samples are drawn from the same distribution. P-value < 0.05 rejects the null — statistically significant difference detected.

```python
from scipy import stats

before = np.array([...])  # latency samples before deployment
after  = np.array([...])  # latency samples after deployment

statistic, p_value = stats.mannwhitneyu(before, after, alternative='two-sided')
print(f"p-value: {p_value:.4f}")
if p_value < 0.05:
    print("Statistically significant difference detected")
```

**Kolmogorov-Smirnov test:** Tests whether two empirical CDFs are drawn from the same distribution. More sensitive to differences in the tail than Mann-Whitney, which is why it's preferable for latency regression detection where tail behavior matters most. The KS statistic is the maximum absolute difference between the two CDFs.

```python
ks_stat, p_value = stats.ks_2samp(before, after)
```

**Effect size — Cohen's d for non-parametric data (rank-biserial correlation):** Statistical significance does not imply practical significance. A change that is statistically significant with p < 0.001 may have an effect size that is irrelevant (e.g., 0.5ms improvement at p99). Always report effect size alongside p-value. For Mann-Whitney, the effect size is the rank-biserial correlation r = 1 - (2U) / (n₁ × n₂). |r| < 0.1 is negligible; |r| > 0.5 is large.

**The multiple comparisons problem:** If your CI pipeline tests 20 metrics (p50, p75, p90, p95, p99, p999 of each of several services) against p < 0.05, you expect 1 false positive per run by chance. Bonferroni correction: divide your significance threshold by the number of comparisons. For 20 comparisons, use p < 0.0025. Alternatively, use Benjamini-Hochberg procedure to control the false discovery rate — less conservative than Bonferroni and more appropriate when many tests are genuinely null.

### Profiler Perturbation and the Observer Effect

Sampling profilers (perf, async-profiler, pprof) observe the call stack at fixed intervals (typically 100Hz to 1000Hz). The overhead is low but nonzero — roughly 1-5% on most workloads. The sample is biased toward long-running frames, exactly as described by the inspection paradox (module 02). Short, fast functions that are called millions of times may be profiling blind spots if they consistently complete between samples.

Instrumentation profilers (adding timers to every function call) add overhead proportional to call frequency. For a function called 10M times/second, even a 100ns timing instrumentation adds 1 second of overhead per second of wall time — 100% overhead. This changes the performance characteristics you're trying to measure.

The correct mental model: a profiler that shows you function X at 30% of samples does not mean X consumes 30% of wall time — it means X was on the stack during 30% of samples. If your workload is I/O bound and the profiler samples during I/O waits, I/O appears expensive even if the I/O is exactly where you want to be spending time. Always distinguish between CPU profiling (on-CPU samples) and wall-clock profiling (all samples including blocking). Async-profiler supports both modes explicitly.

### Statistical Process Control in Production

SPC (Shewhart, 1920s) provides a framework for distinguishing **common cause variation** (inherent noise in the system) from **special cause variation** (a real change in the process). Applied to production latency:

1. Establish a baseline: collect latency samples during normal operation, compute mean μ and standard deviation σ of a suitable metric.
2. Draw control limits at μ ± 3σ. For normal data, 99.73% of observations fall within these limits.
3. In production: alert when an observation falls outside the control limits (special cause), or when there are 8 consecutive observations on the same side of the mean (Western Electric rules — indicates a shift in the process mean).

For non-normal latency distributions, apply SPC to the log-transformed values (which are approximately normal for log-normal distributions), or use CUSUM (cumulative sum) control charts that are sensitive to sustained shifts rather than single-point excursions.

The advantage over static thresholds: SPC adapts to the system's natural variability. A system that normally varies ±30ms on p99 should have wider alert bounds than one that varies ±2ms — static thresholds applied uniformly cause alert fatigue on the former and miss real regressions on the latter.

### Survivor Bias in Distributed Traces

When you sample 1% of traces for collection, you are not observing a representative sample of your traffic — unless you sample uniformly at the entry point. In practice:

- Error traces are often sampled at 100% (you want to see every error)
- Slow traces are sometimes sampled at higher rates (tail-based sampling in Jaeger/Tempo)
- Fast, successful traces are sampled at lower rates

The resulting corpus of collected traces is heavily biased toward the bad cases. Latency statistics computed from this corpus will be systematically higher than the actual production distribution. Cardinality analysis (unique users, unique endpoints) on a biased sample will undercount dimensions that are correlated with fast, successful requests.

Head-based sampling (decision made at the entry point, before the trace completes) is unbiased but blind to outcome — you sample slow traces at the same rate as fast ones. Tail-based sampling is outcome-aware but introduces selection bias for analytics. Know which your tracing infrastructure uses and correct your interpretation accordingly.

---

## Complexity Analysis

- Bootstrap CI computation: O(B × N) where B = bootstrap iterations, N = sample size. For B=10,000, N=10,000: 10⁸ operations. Takes ~2-5 seconds in Python, ~50ms in NumPy with vectorization.
- Mann-Whitney U test: O(N log N) via sorting
- KS test: O(N log N)
- USL curve fitting: O(iterations × evaluations) — converges in milliseconds for typical data sizes
- SPC online computation: O(1) per observation with Welford's algorithm for rolling σ

None of these are performance bottlenecks. The computation is offline analysis, not on the critical path.

---

## Benchmark

A correct benchmark for a single function requires: warmup (JIT compilation, instruction cache warming), sufficient iterations to achieve narrow confidence intervals on the target percentile, coordinated-omission correction if modeling production load, and a statistical comparison against baseline using an appropriate hypothesis test. Under these constraints, the minimum viable benchmark for a p99 comparison with 80% power to detect a 10% effect is on the order of 50,000-500,000 iterations depending on the distribution's variance.

Any benchmark with fewer than a few thousand iterations should not be used to make claims about tail latency. Fewer than a hundred iterations should not be used to make claims about any percentile above p50.

---

## Key Takeaways

1. Coordinated omission is not a benchmark artifact — it is the fundamental difference between measuring service time and sojourn time. Most benchmarking tools measure service time. Your SLA is governed by sojourn time. Use tools that implement fixed-rate injection (wrk2, Gatling) and attribute latency to scheduled time, not sent time.

2. Latency distributions are not Gaussian. They are right-skewed, frequently bimodal, and bounded below. Never use t-tests or ANOVA on latency data. Use Mann-Whitney U or Kolmogorov-Smirnov for distribution comparison.

3. Report confidence intervals alongside point estimates. A p99 of 180ms without error bars is not a measurement — it is an observation. Bootstrap CIs require no distributional assumptions and are trivial to implement.

4. Amdahl's Law gives you the ceiling on parallelization speedup before you begin. If 10% of your workload is serial, stop at 10×. The Universal Scalability Law additionally models coherency overhead — fit it to your throughput vs. concurrency data to find the optimal concurrency level before you accidentally enter the retrograde regime.

5. Regression detection requires hypothesis testing with appropriate power analysis. The multiple comparisons problem means that testing 20 metrics at p < 0.05 yields one false positive per CI run. Apply Bonferroni or Benjamini-Hochberg correction.

6. Statistical significance is not practical significance. Always report effect size. A 0.5ms improvement in p99 that is statistically significant at p < 0.001 is irrelevant if your SLA is 100ms.

7. Sampling profilers are biased toward long-running frames by the inspection paradox. Instrumentation profilers perturb the workload proportionally to call frequency. Know which you're using, and interpret results accordingly.

8. Trace sampling introduces survivor bias. Latency statistics computed from a tail-sampled corpus are not representative of the production distribution. Know your sampling strategy and correct for its biases.

---

## Related Modules

- `../02-probability-for-engineers.md` — distributions, the inspection paradox, and Little's Law as foundations for this module
- `../04-queueing-theory.md` — the M/M/1 sojourn time formula that explains why coordinated omission matters
- `../../09-performance-engineering/02-latency-analysis.md` — applying these methods to production profiling workflows
- `../01-big-o-and-system-reasoning.md` — Amdahl's Law as a complexity bound on parallelism