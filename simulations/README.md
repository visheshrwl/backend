# Simulations

System behavior simulators for understanding emergent properties that are hard to demonstrate with static benchmarks.

## Planned Simulations

| Simulation | What It Models |
|------------|---------------|
| `queueing-theory-demo/` | M/M/c queue behavior: latency explosion at high utilization |
| `cache-stampede/` | Thundering herd at cache expiry, with and without mitigation |
| `connection-pool-saturation/` | Pool exhaustion cascade with circuit breaker vs without |
| `tcp-slow-start/` | Bandwidth-delay product and why pooled connections are faster |

## Contributing Simulations

See `CONTRIBUTING.md` for standards. Simulations must:
- Be runnable with standard library only
- Print a visual output (ASCII chart or text table)
- Include expected output and interpretation guide
