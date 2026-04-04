# Backend Engineer Onboarding

## Week 1: Foundations

**Goal:** Understand what BSPS is and where you are in your learning journey.

### Day 1-2: Orientation
1. Read `../../README.md` — understand the system
2. Read `../../bsps/00-orientation/prerequisites.md` — assess your starting point
3. Choose a learning path from `../../bsps/00-orientation/learning-path.md`

### Day 3-4: First Core Module
1. Read `../../bsps/07-core-backend-engineering/01-n-plus-one-query-problem.md`
2. Run Lab 01: `../../labs/lab-01-n-plus-one-profiling/`
3. Find one N+1 pattern in the codebase you are onboarding into

### Day 5: Audit Your First System
1. Run `backend-audit-checklist.md` on the primary service you own
2. Score each section
3. Identify the top 3 action items

## Week 2: Connection and Caching

1. Read `../../bsps/07-core-backend-engineering/02-connection-pooling.md`
2. Run Lab 02: `../../labs/lab-02-connection-pool-tuning/`
3. Verify pool configuration in your service
4. Read `../../bsps/07-core-backend-engineering/03-caching-strategy.md`
5. Check cache hit rates in your service's metrics

## Week 3: Concurrency and Performance

1. Read `../../bsps/07-core-backend-engineering/04-threading-vs-async-vs-event-loop.md`
2. Run Benchmark 01: `../../benchmarks/01-thread-vs-async-vs-event-loop/`
3. Profile one slow endpoint with your language's profiling tools
4. Review `../../playbooks/performance-tuning.md`

## Week 4: Production Readiness

1. Read `../../bsps/10-production-systems/01-observability.md`
2. Verify your service exports RED metrics
3. Read `../../playbooks/incident-response.md`
4. Shadow one on-call rotation

## Checklist: Ready for Independent Operation

- [ ] Can explain N+1 and fix it in your codebase's ORM
- [ ] Know your service's pool size and why it was chosen
- [ ] Know your service's cache hit rate and whether it's acceptable
- [ ] Have run at least one profiling session on your service
- [ ] Have read through the incident response playbook
- [ ] Completed backend audit checklist for your primary service
