# Backend Audit Checklist

Use this checklist to assess the health of a backend system. Each item has a measurable threshold. Items marked ✗ are immediate action items. Items marked ⚠ are improvement opportunities.

**Scoring:** 1 point per ✓. 0 points per ✗ or ⚠.
- 90–100%: Production-ready
- 70–89%: Some gaps, addressable within one sprint
- 50–69%: Significant technical debt, prioritize this quarter
- <50%: High risk, stop and fix before further feature development

---

## 1. API Performance

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 1.1 | Read endpoints p99 latency | < 200ms | APM or load test with k6/wrk |
| 1.2 | Write endpoints p99 latency | < 500ms | APM or load test |
| 1.3 | No endpoint exceeds p99 > 1s under normal load | 0 violations | APM alerting |
| 1.4 | p99/p50 ratio (tail latency amplification) | < 4× | Histogram analysis |
| 1.5 | Timeouts configured on all outbound HTTP calls | 100% coverage | Code audit |
| 1.6 | Request size limits enforced | Max body size configured | Framework config |

**How to measure:**
```bash
# k6 load test — generates p50/p99 breakdown
k6 run --vus 50 --duration 60s script.js

# wrk quick benchmark
wrk -t 4 -c 100 -d 30s --latency http://localhost:8080/api/users
```

---

## 2. Database

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 2.1 | No query exceeds p99 > 100ms under normal load | 0 violations | `pg_stat_statements` |
| 2.2 | Foreign key columns have indexes | 100% of FKs | `\d tablename` or schema audit |
| 2.3 | No sequential scans on tables > 10,000 rows in hot paths | 0 violations | `EXPLAIN ANALYZE` + `pg_stat_user_tables` |
| 2.4 | N+1 query patterns absent | 0 hot endpoints with queries/req > 10 | APM query count tracing |
| 2.5 | Query timeouts configured | `statement_timeout` set | `SHOW statement_timeout` |
| 2.6 | Slow query log enabled | `log_min_duration_statement` ≤ 100ms | `SHOW log_min_duration_statement` |
| 2.7 | `EXPLAIN ANALYZE` reviewed for all queries used > 1000×/day | Documented | Query plan audit |
| 2.8 | Index bloat < 20% | `pgstattuple` | `SELECT * FROM pgstattuple('tablename')` |

**Quick FK index audit (PostgreSQL):**
```sql
SELECT
    tc.table_name,
    kcu.column_name,
    ccu.table_name AS foreign_table,
    (SELECT COUNT(*) FROM pg_indexes
     WHERE tablename = tc.table_name
     AND indexdef LIKE '%' || kcu.column_name || '%') AS index_count
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
JOIN information_schema.referential_constraints rc
    ON tc.constraint_name = rc.constraint_name
JOIN information_schema.constraint_column_usage ccu
    ON ccu.constraint_name = rc.unique_constraint_name
WHERE tc.constraint_type = 'FOREIGN KEY'
HAVING index_count = 0;
-- Results: FK columns with no index → add these indexes
```

---

## 3. Caching

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 3.1 | Cache hit rate for hot read paths | > 80% | `redis-cli INFO stats` |
| 3.2 | TTL configured on all cache keys | 100% have explicit TTL | Code audit |
| 3.3 | Eviction policy configured | `allkeys-lru` or `volatile-lru` set | `redis-cli CONFIG GET maxmemory-policy` |
| 3.4 | `maxmemory` configured | Not unlimited | `redis-cli CONFIG GET maxmemory` |
| 3.5 | Cache key namespace collision check | No two entities share prefix | Naming convention audit |
| 3.6 | Thundering herd protection on popular keys | PER or mutex in place | Code audit of cache-miss paths |
| 3.7 | Negative caching for non-existent lookups | Bloom filter or null-TTL caching | Code audit |
| 3.8 | Cache eviction rate acceptable | < 5% eviction/hour | `redis-cli INFO stats` evicted_keys |

**Redis health check:**
```bash
redis-cli INFO stats | grep -E "keyspace_hits|keyspace_misses|evicted_keys"
redis-cli INFO memory | grep -E "used_memory_human|maxmemory_human"
# hit_rate = keyspace_hits / (keyspace_hits + keyspace_misses)
# Target: > 0.80
```

---

## 4. Connection Management

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 4.1 | Connection pool configured (not new-connection-per-request) | Pool exists | Code audit |
| 4.2 | Pool size follows sizing formula | pool_size ≈ DB_CPU_cores × 2 | Config review |
| 4.3 | Pool acquire timeout configured | ≤ 30 seconds | Config review |
| 4.4 | `max_conn_lifetime` configured | < 30 minutes | Config review |
| 4.5 | `max_conn_idle_time` configured | < 10 minutes | Config review |
| 4.6 | Connection leak detection enabled | Leak detection implemented | Code audit |
| 4.7 | Pool metrics exposed (active, idle, waiting) | Metrics endpoint exists | `/metrics` or APM |
| 4.8 | Connections released in `finally` blocks | 100% coverage | Code audit |
| 4.9 | No connections held across external HTTP calls | 0 violations | Code audit |
| 4.10 | For serverless: connection proxy (RDS Proxy/PgBouncer) in use | Proxy configured | Architecture review |

**Pool sizing calculator:**
```
target_pool_size = ceil(peak_rps × avg_query_duration_seconds × 1.3)

Example:
  Peak load: 500 req/s
  Avg query: 15ms = 0.015s
  Safety factor: 1.3
  pool_size = ceil(500 × 0.015 × 1.3) = ceil(9.75) = 10
  Cross-check: DB has 4 cores → 4 × 2 = 8 → take max(10, 8) = 10
```

