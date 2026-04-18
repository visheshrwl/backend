# Backend Systems Audit Scorecard

**Document class:** Technical audit instrument
**Intended use:** Point-in-time assessment of a backend service's production readiness across eight operational dimensions. Output is a maturity score per dimension, a weighted total, and a prioritized remediation queue.
**Not a substitute for:** Threat modeling, capacity planning, DR tabletop, or full OWASP ASVS review. This is a triage tool, not a pen test.

---

## 1. Document Control

| Field | Value |
|---|---|
| System / service under review | |
| Environment (prod / stage / canary) | |
| Commit SHA / build number | |
| Audit date | |
| Lead auditor | |
| Second reviewer (required for scores ≥ 4) | |
| Prior audit reference | |
| Distribution | Service owner, EM, on-call rotation |

An audit is only valid against a pinned commit SHA. Scoring a moving target is negligence, not assessment.

---

## 2. Methodology

The audit walks eight dimensions in fixed order. Each dimension is scored on a five-level maturity model (§3). Scores **must** be supported by evidence — command output, dashboard screenshot, code reference (`path/file.go:L123`), runbook link, or ticket ID. **Unsupported scores are recorded as L1 regardless of the auditor's belief.**

The auditor's job is not to be charitable. A service that "should" work fine under load but has never been load-tested is L2, not L4. Optimism is not evidence.

### Evidence hierarchy (strongest first)

1. Reproducible command output or query result captured in this document
2. Dashboard screenshot with timestamp, filtered to the audited commit
3. Production incident postmortem demonstrating the property (positive or negative)
4. Load/chaos test results with methodology
5. Code reference with line numbers
6. Runbook or design doc link
7. Verbal assertion from service owner — **not accepted as sole evidence above L2**

### Audit discipline

- Every command in §5 is expected to be run. If a command cannot be run (access, tooling, environment), the reason is logged and the dimension is capped at L2 for that sub-criterion.
- Findings are recorded at the time of discovery, not reconstructed after the fact.
- Scores are not negotiated with the service owner during the audit. Disputes are logged and resolved in review.

---

## 3. Maturity Model

| Level | Name | Definition |
|---|---|---|
| **L1** | Unknown / absent | Not implemented, or implemented without any operator visibility. Equivalent to "we don't know." |
| **L2** | Ad-hoc | Implemented but with material gaps; configuration is default or guessed; no benchmarking; failure modes untested. |
| **L3** | Deliberate | Implemented with intent; documented configuration; baseline behavior understood; no active monitoring. |
| **L4** | Tuned | Implemented, sized/tuned against measured load; metrics exported; dashboards exist; reviewed in regular cadence. |
| **L5** | Governed | L4 plus SLO-linked alerting, automated regression detection, chaos/load-tested against failure modes, tied to release gating or error budgets. |

Scores are **whole numbers**. A "3.5" is an L3. Maturity models do not interpolate — a half-implemented circuit breaker is a bug, not a half-feature.

---

## 4. Dimensions, Criteria, and Evidence

Each dimension specifies: scope, what each level looks like concretely, required evidence, verification commands, and common failure modes observed in field audits.

---

### 4.1 Query Performance

