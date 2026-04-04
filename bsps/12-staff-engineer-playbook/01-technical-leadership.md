# Technical Leadership

## What Is Technical Leadership?

Technical leadership is the practice of guiding engineering teams toward correct architectural decisions, setting technical direction, and developing the technical capability of the people around you. It is distinct from management: a staff engineer leads through technical influence, not organizational authority.

## Why It Matters

Poor technical leadership compounds over years:
- Wrong architectural decisions taken early cost 10–100× more to unwind later
- Underdeveloped engineers ship slower and make more errors
- Missing technical standards cause inconsistent systems that are hard to operate

## Core Responsibilities

**Decision-making:**
- Frame decisions with explicit trade-offs, not opinions
- Write ADRs (Architecture Decision Records) for reversible and irreversible decisions
- Distinguish reversible decisions (can move fast) from irreversible ones (slow down, validate)

**Technical direction:**
- Define and maintain standards in code review, testing, observability
- Identify and address technical debt before it becomes a blocker
- Anticipate bottlenecks 6–18 months ahead based on growth projections

**People development:**
- Pair on hard problems instead of solving them alone
- Give feedback on technical judgment, not just code correctness
- Create stretch assignments that develop the next level's skills

## The Influence Model

Staff engineers lead without authority. Influence comes from:

```
Technical credibility: Track record of being right, especially on hard calls
Clarity of communication: Can explain complex trade-offs simply
Consistency: Same standards applied to your code and others
Availability: Present at the moment decisions are being made
```

## Deciding vs Advising vs Delegating

```
Decision type          Your role
─────────────────────────────────────────────────────────
Cross-team arch        Decide (with stakeholder input)
Team-level arch        Advise + document in ADR
Feature implementation Delegate (review output)
Hotfix during incident Advise (support, don't take over)
```

## Anti-Patterns

**The Hero:** Solves every hard problem personally. Creates a single point of failure, atrophies the team.

**The Gatekeeper:** Blocks PRs with style preferences instead of substantive feedback. Slows team velocity without quality gain.

**The Ivory Tower:** Makes architectural decisions without understanding operational reality. Designs systems nobody can run.

**The Consensus Seeker:** Waits for unanimous agreement before deciding. Optimal technical decisions are rarely popular; they need explanation, not votes.

## Measuring Technical Leadership Effectiveness

| Metric | What It Measures | Target |
|--------|-----------------|--------|
| Incident root causes | Are the same classes of bugs recurring? | Decreasing repetition |
| Time to onboard new engineer | Documentation and standards quality | < 2 weeks to first PR |
| ADR count | Are decisions being documented? | ≥ 1 per significant decision |
| p99 latency trend | Is technical quality improving? | Improving or stable |
| Team velocity after staff eng absence | Has knowledge been transferred? | Minimal change |

## Key Takeaways

1. Technical leadership is measured by team output, not personal output.
2. The best technical leaders spend more time writing ADRs and reviewing designs than writing code.
3. "Move fast" and "technical quality" are not opposites — they are directly correlated over 12+ month horizons.
4. Your job is to raise the technical ceiling of your team, not to be the ceiling.
5. Influence requires investment in relationships and communication, not just being technically correct.

## Related Modules

- `./02-architecture-decision-records.md` — the primary artifact of technical decision-making
- `./03-system-reviews.md` — how to evaluate system quality at scale
- `../../enterprise-kit/backend-audit-checklist.md` — measurable standards for technical quality
