# 03-http-and-http2

## Problem

Http And Http2 is a critical layer of the network stack with direct implications for backend latency, throughput, and reliability.

## Why It Matters (Latency, Throughput, Cost)

Deep understanding of network protocols enables engineers to diagnose latency issues, optimize connection management, and design resilient systems.

## Mental Model

Network protocols operate as layered abstractions. Each layer provides guarantees and adds overhead.

## Underlying Theory

Builds on OSI model (01) and TCP fundamentals (02).

## Complexity Analysis

Network operations are dominated by propagation delay (O(distance/c)) and processing (O(packet_count)).

## Key Takeaways

1. Protocol understanding enables optimization beyond what framework defaults provide.
2. Each additional round trip multiplies by RTT — minimize round trips in hot paths.
3. Connection reuse, pipelining, and multiplexing all reduce RTT overhead.

## Related Modules

- `./01-osi-and-tcp-ip.md`
- `./02-tcp-deep-dive.md`
- `../../07-core-backend-engineering/02-connection-pooling.md`
