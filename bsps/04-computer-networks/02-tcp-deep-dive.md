# TCP Deep Dive

## Problem

TCP's reliability guarantees come at a cost: connection setup (1.5 RTT), connection teardown (2 RTT), head-of-line blocking, and retransmission delays. Knowing exactly what TCP does explains why every optimization in connection management (pooling, keepalive, HTTP/2) works.

## Why It Matters (Latency, Throughput, Cost)

```
TCP connection lifecycle costs:
  Handshake (SYN/SYN-ACK/ACK):  1.5 RTT = 15ms (cross-AZ)
  TLS 1.3 on top:                1 RTT additional = 10ms
  First data exchange:           1 RTT
  Total to first byte (TTFB):    3.5 RTT = 35ms

With connection pool (reuse):
  Time to first byte:            0 (connection already ESTABLISHED)
  First data exchange:           1 RTT = 10ms
```

## TCP State Machine

```
                    CLOSED
                      │
              SYN sent│ (client)
                      ▼
                  SYN_SENT ──────────────────────────────┐
                      │  SYN received + SYN-ACK sent     │
                      │  (server: LISTEN → SYN_RECEIVED) │
                  ACK received                           │
                      ▼                                  │
                 ESTABLISHED ◄──────────────────────────┘
                  (data flows)
                      │
              FIN sent│ (active close)
                      ▼
                FIN_WAIT_1
                      │ FIN-ACK received
                      ▼
                FIN_WAIT_2
                      │ FIN received from remote
                      ▼
                 TIME_WAIT  ← stays here for 2×MSL (60–120 seconds)
                      │
                   CLOSED
```

**TIME_WAIT:** The connection stays in TIME_WAIT for 2 × MSL (Maximum Segment Lifetime, typically 60s) to ensure delayed packets from the old connection don't corrupt a new connection on the same port pair. This is why rapidly cycling connections (no pool) can exhaust ephemeral ports (EADDRINUSE).

## TCP Flow Control and Congestion Control

**Flow control (receive window):** Receiver advertises how much buffer space it has. Sender cannot send more than the window allows.

```
Bandwidth-delay product (BDP):
  BDP = bandwidth × RTT

For a 1Gbps link with 10ms RTT:
  BDP = 1,000,000,000 × 0.010 = 10,000,000 bytes = 10MB

TCP window must be at least 10MB to fully utilize this link.
Default TCP receive buffer: 4MB → only 40% link utilization!
Tune: net.core.rmem_max and net.ipv4.tcp_rmem
```

**Slow start:** New TCP connections start with a small congestion window (cwnd = ~10 MSS = ~14KB). cwnd doubles each RTT until packet loss is detected. This is why a fresh connection to a CDN is slower than a warm one.

## Nagle's Algorithm

Nagle's algorithm coalesces small writes to reduce packet count:
- Buffer small writes until: ACK received OR buffer ≥ MSS OR 40ms timeout

**Impact on backend:** A database client sending a small query followed by `recv()` will wait up to 40ms for Nagle before the query is sent. Fix: `TCP_NODELAY` socket option.

```python
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # disable Nagle
```

Most database drivers set `TCP_NODELAY` by default. Verify yours does.

## Key Takeaways

1. TCP connection = 1.5 RTT setup + 2 RTT teardown. Pool connections to avoid this.
2. TIME_WAIT (2×MSL) is intentional — it prevents packet reuse bugs. Don't try to eliminate it; increase your ephemeral port range instead.
3. Slow start means fresh connections have low throughput initially. Warm connections perform better — another argument for connection pooling.
4. BDP = bandwidth × RTT. Your TCP window must be ≥ BDP to saturate a link.
5. Nagle's algorithm delays small writes up to 40ms. Set `TCP_NODELAY` for database and RPC connections.

## Related Modules

- `./05-congestion-control.md` — CUBIC, BBR algorithms
- `../../07-core-backend-engineering/02-connection-pooling.md` — TCP cost eliminated by pooling
