# OSI and TCP/IP Models

## Problem

Backend engineers invoke networking concepts daily (TCP, HTTP, DNS, TLS) but often lack the layered model that explains *why* they interact the way they do. Without it, debugging network issues is guesswork.

## Why It Matters

Understanding the layered model explains:
- Why adding TLS doubles handshake RTTs
- Why HTTP/2 multiplexing reduces connection overhead
- Why DNS is separate from TCP and can be cached independently
- Why TCP retransmission affects application-level latency

## The TCP/IP Model

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 5: Application  │ HTTP, gRPC, WebSocket, DNS, SMTP       │
│                        │ Your application code lives here        │
├─────────────────────────────────────────────────────────────────┤
│  Layer 4: Transport    │ TCP, UDP, QUIC                          │
│                        │ Reliability, ordering, flow control     │
├─────────────────────────────────────────────────────────────────┤
│  Layer 3: Network      │ IP (IPv4, IPv6), ICMP                   │
│                        │ Addressing and routing                  │
├─────────────────────────────────────────────────────────────────┤
│  Layer 2: Data Link    │ Ethernet, WiFi (802.11)                 │
│                        │ Local network framing, MAC addresses    │
├─────────────────────────────────────────────────────────────────┤
│  Layer 1: Physical     │ Copper, fiber, radio                    │
│                        │ Bits on the wire                        │
└─────────────────────────────────────────────────────────────────┘

Each layer adds a header when sending, strips it when receiving.
A TCP segment wraps application data:
  [Ethernet header][IP header][TCP header][HTTP request body]
```

## The Cost of Each Layer Crossing

```
Application → TCP:  copy to kernel socket buffer (~200ns)
TCP → IP:           IP header construction (~50ns)
IP → NIC:           DMA transfer to NIC ring buffer (~100ns)
NIC → wire:         actual transmission (bandwidth / frame_size)
Wire → remote NIC:  propagation delay (distance / speed_of_light)
  LAN (1m):         ~3ns
  Same datacenter:  ~100μs
  Cross-AZ:         ~5ms
  Cross-country:    ~40ms
  US → Europe:      ~80ms
```

RTT (round-trip time) = 2 × one-way propagation + processing overhead.

## TCP Header

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
├─────────────────────────────┬─────────────────────────────────┤
│       Source Port           │       Destination Port          │
├─────────────────────────────────────────────────────────────────┤
│                        Sequence Number                          │
├─────────────────────────────────────────────────────────────────┤
│                    Acknowledgment Number                        │
├───────┬───────────┬─────────────────────────────────────────────┤
│ Offset│  Reserved │ Flags: URG ACK PSH RST SYN FIN             │
├───────┴───────────┴─────────────────────────────────────────────┤
│               Window Size (flow control)                        │
└─────────────────────────────────────────────────────────────────┘
```

The SYN and SYN-ACK flags in this header are why TCP connection setup costs 1.5 RTTs.

## Key Takeaways

1. Every network abstraction adds headers and processing cost.
2. Propagation delay is physics — RTT = 2 × distance / c. You can't optimize it, only minimize round trips.
3. TCP guarantees order and reliability at the cost of handshake overhead.
4. HTTP/2 and QUIC reduce the *number* of connections and RTTs needed.
5. DNS is a separate system with its own cache — a 50ms DNS lookup on every request is avoidable.

## Related Modules

- `./02-tcp-deep-dive.md` — TCP state machine and connection lifecycle
- `./03-http-and-http2.md` — Application protocol on top of TCP
- `../../07-core-backend-engineering/02-connection-pooling.md` — Why pooling eliminates handshake cost
