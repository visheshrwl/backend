# DNS and Load Balancing

## Problem

DNS maps human-readable names to IP addresses. Load balancers distribute traffic across multiple backend instances. Together, they are the entry point for all traffic to backend services — and both have failure modes that can cause complete service outages or subtle performance degradation.

## Why It Matters (Latency, Throughput, Cost)

```
Uncached DNS lookup:   50–200ms (resolver chain: client → resolver → root → TLD → auth)
Cached DNS lookup:     <1ms (local OS cache or resolver cache)
Missing DNS cache:     +200ms on first request = 4× overhead for a 50ms API call

Load balancer failure: all traffic fails (single point of failure)
Incorrect LB weights:  uneven load causes hot spots and cold instances
```

## Mental Model

```
Client Request Flow:
  Browser → "api.example.com" → OS resolver cache (TTL check)
    ├─► Cache HIT: IP immediately, no network query
    └─► Cache MISS: DNS query chain:
          Client → Resolver (ISP/8.8.8.8) → Root servers (.)
          → TLD servers (.com) → Authoritative server (example.com)
          → IP returned → cached at resolver and client for TTL seconds
                             │
                             ▼ (IP resolved)
                       Load Balancer (IP)
                         │         │
                    Backend 1   Backend 2   Backend 3
```

## DNS Resolution Chain

```
query: "api.example.com"

1. Check /etc/hosts (local override)              O(1)
2. Check OS DNS cache (TTL-based)                 O(1)
3. Query configured resolver (8.8.8.8)            1 RTT
   └─► If resolver has it cached: returns immediately
   └─► If not:
4. Resolver queries root servers (.)              1 RTT
5. Root returns .com nameservers
6. Resolver queries .com TLD nameservers          1 RTT
7. TLD returns example.com nameservers
8. Resolver queries example.com authoritative NS  1 RTT
9. Authoritative returns A record: 93.184.216.34
10. Resolver caches + returns to client

Total: 0 RTTs (cache hit) to 4+ RTTs (full resolution)
```

**TTL (Time To Live):** DNS records have a TTL. After TTL seconds, the resolver must re-query. Short TTL = faster failover, more DNS queries. Long TTL = slower failover, fewer queries.

```
TTL recommendations:
  Normal operation:     300s (5 minutes)
  Before planned failover: 60s (reduce ahead of time)
  After failover:       300s (increase back)
```

## Load Balancing Algorithms

### Round Robin
```
Request 1 → Backend 1
Request 2 → Backend 2
Request 3 → Backend 3
Request 4 → Backend 1 (cycle)
```
Simple. Ignores backend health and current load. Fast requests can overwhelm a single backend if they cluster.

### Weighted Round Robin
```
Backend 1: weight=3 (handles 3× more traffic — larger instance)
Backend 2: weight=1
Distribution: B1, B1, B1, B2, B1, B1, B1, B2 ...
```

### Least Connections
Route to the backend with the fewest active connections. Better for variable request duration.

### IP Hash / Sticky Sessions
```
client_ip → hash → consistent backend
```
Ensures the same client always goes to the same backend. Needed for session affinity. Breaks even distribution if clients have unequal traffic.

### Health Checking

Every load balancer must health-check backends:
```
Active check:  every 5–10 seconds, send GET /health/ready
               → HTTP 200: backend is healthy
               → HTTP 503 or timeout: mark unhealthy, remove from pool
               → After 3 consecutive successes: re-add to pool

Passive check: if backend returns 5xx, increase error count
               → error_rate > 50% in last 10 requests: mark unhealthy
```

## Complexity Analysis

| Operation | Complexity | Notes |
|-----------|------------|-------|
| DNS lookup (cached) | O(1) | Local memory lookup |
| DNS lookup (uncached) | O(1) with 4 RTTs | Fixed chain depth |
| Round-robin LB | O(1) | Simple counter |
| Least-connections LB | O(log N) | Min-heap of backend connections |
| Consistent hash LB | O(log N) | Binary search on hash ring |

## Benchmark

```
DNS resolution latency:
  Local cache hit:          0.01ms
  Resolver cache hit:       1–5ms
  Full resolution chain:    50–200ms

Load balancer overhead:
  L4 (TCP) load balancer:   0.1–0.5ms additional latency
  L7 (HTTP) load balancer:  1–3ms additional latency (parses HTTP headers)
  With TLS termination:     +1–2ms (TLS offload)
```

## Observability

```bash
# DNS diagnostics
dig api.example.com                      # full resolution chain
dig @8.8.8.8 api.example.com            # specific resolver
dig api.example.com +trace               # trace full resolution
nslookup api.example.com                # quick check

# Check current TTL remaining
dig api.example.com | grep -A1 "ANSWER SECTION"
# returns current TTL countdown

# Load balancer metrics to monitor:
# - requests per backend (detect uneven distribution)
# - active connections per backend
# - backend health check failure rate
# - p99 latency per backend (identify slow instances)
```

## Failure Modes

**1. DNS propagation lag:**
When you change a DNS record, old records persist in caches until their TTL expires. If TTL was 3600 (1 hour), some clients will hit the old IP for up to 1 hour after the change.
Mitigation: Lower TTL to 60s at least 1 TTL interval before the change.

**2. DNS cache poisoning:**
Attacker injects false DNS records to redirect traffic to a malicious IP.
Mitigation: DNSSEC (cryptographic signing of DNS responses).

**3. Backend health check flapping:**
A backend that is 90% healthy will toggle between healthy and unhealthy, causing unnecessary connection redistribution.
Mitigation: Hysteresis — require 3 consecutive successes to re-add, require 3 consecutive failures to remove.

**4. Thundering herd on TTL expiry:**
All clients with the same TTL expiry simultaneously query DNS, causing a spike on the authoritative nameserver.
Mitigation: Add jitter to TTL: `actual_ttl = configured_ttl ± random(0, 20%)`.

## Key Takeaways

1. DNS cache = free latency reduction. Long TTLs reduce DNS queries; short TTLs enable faster failover. Choose based on change frequency.
2. Reduce TTL to 60s before any planned DNS change. It takes one full TTL interval for the reduction to propagate.
3. Load balancers are single points of failure — deploy in pairs with keepalived or use cloud-managed LBs.
4. Least-connections outperforms round-robin when request durations vary significantly.
5. Health checks must be active AND have hysteresis to prevent flapping.

## Related Modules

- `./01-osi-and-tcp-ip.md` — DNS sits at the application layer
- `./02-tcp-deep-dive.md` — TCP connections to backends
- `../../08-systems-design/01-scalability-patterns.md` — load balancing in distributed systems
