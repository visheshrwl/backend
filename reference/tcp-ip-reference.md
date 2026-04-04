# TCP/IP Reference

## TCP Flags

| Flag | Meaning | When set |
|------|---------|----------|
| SYN | Synchronize | Connection initiation |
| ACK | Acknowledge | Every packet after handshake |
| FIN | Finish | Graceful connection close |
| RST | Reset | Abrupt connection close (error) |
| PSH | Push | Deliver data to application immediately (no Nagle delay) |
| URG | Urgent | Urgent data pointer (rarely used) |

## TCP Connection States

| State | Description |
|-------|-------------|
| CLOSED | No connection |
| LISTEN | Server waiting for incoming connections |
| SYN_SENT | Client sent SYN, waiting for SYN-ACK |
| SYN_RECEIVED | Server received SYN, sent SYN-ACK |
| ESTABLISHED | Connection active, data can flow |
| FIN_WAIT_1 | Sent FIN, waiting for ACK |
| FIN_WAIT_2 | Received ACK of FIN, waiting for remote FIN |
| TIME_WAIT | Waiting 2×MSL after FIN exchange |
| CLOSE_WAIT | Received FIN, not yet sent own FIN |
| LAST_ACK | Sent FIN in CLOSE_WAIT, waiting for ACK |

## Key Timers and Durations

| Timer | Default | Purpose |
|-------|---------|---------|
| MSL (Maximum Segment Lifetime) | 30–60s | Max time a TCP segment lives |
| TIME_WAIT | 2×MSL = 60–120s | Prevents delayed packet reuse |
| TCP keepalive idle | 7200s (2h) | After this idle time, send keepalive probes |
| TCP keepalive interval | 75s | Between keepalive probes |
| TCP keepalive count | 9 probes | Before declaring connection dead |
| Nagle delay | up to 40ms | Wait to coalesce small writes |
| SYN timeout | ~75s (with retries) | Connection attempt timeout |

## RTT Latencies (Approximate)

| Path | RTT |
|------|-----|
| Loopback (127.0.0.1) | <0.1ms |
| Same host, different process | <0.1ms |
| Same rack (LAN) | 0.1–0.5ms |
| Same datacenter | 0.5–2ms |
| Same region, different AZ | 2–10ms |
| US West → US East | 40–80ms |
| US → Europe | 80–120ms |
| US → Asia | 150–200ms |
