# Backend Scorecard

A lightweight scoring tool for quick system health assessment.

## Scoring Model

Rate each dimension 1–5:
- **1:** Not implemented / unknown
- **2:** Partially implemented, major gaps  
- **3:** Implemented but not tuned
- **4:** Implemented and tuned
- **5:** Implemented, tuned, and monitored with alerts

## Scorecard

```
System: _______________  Date: _______________  Engineer: _______________

DIMENSION                         SCORE  EVIDENCE / NOTES
─────────────────────────────────────────────────────────────────────
Query Performance                 [1-5]  ___________________________
  (no N+1, indexes, query plans reviewed)

Connection Management             [1-5]  ___________________________
  (pool configured, sized, leak detection)

Caching                           [1-5]  ___________________________
  (hit rate >80%, TTL set, eviction policy)

API Latency SLOs                  [1-5]  ___________________________
  (p99 <200ms read, <500ms write)

Observability                     [1-5]  ___________________________
  (RED metrics, distributed tracing, logs)

Reliability                       [1-5]  ___________________________
  (circuit breakers, retries, timeouts)

Security                          [1-5]  ___________________________
  (auth, rate limiting, input validation)

Scalability                       [1-5]  ___________________________
  (stateless, horizontal scaling tested)

TOTAL                             /40    ___________________________
```

## Score Interpretation

| Score | Interpretation | Action |
|-------|---------------|--------|
| 32–40 | Production-ready | Maintain; focus on continuous improvement |
| 24–31 | Good, gaps present | Address gaps within current quarter |
| 16–23 | Significant debt | Dedicate sprint to remediation |
| < 16 | High risk | Stop feature work, fix critical issues |

## Next Steps

For each dimension scoring 1–3, run the full audit from `../enterprise-kit/backend-audit-checklist.md` for that dimension to get specific action items.