**Scope:** Database read/write path efficiency. Index utilization, plan stability, absence of anti-patterns (N+1, unbounded scans, missing pagination, `SELECT *` across wide tables, misuse of ORMs producing OR'd disjunctions).

**Level criteria:**

- **L1** — No query logging enabled; no record of `EXPLAIN` for hot queries; unknown slow query distribution.
- **L2** — `log_min_duration_statement` set; known N+1 patterns exist in hot paths; FK columns missing indexes; no query budget in tests.
- **L3** — `pg_stat_statements` reviewed; N+1 eliminated in top-10 endpoints; indexes on FKs and sort/filter columns; bounded result sets enforced.
- **L4** — Query plans reviewed for top 20 statements; prepared statements used for hot paths; per-endpoint query count asserted in test suite; plan regressions investigated.
- **L5** — Automated plan-capture in CI; alerts on plan flips or unexpected seq scans on large tables; query budgets enforced per route; partitioning/sharding path documented for tables >100M rows.

**Required evidence:**

- Output of the verification queries below, captured with timestamp.
- Reference to at least one `EXPLAIN (ANALYZE, BUFFERS)` for a hot endpoint.
- For L4+: link to CI job enforcing query count per endpoint.

**Verification — PostgreSQL:**

```sql
-- Top queries by cumulative time (L3+ should have pg_stat_statements)
SELECT substring(query, 1, 100) AS query,
       calls,
       round(total_exec_time::numeric, 2) AS total_ms,
       round(mean_exec_time::numeric, 2) AS mean_ms,
       rows
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;

-- Unused indexes (candidates for removal; also smell of wrong workload assumption)
SELECT schemaname, relname, indexrelname, idx_scan,
       pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
WHERE idx_scan = 0
  AND indexrelname NOT LIKE 'pg_%'
ORDER BY pg_relation_size(indexrelid) DESC;

-- Sequential scans on large tables (L2+ should investigate every row)
SELECT relname,
       seq_scan, seq_tup_read,
       idx_scan, idx_tup_fetch,
       n_live_tup
FROM pg_stat_user_tables
WHERE n_live_tup > 10000
ORDER BY seq_tup_read DESC
LIMIT 20;

-- Cache hit ratio (L3+ expects >0.99 for OLTP)
SELECT sum(blks_hit)::numeric / NULLIF(sum(blks_hit) + sum(blks_read), 0) AS cache_hit_ratio
FROM pg_stat_database;

-- Dead tuple bloat (autovacuum health)
SELECT schemaname, relname, n_dead_tup, n_live_tup,
       round(100 * n_dead_tup::numeric / NULLIF(n_live_tup + n_dead_tup, 0), 2) AS dead_pct,
       last_autovacuum
FROM pg_stat_user_tables
ORDER BY dead_pct DESC NULLS LAST
LIMIT 20;

-- Lock waits (should be near-zero steady state)
SELECT blocked.pid AS blocked_pid,
       blocked.query AS blocked_query,
       blocking.pid AS blocking_pid,
       blocking.query AS blocking_query
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocking ON blocking.pid = ANY(pg_blocking_pids(blocked.pid));
```

**Verification — Django application layer:**

```python
# N+1 detection in test suite (fails if query count exceeds budget)
from django.test.utils import CaptureQueriesContext
from django.db import connection

with CaptureQueriesContext(connection) as ctx:
    response = client.get("/api/v1/orders/")
assert len(ctx.captured_queries) <= 3, ctx.captured_queries

# Or the blunt instrument:
with self.assertNumQueries(3):
    client.get("/api/v1/orders/")
```

```bash
# Runtime N+1 discovery
pip install django-silk  # or django-debug-toolbar for dev
# Silk records queries per request; review /silk/ after hitting the endpoint
```

**Verification — Go (pgx / database/sql):**

```bash
# Use sqlc for compile-time query checks (L4+)
sqlc generate

# Driver-level slow query logging via pgx tracer
# github.com/jackc/pgx/v5 — implement Tracer interface
```

**Common findings:**

- Django `prefetch_related` missing on `ForeignKey` reverse accessors in list views (classic N+1).
- Composite indexes in wrong column order for the actual filter.
- `WHERE status = 'X'` queries where `status` has two distinct values — index unused, seq scan preferred by planner correctly, but the query itself is wrong.
- `ORDER BY created_at DESC LIMIT 10` without index on `created_at` — planner sorts the whole table.
- Migration added an index but never ran `ANALYZE` — stale statistics cause plan regressions.

---

### 4.2 Connection Management

**Scope:** Connection pool sizing, lifecycle, leak prevention, saturation behavior, behavior during DB failover. Includes HTTP outbound pools and Redis/cache connections — not just the primary OLTP pool.

**Level criteria:**

- **L1** — Default pool settings; no metrics; unknown current connection count.
- **L2** — Pool size set, not benchmarked; no `max_lifetime` or `idle_timeout`; no leak detection.
- **L3** — Pool size derived from formula (workers × threads × concurrency factor); `max_lifetime` and `idle_timeout` set; leak detection enabled in dev.
- **L4** — Active/idle/waiting metrics exported; saturation alerts configured; PgBouncer (or equivalent) in front of Postgres; HTTP client pools sized per downstream dependency.
- **L5** — Pool sizes validated by load test; reconnection behavior tested during DB restart; connection churn rate bounded; blue/green deploy proven to not exhaust pool.

**Verification — Postgres server side:**

```sql
-- Connection state distribution
SELECT state, count(*)
FROM pg_stat_activity
GROUP BY state;

-- Connections by application
SELECT application_name, count(*)
FROM pg_stat_activity
GROUP BY application_name
ORDER BY count(*) DESC;

-- 'idle in transaction' — highest-severity leak signal
SELECT pid, usename, application_name,
       now() - xact_start AS tx_age,
       query
FROM pg_stat_activity
WHERE state = 'idle in transaction'
ORDER BY xact_start
LIMIT 20;

-- Connections vs max_connections headroom
SELECT (SELECT count(*) FROM pg_stat_activity) AS active,
       current_setting('max_connections')::int AS max_conn,
       round(100.0 * (SELECT count(*) FROM pg_stat_activity)
             / current_setting('max_connections')::numeric, 1) AS pct_used;
```

**Verification — PgBouncer (L4+ expected):**

```bash
# Connect to pgbouncer admin console
psql -h <pgbouncer-host> -p 6432 -U pgbouncer pgbouncer

# Pool health — cl_waiting > 0 over time means undersized server-side pool
SHOW POOLS;

# Aggregate stats — look for avg_wait_time spikes
SHOW STATS;

# Per-database config
SHOW DATABASES;
```

**Verification — OS level:**

```bash
# Socket state distribution from application host
ss -tan state established '( sport = :5432 or dport = :5432 )' | wc -l
ss -tan | awk 'NR>1 {print $1}' | sort | uniq -c

# TIME_WAIT accumulation (indicates connection churn; tune SO_REUSEADDR, keepalive)
ss -tan | grep TIME-WAIT | wc -l

# Per-process socket count (leak detection)
lsof -p <pid> -nP | grep -c TCP

# Ephemeral port exhaustion check
cat /proc/sys/net/ipv4/ip_local_port_range
ss -tan | awk '$1=="ESTAB"{print $4}' | awk -F: '{print $NF}' | sort -u | wc -l
```

**Verification — Go `database/sql` instrumentation:**

```go
stats := db.Stats()
log.Printf("open=%d in_use=%d idle=%d wait_count=%d wait_duration=%s max_idle_closed=%d max_lifetime_closed=%d",
    stats.OpenConnections, stats.InUse, stats.Idle,
    stats.WaitCount, stats.WaitDuration,
    stats.MaxIdleClosed, stats.MaxLifetimeClosed)
// Export these as Prometheus gauges. WaitCount trending up = undersized pool.
```

**Pool sizing formula (reference):**

For a Django / Gunicorn sync worker stack against Postgres:

```
connections_per_app_instance = workers × threads_per_worker
total_server_connections     = instances × connections_per_app_instance
                             + replication_slots
                             + admin_reserved (≈5)
```

Put PgBouncer in front in `transaction` pooling mode and you decouple the app's naive pool size from Postgres's painful per-connection memory cost (~10 MB each).

**Common findings:**

- Async views in Django using sync ORM calls — silently holds pool connections across await boundaries.
- `COMMIT` skipped on exception path → "idle in transaction" climbs slowly, pool drains over hours.
- HTTP client (`requests`, `http.Client`) created per-call instead of reused → TLS handshake on every request, sockets in `TIME_WAIT`.
- Pool `max_lifetime` unset → connections live forever, defeat DNS-based failover.
- Kubernetes rolling deploy drains connections faster than Postgres `authentication_timeout` → cascade failure during deploy.

---

### 4.3 Caching

**Scope:** Appropriateness of cache usage, hit rate, invalidation correctness, TTL strategy, thundering-herd protection, hot-key handling, negative caching.

**Level criteria:**

- **L1** — No caching, or caching ad-hoc with unknown hit rate.
- **L2** — Cache in place; hit rate <60% or unknown; TTLs guessed; no invalidation strategy beyond TTL expiry.
- **L3** — Hit rate measured at 60–80%; explicit TTLs justified; invalidation strategy documented (versioned keys, pub/sub, or write-through).
- **L4** — Hit rate >80%; cache stampede protection in place (singleflight, mutex, or sliding expiry); eviction policy chosen deliberately (not default `noeviction` in prod).
- **L5** — Hit rate alerted; negative caching where appropriate; cache warming on deploy; hot-key detection; tested behavior under cache outage (service degrades, does not collapse).

**Verification — Redis:**

```bash
# Hit/miss ratio (run, wait, run again for windowed read)
redis-cli INFO stats | grep -E 'keyspace_hits|keyspace_misses|expired_keys|evicted_keys'

# One-line hit rate
redis-cli INFO stats | awk -F: '
  /keyspace_hits/  {h=$2+0}
  /keyspace_misses/{m=$2+0}
  END { if (h+m>0) printf "hit_rate=%.4f total=%d\n", h/(h+m), h+m }
'

# Eviction policy (L4+ must be explicit: allkeys-lru, allkeys-lfu, volatile-lru, etc.)
redis-cli INFO memory | grep -E 'maxmemory_policy|used_memory_human|used_memory_peak_human|mem_fragmentation_ratio'

# Slow commands — anything over a few ms is a red flag in a hot path
redis-cli SLOWLOG GET 20
redis-cli CONFIG GET slowlog-log-slower-than

# Latency sample (L4+ baseline; anything >1ms median locally is suspicious)
redis-cli --latency -i 1

# Big keys (hot-key / cardinality bomb hunt)
redis-cli --bigkeys
redis-cli --memkeys

# Key distribution across DB
redis-cli --scan --pattern 'session:*' | wc -l
redis-cli DBSIZE
```

**Verification — Application layer:**

```python
# Cache stampede test (Django + redis-py)
# Expire a hot key, fire 200 concurrent requests, observe upstream DB
# Without singleflight: you'll see ~200 DB queries. With: 1.
```

```bash
# Load-test cache-miss path specifically
hey -n 1000 -c 100 -H "Cache-Bypass: 1" https://api.example.com/v1/popular
# Compare p99 to normal path; ratio tells you cache value and miss-storm risk
```

**Common findings:**

- Cache-aside with TTL alone → stampede on key expiry under load.
- `maxmemory_policy=noeviction` in prod → OOM errors when memory fills.
- Keys with user IDs embedded but no tenant isolation → cross-tenant data leak.
- TTL set to 1 hour by default on everything → either too long for mutable data or too short for static data.
- Cache serialized with `pickle` — becomes an RCE gadget if Redis is exposed.

---

### 4.4 API Latency SLOs

**Scope:** Defined latency SLOs per endpoint class; measurement via histograms (not averages); burn-rate alerting; error budget accounting.

**Level criteria:**

- **L1** — No SLOs; no per-endpoint latency tracking.
- **L2** — Averages / medians tracked; no SLO definition.
- **L3** — p50/p95/p99 tracked per endpoint; SLOs drafted but not alerted.
- **L4** — SLOs published; error budget calculated; histogram metrics (not summaries — histograms aggregate correctly across instances, summaries do not); burn-rate alerts set.
- **L5** — Multi-window multi-burn-rate alerts (fast + slow window); SLO breach gates releases; customer-visible SLO dashboard.

**Reference SLOs (starting point — calibrate to product):**

| Endpoint class | p50 | p95 | p99 | Availability |
|---|---|---|---|---|
| Read (cacheable) | 20 ms | 80 ms | 200 ms | 99.95% |
| Read (uncached) | 50 ms | 200 ms | 500 ms | 99.9% |
| Write (simple) | 80 ms | 300 ms | 600 ms | 99.9% |
| Write (workflow) | 200 ms | 800 ms | 1500 ms | 99.5% |

**Verification — single-request timing breakdown:**

```bash
# TCP/TLS/TTFB decomposition
curl -o /dev/null -s -w \
'dns=%{time_namelookup}s tcp=%{time_connect}s tls=%{time_appconnect}s ttfb=%{time_starttransfer}s total=%{time_total}s http=%{http_code}\n' \
  https://api.example.com/v1/orders/123

# Against several hosts / cache hit vs miss
for i in 1 2 3 4 5; do
  curl -o /dev/null -s -w '%{time_total}\n' https://api.example.com/v1/orders/123
done
```

**Verification — load testing:**

```bash
# hey — quick, fixed-rate
hey -n 10000 -c 100 -H 'Authorization: Bearer <token>' https://api.example.com/v1/orders

# wrk — higher concurrency, lua scripting
wrk -t8 -c200 -d60s --latency -s post.lua https://api.example.com/v1/orders

# vegeta — rate-based (models Poisson-like traffic better than fixed concurrency)
echo "GET https://api.example.com/v1/orders" | \
  vegeta attack -rate=500 -duration=60s | \
  vegeta report -type=hdrplot

# k6 — scriptable, thresholds-as-code (L4+ preferred)
k6 run --vus 50 --duration 5m \
  --summary-trend-stats="avg,min,med,p(95),p(99),p(99.9),max" \
  script.js
```

**Verification — Prometheus queries for SLO:**

```promql
# p99 per route, last 5 min
histogram_quantile(0.99,
  sum by (le, route) (
    rate(http_request_duration_seconds_bucket[5m])
  )
)

# Error-budget burn rate (1h fast window)
(
  sum(rate(http_requests_total{status=~"5.."}[1h]))
  / sum(rate(http_requests_total[1h]))
) / (1 - 0.999)   -- target is 99.9%; burn > 1 means eating budget faster than allowed
```

**Common findings:**

- Latency "SLO" actually reports the average, not p99. Averages hide long tails that kill user experience.
- Prometheus `summary` metric used instead of `histogram` — p99 values lie when aggregated across pods.
- Client-measured latency not reported — server-side metrics miss network effects.
- SLO measured across all endpoints in aggregate — hides that `/healthz` dominates volume and masks the fact that `/checkout` is broken.

---

### 4.5 Observability

**Scope:** RED metrics (Rate, Errors, Duration) per service; structured logs with trace/request correlation IDs; distributed tracing with context propagation across process and async boundaries; alerts linked to runbooks.

**Level criteria:**

- **L1** — Unstructured stdout logs; no metrics; no tracing.
- **L2** — Logs centralized (Loki/ELK/Cloud equivalent); basic metrics (CPU, memory); no tracing.
- **L3** — RED metrics per service; request IDs in logs; per-request tracing in some services.
- **L4** — OpenTelemetry end-to-end; trace context propagated across queues and async boundaries; logs-trace correlation via `trace_id`; cardinality budget enforced.
- **L5** — SLO-linked alerts with runbooks; trace exemplars on Prometheus histograms; head + tail sampling; chaos exercises rely on tracing to diagnose — not logs-grep.

**Verification:**

```bash
# Metrics endpoint exposed and scraping (L3+)
curl -s localhost:9090/metrics | head -50
curl -s localhost:9090/metrics | grep -c 'http_request_duration_seconds_bucket'   # should be > 0

# Cardinality check — per-metric series count
curl -s http://prometheus:9090/api/v1/status/tsdb | jq '.data.seriesCountByMetricName[0:20]'
# Any metric with >1M series = cardinality bomb (usually unbounded user_id or URL in label)

# Trace propagation — manually inject W3C traceparent
TRACE_ID=$(openssl rand -hex 16)
SPAN_ID=$(openssl rand -hex 8)
curl -H "traceparent: 00-${TRACE_ID}-${SPAN_ID}-01" \
     https://api.example.com/v1/orders
# Then query Tempo/Jaeger/etc. for ${TRACE_ID} — expect the full call graph

# OTel Collector health
curl -s http://otel-collector:13133/
curl -s http://otel-collector:8888/metrics | grep -E 'otelcol_receiver_accepted|otelcol_exporter_send'

# Log correlation — every log line for one request should share trace_id
kubectl logs -l app=api --tail=1000 | grep "${TRACE_ID}" | wc -l
# If 0 and the trace exists, log-to-trace correlation is broken
```

**Verification — cardinality hygiene:**

```promql
# Top metrics by series count (Prometheus)
topk(20, count by (__name__)({__name__=~".+"}))

# Label cardinality per metric — any label with thousands of values = bug
count(count by (user_id) (http_requests_total))
count(count by (route)   (http_requests_total))
```

**Common findings:**

- `user_id` or unbounded `path` in a Prometheus label → series explosion → Prometheus OOMs.
- Tracing works for HTTP but context lost at Celery/SQS/Kafka boundary → half the call graph is missing.
- Alert fires, but alert description is `"KubePodCrashLooping"` with no runbook link and no ownership.
- Logs use `%s` formatting with structured fields inlined as strings — unsearchable.
- Sampling at 1% → you only see the traces you don't need and miss the rare failure you do.

---

### 4.6 Reliability

**Scope:** Timeouts at every boundary; context cancellation; retry policy with jitter and budget; circuit breakers on outbound calls; idempotency for retryable mutations; graceful shutdown; tested dependency failure behavior.

**Level criteria:**

- **L1** — No timeouts set, or timeouts inherited from library defaults (often minutes or infinite). No retries, or unbounded retries. No circuit breakers.
- **L2** — Some client timeouts; retries with fixed backoff, no jitter, no budget. No idempotency keys.
- **L3** — Timeouts at all I/O boundaries; exponential backoff with jitter; graceful shutdown on SIGTERM; idempotency for critical mutations.
- **L4** — Circuit breakers on outbound calls (half-open recovery tested); retry budget enforced to prevent retry storms; deadline/context propagated end-to-end; load shedding under saturation.
- **L5** — Chaos tests run on cadence (pod kill, network partition, dependency slowdown); documented degradation modes (partial availability > full outage); DR drill executed within last quarter.

**Verification:**

```bash
# Timeout enforcement — this request should fail in ≤5s, not hang
time curl --max-time 5 https://api.example.com/v1/slow-downstream

# Idempotency replay — same key, same body, expect 1 resource created total
IDEMP=$(uuidgen)
for i in 1 2 3 4 5; do
  curl -sS -X POST \
    -H "Idempotency-Key: ${IDEMP}" \
    -H "Content-Type: application/json" \
    -d '{"amount": 100, "to": "acc_123"}' \
    https://api.example.com/v1/payments
  echo
done
# Check DB: expect exactly one row. Multiple rows = idempotency is not.

# Graceful shutdown — in-flight requests drain, no 5xx spike
kubectl delete pod <pod-name> --grace-period=30 &
while true; do
  curl -o /dev/null -s -w '%{http_code}\n' https://api.example.com/v1/healthz
  sleep 0.2
done
# Expect: 200s only. Any 502/503/504 during drain = shutdown is not graceful.

# Dependency failure injection — what happens if Redis dies?
# Using toxiproxy (put it in front of Redis for the duration of the test)
toxiproxy-cli create redis --listen 0.0.0.0:26379 --upstream redis:6379
toxiproxy-cli toxic add redis -t latency -a latency=5000
# Now hit your API: does it return 503 fast (good — circuit breaker)?
# Or hang until its own timeout (bad — cascading failure)?

# Network impairment (Linux tc)
sudo tc qdisc add dev eth0 root netem loss 20% delay 200ms 50ms distribution normal
# Run load test. Revert when done:
sudo tc qdisc del dev eth0 root

# SIGTERM local behavior
kill -TERM $(pgrep -f 'my-service')
# Watch logs: expect "draining", "closed listener", in-flight completed, then exit 0.
```

**Verification — retry-storm model:**

```promql
# Retry amplification: if downstream is slow, does upstream retry flood it?
sum(rate(outbound_http_requests_total{downstream="payments"}[1m]))
  /
sum(rate(inbound_http_requests_total{route="/checkout"}[1m]))
# Should be ≈ 1.0 steady state. If 2–3 under load, you have a retry storm.
```

**Common findings:**

- `requests.get(url)` with no `timeout=` — default is infinite. One slow downstream hangs every worker.
- Retry loop with no jitter — all clients retry in lockstep, downstream sees N× traffic spike.
- Retry on non-idempotent POST with no idempotency key — duplicate charges, duplicate emails.
- Readiness probe returns 200 even when upstream DB is down — traffic routed to a broken pod.
- `preStop` hook shorter than `terminationGracePeriodSeconds`, but LB deregistration slower than both — in-flight requests cut off.

---

### 4.7 Security

**Scope:** AuthN/AuthZ correctness; secret handling; input validation and output encoding; rate limiting and anti-abuse; TLS configuration; dependency CVE management; OWASP API Top 10 coverage.

**Level criteria:**

- **L1** — Basic auth or no auth; secrets in `.env` committed to repo; no rate limiting; no dependency scanning.
- **L2** — OAuth / JWT present; token rotation unclear; rate limiting global only; validation ad-hoc per endpoint; secrets in Kubernetes secrets (base64, not encrypted).
- **L3** — Strong AuthN; RBAC or attribute-based authZ; per-route rate limits; validation via shared schema library; secrets in vault (Hashicorp Vault, AWS Secrets Manager, GCP Secret Manager).
- **L4** — Short-lived tokens with refresh; mTLS service-to-service; SAST + dependency scanning + container scanning in CI; WAF in front of public endpoints; secret rotation automated.
- **L5** — Least-privilege IAM (per-service roles, not shared); SBOM generated and published per release; pen test within last 6 months; SOC2/ISO 27001 control mapping documented.

**Verification:**

```bash
# TLS configuration audit
openssl s_client -connect api.example.com:443 -tls1_3 </dev/null 2>/dev/null | \
  openssl x509 -noout -dates -subject -issuer

# Cipher suite & vulnerability scan
nmap --script ssl-enum-ciphers,ssl-heartbleed,ssl-poodle -p 443 api.example.com
testssl.sh --severity HIGH https://api.example.com

# Security response headers
curl -sI https://api.example.com | grep -Ei \
  'strict-transport-security|content-security-policy|x-frame-options|x-content-type-options|referrer-policy|permissions-policy'

# Auth enforcement sweep — expect 401/403 on every line
for path in /api/v1/users /api/v1/admin /api/v1/orders /api/v1/internal/debug; do
  code=$(curl -o /dev/null -s -w "%{http_code}" https://api.example.com$path)
  echo "$code  $path"
done
# Any 200 on an unauthenticated request to a protected path = finding.

# Rate limit verification
for i in $(seq 1 500); do
  curl -o /dev/null -s -w "%{http_code}\n" https://api.example.com/v1/login
done | sort | uniq -c
# Expect a transition to 429. No 429 at 500 req/s = no rate limit.

# IDOR / authZ check — user A's token against user B's resource
curl -H "Authorization: Bearer $TOKEN_USER_A" \
  https://api.example.com/v1/users/$USER_B_ID/orders
# Expect 403/404. Anything else = horizontal privilege escalation.

# JWT inspection — alg=none, weak secret, missing exp?
echo "$JWT" | cut -d. -f1 | base64 -d 2>/dev/null | jq
echo "$JWT" | cut -d. -f2 | base64 -d 2>/dev/null | jq
# Look for: "alg":"none", missing "exp", missing "aud"/"iss"

# Dependency CVE scanning
pip-audit --strict
npm audit --audit-level=high
govulncheck ./...
cargo audit
trivy fs --severity HIGH,CRITICAL --ignore-unfixed .
trivy image --severity HIGH,CRITICAL my-service:latest

# Secret scanning — catches accidents that `git grep` misses
gitleaks detect --source . --redact
trufflehog git file://. --only-verified

# Container / infra
trivy config .
checkov -d . --framework kubernetes,dockerfile,terraform

# Input fuzzing (quick smoke)
ffuf -w /usr/share/wordlists/SecLists/Fuzzing/special-chars.txt \
     -u 'https://api.example.com/v1/search?q=FUZZ' \
     -mc 500 -t 20
# Any 5xx on inputs that should produce 400 = input validation gap (often a DoS vector)
```

**Common findings:**

- JWT verification skips `exp` check because "we tested in staging and it worked."
- `X-Forwarded-For` trusted without checking whether the request came through the proxy — attackers spoof rate-limit bypass.
- Rate limit keyed on user ID — unauthenticated login endpoint has no user ID, so the limit is ineffective against credential stuffing.
- SSRF via URL field that lets user supply `http://169.254.169.254/` (cloud metadata) or internal services.
- `DEBUG=True` still on in production staging alias — exposes full stack traces with secrets.

---

### 4.8 Scalability

**Scope:** Statelessness; horizontal scaling headroom; autoscaler signal correctness; backpressure under overload; database scale path (read replicas, sharding); cold start behavior.

**Level criteria:**

- **L1** — Single instance; sticky sessions; local filesystem state; scaling requires manual intervention.
- **L2** — Multi-instance but in-process session or cache; scaling manual or reactive.
- **L3** — Stateless services; shared cache and session store; horizontal scaling verified at 2x and 3x.
- **L4** — HPA tuned on correct signals (request rate, queue depth, p99 latency — not just CPU); read replicas used; sharding path documented even if not yet needed.
- **L5** — Load-tested at 10x peak; backpressure under overload (service sheds, not collapses); DR / region failover tested; quarterly capacity planning with forecast vs actual review.

**Verification:**

```bash
# Statelessness — kill and replace mid-request
# In one shell: sustained load
hey -n 100000 -c 50 -q 100 https://api.example.com/v1/orders &

# In another: roll the pods
kubectl rollout restart deployment/api-service
# Load test should show: same success rate, no 5xx spike beyond the drain window.
# Any persistent failure tied to a specific pod = stateful leak.

# Session affinity detection (accidental statefulness)
for i in $(seq 1 20); do
  curl -s https://api.example.com/v1/whoami | jq -r .pod_id
done | sort | uniq -c
# Expect roughly even distribution. Pinned to one pod = sticky session (bug unless intentional).

# HPA configuration review
kubectl get hpa -A
kubectl describe hpa <name>
# Look at: min/max replicas, metrics (CPU alone = L3 at best), behavior.scaleDown.stabilizationWindowSeconds
# If scaleDown window < 5 min → thrashing risk.

# Queue depth — the truth serum for async systems
# SQS
aws sqs get-queue-attributes \
  --queue-url $QUEUE_URL \
  --attribute-names ApproximateNumberOfMessages \
      ApproximateNumberOfMessagesNotVisible \
      ApproximateNumberOfMessagesDelayed

# RabbitMQ
rabbitmqctl list_queues name messages messages_ready messages_unacknowledged consumers

# Kafka consumer lag — the most important number in any event-driven system
kafka-consumer-groups --bootstrap-server $BROKER \
  --describe --group <consumer-group>
# Look at LAG column. Sustained positive lag = consumer can't keep up.

# Load-test ramp — find the knee
k6 run --stage '2m:100,5m:500,5m:1000,5m:2000,2m:0' script.js
# Plot p99 vs throughput. The knee (where p99 inflects) is your real capacity.

# Cold start measurement (matters for serverless and k8s HPA reactions)
kubectl delete pod <pod>
# Time until readinessProbe passes = cold start. >30s is a scaling hazard.
kubectl get pod <new-pod> -o jsonpath='{.status.containerStatuses[0].started}'
```

**Verification — DB scale path:**

```sql
-- Read replica lag (Postgres streaming replication)
SELECT client_addr, state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), sent_lsn)   AS sent_lag,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag,
       write_lag, flush_lag, replay_lag AS replay_time_lag
FROM pg_stat_replication;
-- replay_lag > ~1s sustained = reads served from replica will show stale data

-- Biggest tables (sharding candidates when they cross ~100M rows / ~500GB)
SELECT schemaname, relname,
       pg_size_pretty(pg_total_relation_size(relid)) AS total,
       n_live_tup
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;
```

**Common findings:**

- Stateless in theory, but an in-memory LRU cache means per-pod cache incoherence and hot-pod behavior.
- HPA scales on CPU only — service is I/O-bound, CPU stays flat, queue backs up unbounded.
- "Scales horizontally" — but every instance holds an open Postgres connection, and the pool size in front of Postgres does not scale.
- Cold start 45s because the app does DB migrations on every boot — first request after scale-up times out.
- Read replicas configured but app never routes reads to them — replicas are dead weight.

---

## 5. Master Scorecard

```
System: ___________________________________  Date: ______________
Commit SHA: _______________________________  Environment: _______
Lead auditor: _____________________________  Reviewer: __________

┌──────────────────────────────────┬───────┬────────────────────────────┐
│ Dimension                        │ Score │ Evidence reference         │
├──────────────────────────────────┼───────┼────────────────────────────┤
│ 4.1 Query Performance            │ [1-5] │                            │
│ 4.2 Connection Management        │ [1-5] │                            │
│ 4.3 Caching                      │ [1-5] │                            │
│ 4.4 API Latency SLOs             │ [1-5] │                            │
│ 4.5 Observability                │ [1-5] │                            │
│ 4.6 Reliability                  │ [1-5] │                            │
│ 4.7 Security                     │ [1-5] │                            │
│ 4.8 Scalability                  │ [1-5] │                            │
├──────────────────────────────────┼───────┼────────────────────────────┤
│ TOTAL                            │ /40   │                            │
└──────────────────────────────────┴───────┴────────────────────────────┘

Lowest-scoring dimension: _________________________________________
Blocking findings (any L1 in Security or Reliability): ___________
Overall disposition (§6): _________________________________________
```

---

## 6. Disposition Matrix

| Total | Minimum per dim. | Disposition | Required action |
|---|---|---|---|
| 36–40 | ≥ 4 | **Production-ready** | Continuous improvement; revisit in 6 months or on major architectural change. |
| 28–35 | ≥ 3 | **Production with watchlist** | Remediate all L3 dimensions within one quarter; re-audit lowest scorer in 90 days. |
| 20–27 | ≥ 2 | **Conditional production** | Dedicate a dedicated remediation sprint; new feature work gated on plan approval. |
| 12–19 | any | **Significant technical debt** | Halt non-critical feature work; remediation plan required within 2 weeks; re-audit in 60 days. |
| < 12 | any | **Not production-grade** | Stop customer-facing rollout. Executive escalation. Service is operating on borrowed time. |

**Overrides (any one triggers a one-tier downgrade regardless of total):**

- Any L1 in **Security** or **Reliability**.
- Any dimension scored solely on verbal assertion.
- Any outstanding P0 incident in the last 30 days mapped to a dimension scored ≥ 4.

---

## 7. Findings & Remediation Queue

For every dimension scored ≤ 3, record:

```
Finding ID:          F-<NNNN>
Dimension:           4.x <name>
Severity:            Critical | High | Medium | Low
Observed score:      Lx
Target score:        Lx
Evidence:            (paste command output, log excerpt, or link)
Impact if unfixed:   (user-facing consequence, not "technical debt")
Effort estimate:     (person-days, t-shirt size)
Proposed remediation:(specific, testable)
Owner:               (named engineer)
Due date:
Verification plan:   (how we'll prove it's fixed — usually a command from §4)
```

Findings are tracked in the engineering backlog. Closing a finding requires re-running the relevant verification command and attaching output to the ticket.

---

## 8. Appendix A — Tooling Checklist

| Category | Baseline tool | Notes |
|---|---|---|
| DB query analysis | `pg_stat_statements`, `EXPLAIN (ANALYZE, BUFFERS)` | Enable in all envs; redact params in logs. |
| Connection pooling | PgBouncer (transaction mode) | Effectively required above ~50 app instances. |
| Load testing | k6 or Vegeta | k6 for scripted scenarios, Vegeta for rate-based traffic shapes. |
| Fault injection | toxiproxy, `tc netem`, Chaos Mesh | Start local (toxiproxy), graduate to cluster-wide. |
| Tracing | OpenTelemetry (OTLP) | Vendor-neutral; supported by Tempo, Jaeger, Datadog, Honeycomb, etc. |
| Metrics | Prometheus + histograms | Avoid summaries except for strictly per-instance data. |
| Dependency CVE | `trivy`, `govulncheck`, `pip-audit`, `npm audit`, `cargo audit` | Run in CI, break build on HIGH/CRITICAL. |
| Secret scanning | `gitleaks`, `trufflehog` | Pre-commit hook + CI job. |
| TLS audit | `testssl.sh`, `nmap ssl-enum-ciphers` | Quarterly at minimum. |

## 9. Appendix B — Common Auditor Pitfalls

1. **Scoring optimism.** The service owner's belief that something works is L1 evidence at best. Ask for the command output.
2. **Mid-audit tuning.** The engineer fixes the issue while you are watching and says "so we'd score a 4 now." Score the system as it was at the pinned SHA. File a finding; re-audit after merge.
3. **Partial coverage counted as full.** Rate limiting on one endpoint is not rate limiting. Caching in one service is not a caching posture.
4. **Averages masquerading as SLOs.** If the metric is `avg(latency)`, the SLO is not measured. Histograms or nothing.
5. **Green dashboard, red reality.** Dashboards showing 200 OK do not prove correctness — they prove the server didn't crash. Run a functional check.
6. **"It worked in staging."** Staging does not have production traffic, production data volume, production secrets rotation, or production cost of failure. Staging confirms absence of syntax errors, not production readiness.

---

**End of scorecard.**

Next action after completion: for every dimension scored 1–3, open a finding per §7 and queue remediation with owner and due date. The audit document is filed at `/audits/<service>/<YYYY-MM-DD>.md` and referenced in the service's runbook.