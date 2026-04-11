# 05-numerical-stability

## Problem

In 1996, the Ariane 5 rocket self-destructed 37 seconds after launch. The cause: a 64-bit floating-point number representing horizontal velocity was converted to a 16-bit signed integer. The value exceeded 32,767. The conversion overflowed. The guidance system interpreted the garbage value as flight data, concluded the rocket was off-course, and issued a self-destruct command.

The software was reused from Ariane 4 without modification. It had been correct for Ariane 4 — which flew a different trajectory where the velocity never exceeded the 16-bit range. No one had checked whether the assumption still held.

Your backend systems are full of these assumptions. A payment total stored as a `float`. A timestamp arithmetic operation that overflows in 2038. A running sum accumulated over millions of records where rounding error has silently compounded to a value that is wrong by thousands. These failures don't announce themselves. They accumulate quietly until something downstream notices the number doesn't make sense — or doesn't.

---

## Why It Matters (Latency, Throughput, Cost)

**Financial systems.** A single incorrect floating-point operation in a payment pipeline can create discrepancies that don't surface until reconciliation — sometimes days later, sometimes never. The correct representation for money is not `float` or `double`. It is a fixed-point decimal type (SQL `DECIMAL(19, 4)`, Java `BigDecimal`, Python `decimal.Decimal`). Every financial system that uses IEEE 754 floating-point for currency is wrong, even if it hasn't been caught yet.

**Aggregation at scale.** Summing one million `float32` values naively introduces rounding errors proportional to N × machine epsilon (≈ 1.19 × 10⁻⁷). For N = 10⁶, that's roughly 0.119 units of accumulated error. For analytics systems that aggregate billing data, usage metrics, or revenue totals, this error is not theoretical — it produces numbers that don't match between different aggregation paths, creating a class of incident that is uniquely difficult to debug because everything looks correct locally.

**Distributed disagreement.** When two services independently compute the same floating-point value and compare them for equality, they will occasionally disagree — not because of bugs, but because floating-point arithmetic is not associative and the order of operations may differ across services. Two nodes independently summing the same dataset in different orders can produce results that differ in the last few bits, causing consensus checks to fail on data that is semantically identical.

---

## Mental Model

IEEE 754 floating-point numbers do not represent real numbers — they represent the closest representable value in a discrete, finite set. The `double` type has 2⁶⁴ possible bit patterns, which is a large but finite set. Most real numbers are not in this set. What you are actually computing is an approximation — and the approximation error accumulates with every operation.

Think of it this way: the real number line is continuous and infinite. Your floating-point system is a very dense but ultimately finite ruler with unevenly spaced tick marks. Near zero, the tick marks are extremely close together (high precision). Near 10¹⁵, the tick marks are spaced 0.125 apart — meaning integers larger than 2⁵³ cannot all be represented exactly as `double`. This non-uniform spacing is the root of most numerical instability bugs.

The discipline of numerical stability is the art of designing computations so that the accumulated approximation error stays within acceptable bounds.

---

## Underlying Theory

### IEEE 754 and Machine Epsilon

A `double` (64-bit float) has:
- 1 sign bit
- 11 exponent bits (biased by 1023)
- 52 explicit mantissa bits (with an implicit leading 1)

The value represented is: `(-1)^sign × 2^(exponent-1023) × 1.mantissa`

Machine epsilon (ε_mach) for `double` is 2⁻⁵² ≈ 2.22 × 10⁻¹⁶. This is the smallest value such that `1.0 + ε_mach ≠ 1.0`. It bounds the relative rounding error of a single operation: the computed result of any arithmetic operation fl(a ⊙ b) satisfies:

fl(a ⊙ b) = (a ⊙ b)(1 + δ),  |δ| ≤ ε_mach


Each operation introduces at most ε_mach relative error. The problem is that errors compose. After N operations, the accumulated error can be O(N × ε_mach) in the worst case — and much worse if catastrophic cancellation occurs.

The canonical demonstration every engineer should internalize:

```python
>>> 0.1 + 0.2
0.30000000000000004
>>> 0.1 + 0.2 == 0.3
False
```

`0.1` is not representable exactly in binary. The nearest `double` to 0.1 is `0.1000000000000000055511151231257827021181583404541015625`. The rounding errors in representing 0.1 and 0.2 don't cancel when added — they partially compound. The result is not `0.3` but the nearest `double` to `0.3`, which is a different number.

This is not a Python bug. It is not a language-specific quirk. It is the behavior of IEEE 754 double-precision arithmetic in every language on every platform.

### Catastrophic Cancellation

Catastrophic cancellation occurs when you subtract two nearly equal quantities, causing leading significant digits to cancel and leaving only noisy low-order bits as the result.

Example: computing `f(x) = 1 - cos(x)` for small x.

