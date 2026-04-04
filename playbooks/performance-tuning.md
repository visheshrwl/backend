# Performance Tuning Playbook

## When to Use This Playbook

- After an incident caused by latency or throughput degradation
- Before a planned high-traffic event
- When p99 latency exceeds SLO for 2+ consecutive days
- After a significant feature release that changed data access patterns

## Step 1: Establish Baseline (15 minutes)

```bash
# Capture current state
# 1. p50/p99/p999 per endpoint from APM
# 2. DB: avg queries per request per endpoint
# 3. Cache hit rate
# 4. Connection pool: active/idle/waiting
# 5. CPU and memory utilization

# Document in your incident/tuning notes:
echo "Baseline: $(date)"
echo "p99: [from APM]"
echo "DB queries/req: [from APM]"
echo "Cache hit rate: $(redis-cli INFO stats | grep keyspace)"
```

## Step 2: Identify the Bottleneck (30 minutes)

Work through this checklist in order:

### 2a. Database Queries
```sql
-- Top queries by total time
SELECT query, calls, mean_exec_time, total_exec_time
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;

-- Queries with high call count (N+1 suspects)
SELECT query, calls
FROM pg_stat_statements
WHERE calls > 10000
ORDER BY calls DESC;
```

### 2b. Missing Indexes
```sql
SELECT schemaname, tablename, seq_scan, seq_tup_read,
       idx_scan, idx_tup_fetch
FROM pg_stat_user_tables
WHERE seq_scan > idx_scan
  AND n_live_tup > 10000
ORDER BY seq_scan DESC;
```

### 2c. Connection Pool Health
```
# Check pool metrics in Prometheus/Datadog:
db_pool_connections_total{state="active"} / max_pool_size
db_pool_wait_duration_seconds (p99)
```

### 2d. Cache Performance
```bash
redis-cli INFO stats | grep -E "keyspace_hits|keyspace_misses"
# Calculate: hit_rate = hits / (hits + misses)
```

## Step 3: Apply Fix and Measure

| Bottleneck Found | Fix | Expected Improvement |
|-----------------|-----|---------------------|
| N+1 queries | Add eager loading / IN batch | 10-100x query count reduction |
| Missing index | `CREATE INDEX CONCURRENTLY` | 100-1000x for point lookups |
| Pool undersized | Increase max_connections | p99 wait drops proportionally |
| Low cache hit rate | Increase TTL or cache size | Direct improvement per equation |
| CPU-bound work blocking async | Move to thread/process pool | Unblocks event loop |

## Step 4: Verify Improvement

After applying fix:
- Wait 5 minutes for metrics to stabilize
- Compare p50/p99 to baseline
- Confirm no regression in error rate
- Document the before/after in tuning notes

## Related Modules

- `../../bsps/07-core-backend-engineering/01-n-plus-one-query-problem.md`
- `../../bsps/07-core-backend-engineering/02-connection-pooling.md`
- `../../bsps/07-core-backend-engineering/03-caching-strategy.md`
- `../../bsps/09-performance-engineering/01-profiling-and-benchmarking.md`
