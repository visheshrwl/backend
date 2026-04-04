# Complexity Cheat Sheet

## Python Data Structures

| Operation | list | dict | set | deque |
|-----------|------|------|-----|-------|
| Access by index | O(1) | N/A | N/A | O(1) ends, O(N) middle |
| Search | O(N) | O(1) avg | O(1) avg | O(N) |
| Insert at end | O(1) amortized | O(1) amortized | O(1) amortized | O(1) |
| Insert at front | O(N) | N/A | N/A | O(1) |
| Delete by key | O(N) | O(1) avg | O(1) avg | O(N) |
| Membership test | O(N) | O(1) avg | O(1) avg | O(N) |

## Go Data Structures

| Operation | slice | map | channel (buffered) |
|-----------|-------|-----|--------------------|
| Access by index | O(1) | O(1) avg | N/A |
| Insert at end (append) | O(1) amortized | O(1) amortized | O(1) if not full |
| Delete by key | O(N) shift | O(1) avg | N/A |
| Range (for-range) | O(N) | O(N) | O(N) until closed |

## Database Operations

| Operation | Complexity | Condition |
|-----------|------------|-----------|
| Point lookup | O(log N) | Index exists |
| Point lookup (no index) | O(N) | Full table scan |
| Range scan | O(log N + K) | Index exists, K=rows returned |
| Sort result set | O(N log N) | No index on sort column |
| JOIN (hash join) | O(N+M) | Planner chooses hash join |
| JOIN (nested loop) | O(N×M) | Small inner table or index |
| INSERT | O(log N) | Updates all indexes |
| UPDATE with index | O(log N) update + O(log N) index | Per updated index |

## Redis Operations

| Command | Complexity | Notes |
|---------|------------|-------|
| GET/SET | O(1) | |
| MGET/MSET | O(N) | N = key count |
| HGET/HSET | O(1) | |
| HGETALL | O(N) | N = field count |
| LPUSH/RPUSH | O(1) | |
| LRANGE | O(S+N) | S = start offset |
| ZADD | O(log N) | |
| ZRANGE | O(log N + K) | K = elements returned |
| KEYS pattern | O(N) | ⚠ Never in production — blocks Redis |
| SCAN | O(1) per call, O(N) total | Use this instead of KEYS |

## HTTP / Network

| Item | Value |
|------|-------|
| TCP 3-way handshake | 1.5 RTT |
| TLS 1.3 handshake | 1 RTT |
| TLS 1.2 handshake | 2 RTT |
| DNS lookup (uncached) | 1 RTT to resolver + resolver's RTT to authoritative |
| HTTP/1.1 request | 1 RTT per request (no pipelining) |
| HTTP/2 request | 1 RTT for all multiplexed on established connection |
| gRPC (over HTTP/2) | 1 RTT for unary, streaming for streaming |
