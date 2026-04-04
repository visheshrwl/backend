# Contributing to BSPS

## Overview

BSPS grows by adding modules that are **deep, precise, and runnable**. The bar is high: every contribution must include real content, real benchmarks, and working code. No placeholders, no "TODO: fill in later".

---

## How to Propose a New Module

1. **Check for overlap.** Does this module belong in an existing file or warrant a new one? A single concept belongs in a section of an existing module. A cluster of related concepts with distinct theory, implementation, and benchmarks warrants a new file.

2. **Open a discussion** (GitHub Discussions) describing:
   - What concept the module covers
   - Which existing modules it cross-references
   - What benchmark data you can include
   - What languages you will implement examples in (Python + Go + Node.js required)

3. **Write the module** following the mandatory template (see below).

4. **Submit a PR** with the new file and updates to the parent `README.md` index.

---

## How to Propose Corrections

For factual errors, open a PR with:
- The corrected content
- A source or benchmark that validates the correction
- A note in the PR description explaining what was wrong and why

For typos and formatting, a PR is sufficient without discussion.

---

## Mandatory Content Template

Every concept file MUST include all of these sections:

```markdown
# Title

## Problem
## Why it matters (latency, throughput, cost)
## Mental Model
## Underlying Theory (OS / CN / DSA / Math linkage)
## Naive Approach
## Why it fails at scale
## Optimized Approach
## Complexity Analysis (time, space)
## Benchmark (p50, p99, CPU, memory)
## Observability (metrics, tracing, logs)
## Multi-language Implementation (Python, Go, Node.js)
## Trade-offs
## Failure Modes
## When NOT to use
## Lab
## Key Takeaways
```

**Sections you may omit only with justification:**
- `Naive Approach` — if there is no naive approach (purely additive patterns)
- `Multi-language Implementation` — if the concept is language-agnostic theory (e.g., queueing theory math)

**Sections you must never omit:**
- `Benchmark` with real numbers (p50, p99, CPU, memory)
- `Failure Modes`
- `When NOT to use`
- `Key Takeaways`

---

## Content Quality Standards

### Benchmark requirement

Every module must include benchmark data. Acceptable formats:

1. **Table with real numbers** — run the code yourself, report actual measurements
2. **ASCII benchmark table** — labeled with setup conditions (hardware, OS, data size)
3. **Comparative table** — showing the naive vs optimized approach difference

Do NOT write "benchmark results depend on your hardware" as a substitute. Write "on a MacBook M2 with 16GB RAM, running PostgreSQL 15 in Docker..." and give real numbers.

### Code quality standards

All code examples must be:
- **Syntactically correct** and tested by the author
- **Idiomatic** for the language (PEP 8 for Python, gofmt for Go, ESLint standard for Node.js)
- **Self-contained** when possible (copy-paste runnable without setup)
- **Labeled** with the language at the top of the code block

### Cross-reference requirement

Every module must link to at least 2 other modules that explain the underlying theory. Format:

```markdown
See `../../bsps/03-operating-systems/01-processes-and-threads.md` for OS thread internals.
```

---

## How to Add Labs

Labs live in `labs/lab-NN-name/README.md` and must include:

1. **Complete runnable code** — no dependencies beyond Python standard library, or clearly stated dependencies
2. **Expected output** — what the reader should see when they run it
3. **Step-by-step instructions** — numbered, no gaps
4. **Extension exercises** — 2–3 suggestions for deeper exploration
5. **Checklist** — what the reader should be able to explain after completing the lab

Lab code standards:
- Python labs: standard library only (sqlite3, threading, asyncio, time, statistics)
- Go labs: standard library + one well-known dependency maximum
- Node.js labs: built-ins only (net, http, worker_threads, fs/promises)

---

## How to Add Benchmarks

Benchmarks live in `benchmarks/NN-name/README.md` and must include:

1. **Setup code** — complete, runnable scripts in the relevant language(s)
2. **Methodology** — what you're measuring and why
3. **Results table** — with hardware/OS/version context
4. **Analysis** — explain why the results look the way they do

---

## Review Criteria

PRs will be reviewed for:

- [ ] Mandatory template sections all present
- [ ] Benchmark data included with real numbers
- [ ] Code is runnable and correct (reviewer will test it)
- [ ] Cross-references to related modules included
- [ ] No placeholder text anywhere
- [ ] Failure modes section is non-trivial (at least 2 real failure scenarios)
- [ ] "When NOT to use" section is honest about the pattern's limits

PRs missing any of the above will be returned for revision.

---

## Style Guide Reference

See `STYLE_GUIDE.md` for:
- Markdown formatting conventions
- ASCII diagram standards
- Complexity notation format
- Code style per language
