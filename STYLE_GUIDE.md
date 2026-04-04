# BSPS Style Guide

## Mandatory Template Structure

Every concept file follows this exact section order. Do not reorder sections.

```
# Title

## Problem
## Why it matters (latency, throughput, cost)
## Mental Model
## Underlying Theory (OS / CN / DSA / Math linkage)
## Naive Approach
## Why it fails at scale
## Optimized Approach
## Complexity Analysis (time, space)
## Benchmark (p50, p99, CPU, memory)
## Observability (metrics, tracing, logs)
## Multi-language Implementation (Python, Go, Node.js)
## Trade-offs
## Failure Modes
## When NOT to use
## Lab
## Key Takeaways
```

**Section heading style:** Use `##` for top-level sections, `###` for subsections within a section. Do not skip levels.

---

## Complexity Analysis Format

Always use Big-O notation. Always specify what N represents.

```markdown
## Complexity Analysis

| Operation | Time | Space | Notes |
|-----------|------|-------|-------|
| Cache get | O(1) | O(1) | Hash map lookup |
| Cache put | O(1) | O(1) amortized | Eviction is O(1) with doubly linked list |
| Cache scan | O(N) | O(1) | N = cache size |

Where N = number of items in the cache.
```

When comparing naive vs optimized:

```markdown
N+1 approach: O(N) queries, O(N×M) total rows fetched
JOIN approach: O(1) queries, O(N×M) total rows fetched
Speedup: O(N) → O(1) query overhead
```

Always include the constant factors when they matter:
```
O(1) cache lookup ≈ 0.5ms network RTT (Redis, same AZ)
O(1) DB query ≈ 5–15ms (parse + plan + execute + network)
```

---

## Benchmark Format

Use ASCII tables for benchmark results. Always include setup context.

```markdown
## Benchmark

Setup: Python 3.11, PostgreSQL 15, Redis 7, 8-core Intel i7, 16GB RAM,
       Ubuntu 22.04, connections over localhost (RTT ≈ 0.05ms).

┌─────────────────┬────────┬────────┬──────────┬──────────┐
│ Approach        │  p50   │  p99   │ Queries  │ CPU %    │
├─────────────────┼────────┼────────┼──────────┼──────────┤
│ Naive           │  52ms  │ 115ms  │ 101      │ 18%      │
│ Optimized       │   2ms  │   4ms  │ 1        │  2%      │
└─────────────────┴────────┴────────┴──────────┴──────────┘

Methodology: 1,000 requests, 10 concurrent workers, warm DB buffer pool.
             Measured with time.perf_counter() around the query logic.
             p50 = median, p99 = 99th percentile of 1,000 measurements.
```

Required columns (include as many as apply): p50, p99, throughput (req/s), memory, CPU.

Optional columns: p95, max, min, stddev.

---

## ASCII Diagram Conventions

Use ASCII diagrams for:
- Architecture flows
- Data structure layouts
- State machines
- Before/after comparisons

**Box drawing characters (preferred):**
```
┌──────┐   ┌──────┐
│ box1 │──►│ box2 │
└──────┘   └──────┘

Single line borders: ─ │ ┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼
Double line borders: ═ ║ ╔ ╗ ╚ ╝ ╠ ╣ ╦ ╩ ╬
Arrows: → ← ↑ ↓ ↔ ↕ ► ◄
```

**Fallback (when box-drawing chars are problematic):**
```
+-------+   +-------+
| box1  |-->| box2  |
+-------+   +-------+
```

**Label alignment:** left-align labels inside boxes. Right-align numbers.

**Diagram width:** max 72 characters to fit in most terminals without wrapping.

---

## Code Style

### Python

- Follow PEP 8 (4-space indent, 79-char line limit for code, 99 for comments)
- Use type hints on all function signatures
- Use f-strings for string formatting
- Use `async`/`await` for I/O-bound code examples
- Use `dataclasses` for data holders
- Docstrings on classes; inline comments on non-obvious lines only

