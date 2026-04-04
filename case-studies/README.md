# Case Studies

Real-world failure analysis and performance investigations. Each case study follows the structure:
1. **Incident:** What happened, impact, duration
2. **Investigation:** How it was diagnosed (tools, traces, metrics)
3. **Root Cause:** The underlying technical cause
4. **Fix:** What was changed and why
5. **Prevention:** What monitoring/code change prevents recurrence
6. **Lessons:** Generalizable takeaways

## Planned Case Studies

| Case Study | Domain | Root Cause |
|------------|--------|-----------|
| `01-n-plus-one-at-scale.md` | Databases | ORM lazy loading × 500k users |
| `02-pool-exhaustion-cascade.md` | Connection Pooling | Pool undersized + traffic spike |
| `03-cache-cold-start.md` | Caching | Deploy + cold cache + DB overload |
| `04-goroutine-leak.md` | Concurrency | Unclosed channels in long-running service |
| `05-index-regression.md` | Databases | Schema migration dropped index |

## Contributing Case Studies

Case studies must be anonymized (no client/company identifiers) and technically accurate. The root cause must be verifiable and the fix demonstrably effective.