```python
x = 1e-8
# Naive:
result = 1 - math.cos(x)  # → 0.0  (completely wrong)

# Stable: use the identity 1 - cos(x) = 2*sin²(x/2)
result = 2 * math.sin(x/2)**2  # → 5e-17 (correct)
```

For x = 10⁻⁸, cos(x) ≈ 1 - 5×10⁻¹⁷. But `double` represents 1.0 and cos(x) with about 16 significant decimal digits. Their difference is in the 17th digit — which doesn't exist in the representation. The subtraction yields exactly 0.0.

This pattern appears in production systems whenever you compute:
- Rate of change over a small time interval: `(v2 - v1) / dt` for small dt
- Relative differences: `(a - b) / b` when a ≈ b  
- Variance: `E[X²] - E[X]²` (never do this — see Welford below)

### Kahan Compensated Summation

Naive summation of N floating-point numbers:

```python
total = 0.0
for x in values:
    total += x
```

Error bound: O(N × ε_mach × |sum|). For a million items, this is roughly 2 × 10⁻¹⁰ times the magnitude of the sum — small but not zero, and larger than necessary.

Kahan summation tracks the lost low-order bits explicitly with a compensation term:

```python
total = 0.0
compensation = 0.0
for x in values:
    y = x - compensation
    t = total + y
    compensation = (t - total) - y
    total = t
```

Error bound: O(ε_mach × |sum|) — independent of N. This is tight enough for most production aggregation. NumPy uses pairwise summation (O(ε_mach × log N × |sum|)) as a compromise between Kahan's overhead and naive summation's accumulation.

The implementation cost: a few extra arithmetic operations per element. The correctness gain: your aggregation pipeline matches between different aggregation orders.

### Welford's Online Algorithm for Variance

The naive formula for variance: `Var[X] = E[X²] - E[X]²`

Never implement this in production. Computing large sums of squares and subtracting is a textbook case of catastrophic cancellation. When all values are clustered near a large mean, `E[X²]` and `E[X]²` are nearly equal large numbers, and their difference is noise.

Welford's algorithm computes mean and variance in a single pass without catastrophic cancellation:

```python
n, mean, M2 = 0, 0.0, 0.0
for x in values:
    n += 1
    delta = x - mean
    mean += delta / n
    delta2 = x - mean
    M2 += delta * delta2
variance = M2 / (n - 1)  # sample variance
```

This is numerically stable, online (constant memory), and correct. It is the standard algorithm in Knuth Vol. 2. Any system computing streaming variance should use this. Anything using `sum_squares - sum**2/n` is wrong.

### Integer Overflow

Floating-point isn't the only source of numerical bugs. Integer overflow is silent in most languages and has caused production incidents at scale.

**The binary search bug (Bloch, 2006):**

```java
// Wrong — overflows when low + high > Integer.MAX_VALUE
int mid = (low + high) / 2;

// Correct
int mid = low + (high - low) / 2;
```

This bug existed in Java's standard library `Arrays.binarySearch()` for nearly a decade. It only manifests for arrays with more than ~10⁹ elements — rare in 2006, less rare now.

**The Y2K38 problem:** Unix timestamps stored as 32-bit signed integers overflow on January 19, 2038 at 03:14:07 UTC. `2^31 - 1 = 2,147,483,647` seconds after the Unix epoch. Any system storing timestamps as `int32` will interpret this as January 13, 1901 — or undefined behavior. MySQL's `TIMESTAMP` type (historically 32-bit) has this problem; `DATETIME` does not. PostgreSQL's `timestamp` is 64-bit and safe. Check your schema.

**Accumulation in counters:** An unsigned 32-bit counter wraps at 4,294,967,295. At 100,000 events/second, this wraps in about 43,000 seconds — 12 hours. Monitoring systems that use 32-bit counters for high-throughput metrics will see the counter reset to zero and interpret it as a massive traffic drop, triggering false alerts.

### The Log-Sum-Exp Trick

Computing probabilities in systems (Bayesian scoring, ranking, recommendation) often involves expressions like:

log(exp(a) + exp(b) + exp(c))

For large values of a, b, c, `exp()` overflows to infinity. For very negative values, `exp()` underflows to 0. The trick: factor out the maximum value.

```python
def log_sum_exp(values):
    m = max(values)
    return m + math.log(sum(math.exp(v - m) for v in values))
```

The largest term becomes `exp(0) = 1`; all other terms are in (0, 1] and never overflow. This is not an ML-specific trick — any backend system computing log-probabilities, scoring functions, or softmax-like operations should use this. The naive implementation will silently produce `-inf` or `nan` for inputs outside the safe range, and these NaN values will propagate through your entire computation.

### NaN Propagation

IEEE 754 defines that any arithmetic operation involving `NaN` produces `NaN`. This seems safe — NaN is at least explicit. But NaN has two dangerous properties:

1. `NaN != NaN` evaluates to `true`. Any equality check, deduplication, or sorting that involves a NaN-contaminated field silently misbehaves. Sorted arrays containing NaN have undefined order. SQL aggregates silently exclude NaN (or propagate it, depending on the database and function).

2. NaN propagates silently through pipelines. A single NaN injected at the beginning of a computation graph will infect every downstream value without raising an exception. You will see the corrupted output at the far end with no traceback to the origin.

Production discipline: validate numeric inputs at system boundaries. Assert that values are finite (`math.isfinite(x)`) before they enter computation pipelines. Reject rather than propagate. The earlier you detect NaN, the cheaper the debugging.

### Money: Use Decimal, Not Float

The rule is absolute: never use `float` or `double` for monetary amounts.

Storing $19.99 as a `double` gives you `19.989999999999998436805981327779591083526611328125`. Round-trip through arithmetic and you may end up with $19.98 or $20.00. For a single transaction, this is invisible. For a billing system processing millions of transactions, the discrepancy surfaces in reconciliation as unexplained differences that grow with volume.

Correct representations:
- **Integer cents** (or smallest currency unit): store 1999 for $19.99. Add, subtract, and compare as integers. Only convert to decimal for display.
- **SQL `DECIMAL(p, s)`**: exact fixed-point arithmetic in the database. `DECIMAL(19, 4)` supports up to $999,999,999,999,999.9999 with 4 decimal places of precision. Use this for all monetary columns.
- **Application layer**: `BigDecimal` in Java/Kotlin, `decimal.Decimal` in Python, `numeric` in Go via a library, Rust's `rust_decimal` crate.

The integer-cents approach is preferable because it uses native integer arithmetic (fast, exact, no rounding) and makes the unit of storage explicit in the type. A field named `price_cents: int64` cannot accidentally be treated as dollars.

---

## Complexity Analysis

- Naive summation: O(N) time, O(1) space, O(N × ε_mach) error
- Kahan summation: O(N) time, O(1) space, O(ε_mach) error
- Pairwise summation: O(N) time, O(log N) space, O(log N × ε_mach) error
- Welford's variance: O(N) time, O(1) space, numerically stable
- Log-sum-exp: O(N) time, O(1) space, no overflow for any finite input
- `BigDecimal` multiplication: O(p²) where p is precision in digits — avoid in hot paths

The asymptotic complexity is identical for stable and unstable algorithms in most cases. The difference is correctness, not speed. Choose the numerically stable version.

---

## Benchmark

Float arithmetic is O(1) and executes in 1-5ns on modern hardware. `BigDecimal` operations are 10-100× slower. For most backend systems this doesn't matter — monetary computations are not on the critical path for latency. For high-frequency financial systems where it does matter, use integer-cents arithmetic (exact and fast) rather than `BigDecimal` (exact but slow).

Kahan summation introduces roughly 2-3× arithmetic operations per element over naive summation — negligible compared to memory bandwidth for large arrays. There is no performance excuse to not use it.

---

## Key Takeaways

1. `float` and `double` do not represent real numbers — they represent the nearest value in a finite, non-uniformly spaced set. `0.1 + 0.2 ≠ 0.3` is not a quirk. It is the expected behavior of IEEE 754 arithmetic on every platform in every language.

2. Never store money as floating-point. Use integer cents or SQL `DECIMAL`. This is a correctness requirement, not a style preference.

3. Catastrophic cancellation destroys precision when subtracting nearly equal values. Recognize the pattern — `E[X²] - E[X]²`, `1 - cos(x)` for small x, `(a - b) / b` when a ≈ b` — and substitute a numerically stable identity.

4. Use Kahan summation or pairwise summation when aggregating large datasets. Naive summation accumulates O(N × ε_mach) error. The implementation cost is trivial.

5. Use Welford's algorithm for streaming mean and variance. It is online, stable, and constant-memory. The two-pass formula is wrong.

6. Check all 32-bit timestamp fields in your schema for Y2K38 exposure. Use `DATETIME` over `TIMESTAMP` in MySQL. Use `int64` for Unix timestamps in application code.

7. NaN propagates silently. Validate inputs as finite at system boundaries. Do not let NaN enter a computation pipeline — it will infect every downstream value without raising an error.

8. The log-sum-exp trick prevents overflow and underflow in any computation involving `exp()` of potentially large or small values. It costs nothing and prevents a class of silent corruption.

---

## Related Modules

- `../04-queueing-theory.md` — numerical precision in arrival rate and utilization calculations
- `../../09-performance-engineering/02-latency-analysis.md` — Kahan summation in latency aggregation pipelines
- `../01-big-o-and-system-reasoning.md` — understanding algorithm correctness independently of complexity class
- `../02-probability-for-engineers.md` — log-space probability arithmetic and the log-sum-exp trick in scoring systems