```python
# Good
async def get_user(user_id: int, pool: asyncpg.Pool) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, name, email FROM users WHERE id = $1", user_id
        )
        return dict(row) if row else None

# Bad (no types, no async, string concat)
def get_user(id, pool):
    conn = pool.getconn()
    row = conn.execute("SELECT * FROM users WHERE id = " + str(id))
    pool.putconn(conn)
    return row
```

### Go

- Follow `gofmt` formatting (run before committing)
- Use named return values only when the function is complex enough to benefit
- Handle all errors explicitly — never `_` an error from a significant operation
- Use `context.Context` as the first argument of any function that does I/O
- Prefer table-driven tests

```go
// Good
func GetUser(ctx context.Context, pool *pgxpool.Pool, userID int) (*User, error) {
    row := pool.QueryRow(ctx,
        "SELECT id, name, email FROM users WHERE id = $1", userID)
    var u User
    if err := row.Scan(&u.ID, &u.Name, &u.Email); err != nil {
        if errors.Is(err, pgx.ErrNoRows) {
            return nil, nil
        }
        return nil, fmt.Errorf("GetUser: %w", err)
    }
    return &u, nil
}

// Bad
func GetUser(id int) User {
    row := db.QueryRow("SELECT * FROM users WHERE id = ?", id)
    var u User
    row.Scan(&u.ID, &u.Name) // ignoring error
    return u
}
```

### Node.js

- Use `async`/`await` — no raw callbacks in new code
- Use `const` by default, `let` only when reassignment is needed
- Use destructuring for cleaner code
- Always handle promise rejections (`.catch()` or `try/catch`)
- Use ESM (`import`/`export`) for new modules

```javascript
// Good
async function getUser(userId, pool) {
    const { rows } = await pool.query(
        'SELECT id, name, email FROM users WHERE id = $1',
        [userId]
    );
    return rows[0] ?? null;
}

// Bad
function getUser(userId, pool, callback) {
    pool.query("SELECT * FROM users WHERE id = " + userId, (err, res) => {
        callback(err, res.rows[0]);
    });
}
```

---

## Cross-Reference Format

When linking to other modules, use relative paths from the current file's location:

```markdown
See `../../bsps/03-operating-systems/01-processes-and-threads.md` for OS thread internals.
See `../02-connection-pooling.md` for how this pattern affects pool sizing.
```

At the end of every module, include a `## Related Modules` section listing 3–5 direct dependencies.

---

## Writing Failure Modes

Failure modes must be **concrete scenarios**, not vague warnings.

**Bad:**
```markdown
## Failure Modes
- Cache can become inconsistent
- Pool might run out of connections
```

**Good:**
```markdown
## Failure Modes

**1. Cache stampede on key expiry:**
When a hot cache key expires, N concurrent requests all see a cache miss
simultaneously and all query the database. With N=1000 requests/second
and a key that expires every 60 seconds, this fires 1000 DB queries in
<1ms — spiking DB CPU from 5% to 100%.
Mitigation: probabilistic early revalidation (PER), or distributed lock.

**2. Pool exhaustion cascade:**
Traffic spike → all 20 connections in use → requests queue → queue
grows → timeouts fire → 500 errors → client retries → more load.
Mitigation: circuit breaker, exponential backoff on retries.
```

---

## Writing "When NOT to Use"

This section must be honest and specific. Every pattern has limits.

**Required structure:**
```markdown
## When NOT to Use

**When [specific condition]:**
[Why the pattern fails or is overkill in this case]
[What to use instead]

**When [another condition]:**
...
```

---

## Markdown Formatting Rules

- Use **bold** for key terms on first introduction
- Use `code formatting` for: file paths, function names, SQL keywords, variable names, config keys
- Use `>` blockquotes for important callouts
- Use numbered lists for sequential steps
- Use bullet lists for unordered items
- Tables must have header rows and alignment separators
- Code blocks must specify the language: ` ```python `, ` ```go `, ` ```javascript `, ` ```sql `, ` ```bash `
- Maximum heading depth: `####` (4 levels). If you need 5, restructure.

---

## File Naming

- All files: lowercase with hyphens, `.md` extension
- Numbered files: zero-padded two-digit prefix (`01-`, `02-`, etc.)
- No spaces in filenames
- Descriptive: `01-n-plus-one-query-problem.md` not `n1.md`
