# System Reviews

## What Is a System Review?

A system review is a structured evaluation of a production system's architecture, implementation quality, and operational posture. It is distinct from a code review (tactical, PR-level) and a design review (prospective, pre-build). System reviews are retrospective and comprehensive.

## When to Conduct a System Review

- Before a system is handed off to a new team
- After a significant incident whose root cause implicated system design
- When a system's scale changes by 10× (new requirements, not new code)
- Annually for any system with > $100k/year operational cost
- When considering a major re-architecture investment

## Review Structure

**Preparation (1 week before review):**
1. Collect metrics: p50/p99 latency, error rate, throughput for last 90 days
2. Run `enterprise-kit/backend-audit-checklist.md`
3. List all ADRs for the system
4. List known technical debt (backlog items tagged "tech-debt")

**Review Meeting (2-3 hours):**

### 1. Observability Assessment (30 min)
Questions:
- Can you tell, within 5 minutes, when the system is degraded?
- Do alerts fire before users notice?
- Can you trace a single request from entry to exit across all components?

Pass criteria: RED metrics (Rate/Errors/Duration) per endpoint, distributed traces, structured logs.

### 2. Performance Profile (45 min)
```
For each critical path:
  □ What is the p50 and p99 latency? What is the target?
  □ How many database queries per request?
  □ What is the cache hit rate?
  □ What is the connection pool utilization at peak?
  □ Are there known N+1 patterns?
```

### 3. Reliability Assessment (30 min)
- What are the top 3 single points of failure?
- Is there a circuit breaker on every external dependency?
- What happens when the database is unavailable for 30 seconds?
- What is the RTO (Recovery Time Objective) and RPO (Recovery Point Objective)?

### 4. Scalability Assessment (30 min)
- At what load does the current architecture break?
- What is the cost to scale 2×? 10×?
- Are there stateful components that prevent horizontal scaling?

### 5. Security Assessment (15 min)
- Are all endpoints authenticated?
- Is there rate limiting on public endpoints?
- When were dependencies last scanned for CVEs?

### 6. Technical Debt Inventory (30 min)
For each known debt item:
- What is the cost of carrying it (reliability risk, performance impact, development friction)?
- What is the cost to pay it down?
- What is the recommended priority?

## Review Output

The review produces a written report with:

```
System: _______________  Review Date: _______________

SUMMARY
  Overall health: [Green / Yellow / Red]
  Immediate actions required: [list]

FINDINGS
  [Finding]: [Description]
  [Severity]: [Critical / High / Medium / Low]
  [Recommendation]: [Specific action with owner and deadline]
  [Linked ADR/ticket]: [reference]

METRICS BASELINE (for next review comparison)
  p99 latency: ___ms  Error rate: ___%  Throughput: ___/s

NEXT REVIEW: [Date]
```

## Severity Definitions

| Severity | Definition | Response |
|----------|-----------|----------|
| Critical | Data loss risk, security breach, or imminent outage | Fix within 1 week |
| High | Significant reliability/performance issue | Fix within 1 sprint |
| Medium | Technical debt with tangible cost | Plan within quarter |
| Low | Improvement opportunity | Backlog |

## System Review vs Architecture Review

| | System Review | Architecture Review |
|--|--------------|---------------------|
| Timing | Retrospective (system exists) | Prospective (pre-build) |
| Focus | Current state quality | Proposed design soundness |
| Output | Remediation plan | Approval or redesign request |
| Duration | 2–3 hours | 1–2 hours |

## Key Takeaways

1. System reviews are how institutional knowledge is transferred between teams.
2. The hardest part is quantifying "good enough" — use the audit checklist thresholds.
3. Always produce a written report. Verbal reviews produce no accountability.
4. Severity ratings must be honest. "Low" severity is how critical debt hides for years.
5. The next review date is mandatory. Without it, the review is a one-time event, not a practice.

## Related Modules

- `./01-technical-leadership.md` — who leads system reviews
- `./02-architecture-decision-records.md` — ADRs provide context for system reviews
- `../../enterprise-kit/backend-audit-checklist.md` — structured checklist for the review
- `../../bsps/10-production-systems/01-observability.md` — what to examine during review
