# Incident Response Playbook

## Severity Classification

| Severity | Definition | Response Time | Example |
|----------|------------|---------------|---------|
| P0 | Complete service outage | Immediate | 100% 5xx, no traffic |
| P1 | Significant degradation | < 15 min | p99 > 5s, >10% error rate |
| P2 | Partial degradation | < 1 hour | p99 > 1s, <10% error rate |
| P3 | Minor issue | Next business day | p99 elevated, no errors |

## Immediate Response (First 5 Minutes)

1. **Acknowledge** the alert — post in incident channel: "Investigating [alert name]"
2. **Assess scope:** How many users affected? Which endpoints?
3. **Check recent changes:** `git log --since="30 minutes ago"` — was there a recent deploy?
4. **Check the dashboard:** error rate, p99 latency, connection pool utilization, cache hit rate

## Investigation Decision Tree

```
Is error rate > 50%?
  YES → Check: recent deploy? → YES: rollback
        Check: DB connectivity? → NO: escalate infra
        Check: dependency outage? → YES: enable circuit breaker
  NO  → Check: latency elevated?
    YES → Check: DB query count spiking? → YES: N+1 regression
          Check: connection pool exhausted? → YES: see pool runbook
          Check: cache hit rate dropped? → YES: cache cold start
    NO  → False alarm or intermittent; investigate traces
```

## Rollback Procedure

```bash
# Kubernetes
kubectl rollout undo deployment/my-service
kubectl rollout status deployment/my-service

# Verify
curl -w "@curl-format.txt" https://api.example.com/health
```

## Escalation

Escalate to on-call lead if:
- P0 incident not resolved in 15 minutes
- Multiple services affected
- Data integrity concern (writes may be corrupted)
- Security concern (potential breach)

## Post-Incident

Within 48 hours:
1. Write blameless post-mortem
2. Add monitoring that would have caught this sooner
3. Document in `../../case-studies/`

## Related Modules

- `../../bsps/10-production-systems/02-incident-response.md` — theory
- `../../bsps/10-production-systems/01-observability.md` — what to look at
