# Migration Playbook

## Purpose

Step-by-step guide for common backend migration patterns with zero-downtime requirements.

## Migration 1: Introduce Connection Pooling

**Scenario:** Application creates new connections per request. Add a pool.

### Step 1: Instrument current behavior
```python
# Add metrics to measure connection creation rate
# before introducing a pool
connection_creation_rate = Counter('db_connections_created_total')
```

### Step 2: Calculate optimal pool size
```
pool_size = ceil(peak_rps × avg_query_duration_seconds × 1.3)
```

### Step 3: Introduce pool in shadow mode
Run pool alongside existing code. Log both paths. Verify behavior is identical.

### Step 4: Cut over
Deploy with pool active. Monitor:
- Connection creation rate (should drop to near zero after pool warms)
- p99 latency (should improve)
- Error rate (should stay flat)

### Step 5: Remove old code path

---

## Migration 2: Add Caching Layer

**Scenario:** All reads hit the database. Add Redis cache.

### Step 1: Identify hot read paths
```sql
SELECT query, calls FROM pg_stat_statements
WHERE query LIKE '%SELECT%'
ORDER BY calls DESC LIMIT 10;
```

### Step 2: Implement cache-aside on one endpoint
Start with the highest-call, lowest-write-frequency query.

### Step 3: Measure hit rate after 1 hour
```bash
redis-cli INFO stats | grep keyspace
# Target: hit_rate > 80%
```

### Step 4: Roll out to remaining hot paths

### Step 5: Set up hit rate alerting
Alert if hit_rate drops below 70% — indicates cache misconfiguration or workload change.

---

## Migration 3: Fix N+1 Queries

### Step 1: Detect
```sql
-- Find high-call queries with identical structure
SELECT regexp_replace(query, '\$[0-9]+', '?', 'g') AS normalized,
       count(*) AS variations, sum(calls) AS total_calls
FROM pg_stat_statements
GROUP BY normalized
HAVING count(*) > 10
ORDER BY total_calls DESC;
```

### Step 2: Add query count assertion to tests
```python
def test_no_n_plus_one(db_session):
    with count_queries() as counter:
        response = client.get('/api/users')
    assert counter.count <= 5, f"Expected ≤5 queries, got {counter.count}"
```

### Step 3: Fix in ORM, verify test passes

### Step 4: Monitor query count per request in production
