# Congestion Control

## Problem

TCP connections share network capacity. Without congestion control, all senders would transmit as fast as possible, causing routers to drop packets, triggering retransmissions, which cause more drops — a congestion collapse. Congestion control is TCP's mechanism for fairly sharing bandwidth and avoiding collapse.

## Why It Matters (Latency, Throughput, Cost)

```
Without congestion control (naive): all senders flood network
  → router buffers fill → 100% packet loss → 0 throughput (collapse)

With congestion control (CUBIC, BBR):
  → senders probe available bandwidth, back off on loss
  → fair sharing, 90%+ link utilization achievable
  → predictable latency
```

Bufferbloat (large router buffers + aggressive congestion control) can cause p99 latency 100× higher than p50 — a major cause of high tail latency in cloud environments.

## Mental Model

```
Congestion window (cwnd): how much data can be "in flight" at once
  (unacknowledged bytes in transit)

Send rate ≈ cwnd / RTT

CUBIC congestion control phases:
  1. Slow start:  cwnd doubles each RTT until loss (cwnd = 1, 2, 4, 8, 16, ...)
  2. CUBIC:       cwnd grows as cubic function of time since last loss
  3. Loss event:  cwnd cut to ~70% on packet loss (multiplicative decrease)
  4. BBR:         probes bandwidth and RTT explicitly, doesn't rely on loss signal
```

## CUBIC Algorithm (Linux default since kernel 2.6.19)

CUBIC grew cwnd based on a cubic function of time since last congestion event:

```
W(t) = C × (t - K)³ + W_max

Where:
  W_max = cwnd at last congestion event
  K = time it takes to reach W_max again
  C = cubic scaling factor (default 0.4)
  t = time since last congestion event
```

This grows cwnd slowly when far from the last congestion point, and quickly when approaching it — "CUBIC" shape.

## BBR Algorithm (Google, 2016)

BBR (Bottleneck Bandwidth and RTT) uses a different model: instead of reacting to packet loss, it directly measures the bottleneck bandwidth and RTT to compute the optimal send rate:

```
send_rate = BtlBw × (1 - GAIN)
where BtlBw = estimated bottleneck bandwidth

Probing phases:
  PROBE_BW: send at estimated BtlBw, filter for max
  PROBE_RTT: periodically drain pipe to measure true min RTT
  STARTUP: exponential growth until BtlBw plateaus
```

BBR achieves 2–25× higher throughput than CUBIC on links with random packet loss (e.g., wireless) where loss is not a congestion signal.

## Slow Start and New Connections

Every new TCP connection starts in slow start:

```
Initial cwnd (Linux): 10 × MSS ≈ 14KB
                      (increased from 3 in RFC 6928, 2013)

Time to reach 1MB cwnd with 40ms RTT (cross-Atlantic):
  14KB → 28KB → 56KB → 112KB → 224KB → 448KB → 896KB → 1.7MB
  7 RTTs × 40ms = 280ms before 1MB throughput possible
```

**This is why CDNs matter:** CDN servers are close (low RTT), so slow start completes faster. A CDN node 5ms away reaches 1MB cwnd in 7 × 5ms = 35ms vs 280ms from origin.

**This is another reason connection pooling matters:** A reused TCP connection has already completed slow start and has a grown cwnd. A new connection starts from 14KB again.

## ECN (Explicit Congestion Notification)

ECN allows routers to signal congestion without dropping packets:
- Router marks packets with CE (Congestion Experienced) bit instead of dropping
- Receiver echoes CE to sender via ECE flag in ACK
- Sender reduces cwnd immediately, before loss

ECN reduces retransmissions and lowers latency under moderate congestion.

```bash
# Enable ECN on Linux
sysctl -w net.ipv4.tcp_ecn=1
```

## Complexity Analysis

| Algorithm | Probing behavior | Loss reaction | Latency under congestion |
|-----------|-----------------|---------------|--------------------------|
| CUBIC | Cubic probe | Halve cwnd | Moderate (buffers fill) |
| BBR | Rate probe | Reduce to measured BtlBw | Low (doesn't fill buffers) |
| Reno | Linear probe | Halve cwnd | High (aggressive) |

## Benchmark

```
Transfer of 10MB file, 100ms RTT, 1% random packet loss:
  CUBIC: 45 seconds (loss treated as congestion, cwnd repeatedly halved)
  BBR:   8 seconds (random loss doesn't trigger cwnd reduction)

Same, 0% packet loss:
  CUBIC: 4 seconds
  BBR:   3.8 seconds (similar under ideal conditions)
```

## Observability

```bash
# Current TCP congestion algorithm
sysctl net.ipv4.tcp_congestion_control
# Linux default: cubic (or bbr if configured)

# Available algorithms
sysctl net.ipv4.tcp_available_congestion_control

# Enable BBR
sysctl -w net.ipv4.tcp_congestion_control=bbr

# Monitor TCP retransmissions (high retransmit = congestion or loss)
ss -s | grep -i retrans
netstat -s | grep -i retrans

# Per-connection cwnd (current congestion window)
ss -ti dst :5432  # connections to PostgreSQL port
# Look for: cwnd:N (current window size in segments)
```

## Failure Modes

**1. Bufferbloat:**
Large router/switch buffers allow queues to grow enormous before dropping. cwnd grows to fill the buffer → RTT spikes → p99 latency 100× higher than p50.
Mitigation: AQM (Active Queue Management): CoDel, FQ-CoDel on routers. Or use BBR which doesn't fill buffers.

**2. Spurious retransmissions (mobile/wireless):**
WiFi packet loss is not congestion — it is interference. CUBIC cuts cwnd on every loss. BBR detects whether loss correlates with RTT increase (congestion) vs not (wireless loss).

**3. Slow start on every new connection:**
As described above: 14KB initial cwnd × N RTTs to reach full throughput.
Mitigation: Connection pooling (reuse warm connections), TCP Fast Open (skip handshake for repeat clients).

## Key Takeaways

1. Slow start takes 7 RTTs to reach 1MB cwnd — new connections are slow to start.
2. BBR outperforms CUBIC on lossy links (wireless, cross-continent) — consider enabling it.
3. Bufferbloat causes high tail latency. AQM algorithms (CoDel) on routers fix this.
4. Connection reuse (pooling) skips slow start — another performance argument for pools.
5. ECN reduces retransmissions under moderate congestion — enable it in data center networks.

## Related Modules

- `./02-tcp-deep-dive.md` — TCP connection lifecycle and windowing
- `../../07-core-backend-engineering/02-connection-pooling.md` — slow start is another pool argument
