# Architecture Decision Records

## What Is an ADR?

An Architecture Decision Record (ADR) is a short document that captures a significant architectural decision: the context that necessitated it, the decision made, and the consequences of that decision.

ADRs are **not** design documents. They are records of decisions already made — or being finalized — including the alternatives considered and why they were rejected.

## Why ADRs Matter

Without ADRs:
- New engineers spend weeks reverse-engineering why systems are built the way they are
- "Why don't we just use X?" is asked repeatedly in every new team member's first month
- Decisions are re-litigated from scratch when the original decision-makers leave
- Context about constraints that no longer exist (but shaped the design) is lost

With ADRs:
- New engineers onboard in days, not weeks
- Technical debt decisions are explicit and traceable
- Trade-offs are visible to auditors, security reviewers, and future engineers

## ADR Format

```markdown
# ADR-{number}: {Title}

**Date:** YYYY-MM-DD
**Status:** [Proposed | Accepted | Deprecated | Superseded by ADR-N]
**Deciders:** {Names or roles}

## Context

What is the situation that requires a decision?
What constraints or requirements exist?
What is the consequence of not deciding?

## Decision

What was decided?
State it in the active voice: "We will use X because..."

## Considered Alternatives

### Alternative A: {Name}
Pros:
- ...
Cons:
- ...
Why rejected: ...

### Alternative B: {Name}
...

## Consequences

What becomes easier or harder because of this decision?
What follow-up decisions does this create?
What technical debt is being knowingly accepted?

## References

- Links to RFCs, blog posts, internal data that informed the decision
```

## When to Write an ADR

Write an ADR for decisions that are:
- **Hard to reverse** (database choice, framework choice, data model)
- **Cross-team impact** (API contract changes, shared service behavior)
- **Require significant context** (why we chose eventual consistency here)
- **Create technical debt** (shortcuts taken with known future cost)

Do NOT write an ADR for:
- Implementation details within a component
- Stylistic choices (covered by STYLE_GUIDE)
- Decisions that will obviously be revisited soon

## ADR Lifecycle

```
Proposed → (review period, typically 1 week) → Accepted → [in use]
                                                         ↓
                                              Deprecated (retired)
                                              Superseded by ADR-N (replaced)
```

## Example: ADR-007: Connection Pooling Strategy

```markdown
# ADR-007: Use pgx Connection Pool Over Standard database/sql

**Date:** 2024-01-15
**Status:** Accepted
**Deciders:** Platform team

## Context

Our PostgreSQL-backed services were creating new connections per request.
At peak load (500 req/s), this caused 500 TCP handshakes/second against
the database, adding 15ms overhead per request and exhausting DB connections.

## Decision

We will use pgx/pgxpool for all PostgreSQL connections, configured with:
- MinConns: 5, MaxConns: 20 (based on Little's Law: 500 req/s × 0.015s × 1.3)
- MaxConnLifetime: 30 minutes
- Health check period: 1 minute

## Considered Alternatives

### standard database/sql with lib/pq
Pros: Standard library interface, familiar
Cons: No native connection pool; requires third-party pool; lower performance
Why rejected: pgx provides 40% higher throughput in benchmarks.

### PgBouncer (external proxy)
Pros: Language-agnostic, handles serverless patterns
Cons: Additional infrastructure component; adds latency hop
Why rejected: Not needed at current scale; revisit if we move to Lambda.

## Consequences

- All new services must use pgx pool; legacy services migrated by Q2
- Pool metrics must be exported (active, idle, wait time)
- Developers must not hold connections across external HTTP calls
- Follow-up ADR needed for connection pool in Lambda environments
```

## ADR Numbering and Storage

Store ADRs in the repository under `docs/adr/` or `decisions/`:
```
docs/
  adr/
    001-use-postgresql-over-mysql.md
    002-event-driven-for-notifications.md
    007-connection-pooling-strategy.md
```

Number sequentially. Never reuse a number. Superseded ADRs are kept for history.

## Key Takeaways

1. ADRs are the most valuable documentation investment a team can make — they depreciate slowly.
2. Write them when the decision is being made, not after. Reconstruction produces poorer documents.
3. The "Consequences" section is the most important — honest about trade-offs accepted.
4. "Superseded by ADR-N" is a success pattern, not a failure — decisions should evolve.
5. An ADR library is a team's institutional memory. Protect it.

## Related Modules

- `./01-technical-leadership.md` — when and how to drive architectural decisions
- `./03-system-reviews.md` — ADRs are reviewed during system reviews