---

## 5. Observability

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 5.1 | Distributed tracing implemented | 100% of service boundaries | Jaeger/Datadog APM |
| 5.2 | RED metrics exported (Rate, Errors, Duration) | Per endpoint | Prometheus/Datadog |
| 5.3 | Structured logging (JSON, not freeform text) | 100% of log lines | Log aggregator check |
| 5.4 | Request IDs propagated across service calls | 100% of requests | Header audit |
| 5.5 | Error rates alerted | Alert at > 1% error rate | Alert config |
| 5.6 | Latency p99 alerted | Alert at > 500ms p99 | Alert config |
| 5.7 | DB query count per request tracked | Metric exists | APM or middleware |
| 5.8 | Cache hit rate tracked | Metric exists | Redis metrics |

**Minimum viable metric set (Prometheus):**
```yaml
# Must have these metrics for every HTTP endpoint:
http_requests_total{method, path, status_code}
http_request_duration_seconds{method, path}  # histogram with p50/p99

# Database:
db_queries_total{endpoint}
db_query_duration_seconds  # histogram

# Cache:
cache_hits_total
cache_misses_total
cache_evictions_total

# Connection pool:
db_pool_connections_total{state}  # state: idle, active
db_pool_wait_duration_seconds     # histogram
```

---

## 6. Security

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 6.1 | Authentication on all non-public endpoints | 100% coverage | Endpoint audit |
| 6.2 | Authorization checked at data layer (not just route layer) | 100% coverage | Code audit |
| 6.3 | Rate limiting on all public endpoints | Limits configured | Rate limiter config |
| 6.4 | Rate limiting on auth endpoints | Stricter limits (e.g., 10 req/min) | Rate limiter config |
| 6.5 | SQL injection prevention (parameterized queries) | 0 string-concatenated queries | Code audit + SAST |
| 6.6 | Input validation on all user-provided data | Validation library in use | Code audit |
| 6.7 | Secrets not in source code | 0 secrets in git history | `git log -S "password"` + vault |
| 6.8 | CORS configured (not `*` on API) | Domain allowlist | Config audit |
| 6.9 | Security headers set (HSTS, X-Frame-Options, etc.) | Headers present | `curl -I https://...` |
| 6.10 | Dependencies scanned for CVEs | Scan in CI pipeline | `npm audit`, `safety`, `govulncheck` |

---

## 7. Reliability

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 7.1 | Circuit breakers on all external service calls | 100% of external calls | Code audit |
| 7.2 | Retry with exponential backoff implemented | Retries capped, jittered | Code audit |
| 7.3 | Timeouts on ALL outbound calls (DB, HTTP, cache) | 100% have explicit timeout | Code audit |
| 7.4 | Graceful shutdown implemented | SIGTERM handled, in-flight requests complete | Code audit |
| 7.5 | Health check endpoints exist (liveness + readiness) | `/health/live` and `/health/ready` | Endpoint test |
| 7.6 | Deployment is zero-downtime | Rolling update or blue/green | Deploy config |
| 7.7 | Database migrations are backward-compatible | Non-breaking schema changes | Migration review |
| 7.8 | Feature flags available | Flag system in use | Code audit |

**Circuit breaker threshold guidance:**
```
Open circuit when:
  error_rate > 50% in last 10 seconds AND at least 20 requests
Half-open: allow 1 request through every 30 seconds to test recovery
Close: if 3 consecutive successes in half-open state
```

---

## 8. Scalability

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 8.1 | Horizontal scaling tested | Service runs with N>1 instances without issues | Load test |
| 8.2 | Stateless service (no in-process session state) | State in Redis/DB only | Architecture review |
| 8.3 | No shared mutable in-memory state across requests | Confirmed stateless | Code audit |
| 8.4 | Database read replicas used for read-heavy queries | Read/write split configured | DB config |
| 8.5 | Long-running jobs in async queue (not synchronous HTTP) | Job queue in use | Architecture review |
| 8.6 | File uploads/downloads proxied (not through app server) | Presigned URLs or CDN | Code audit |
| 8.7 | Pagination on all list endpoints | Cursor or offset pagination | Endpoint audit |
| 8.8 | Max response size bounded | No unbounded result sets | Code audit |

---

## 9. Development Practices

| # | Check | Threshold | Method |
|---|-------|-----------|--------|
| 9.1 | `EXPLAIN ANALYZE` for every new query | PR review requirement | Code review |
| 9.2 | Load tests for every new endpoint > 100 req/day | Load test in CI | CI config |
| 9.3 | Query count asserted in tests | Test fails if N+1 introduced | Test suite |
| 9.4 | Database migrations reviewed for lock risk | Advisory lock audit | Migration review |
| 9.5 | Performance budget defined and enforced | p99 SLA per endpoint documented | ADR or runbook |

---

## Score Summary Template

```
Team: _______________   Date: _______________   Reviewer: _______________

Section                    Score    Max    %
─────────────────────────────────────────────
1. API Performance         ___      6      ___
2. Database                ___      8      ___
3. Caching                 ___      8      ___
4. Connection Management   ___      10     ___
5. Observability           ___      8      ___
6. Security                ___      10     ___
7. Reliability             ___      8      ___
8. Scalability             ___      8      ___
9. Development Practices   ___      5      ___
─────────────────────────────────────────────
TOTAL                      ___      71     ___  %

Top 3 action items:
1. _______________________________________________
2. _______________________________________________
3. _______________________________________________
```
