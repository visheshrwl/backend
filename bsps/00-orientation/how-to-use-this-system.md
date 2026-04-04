# How to Use This System

## Navigation Principles

BSPS is organized as a **dependency graph**, not a flat list. Some modules require knowledge from others.

```
01-mathematics ──┐
02-dsa ──────────┼──► 07-core-backend (most important for practitioners)
03-os ───────────┤
04-networks ─────┘
       │
       ▼
05-network-programming ──► 06-databases ──► 08-systems-design
                                              │
                                              ▼
                           09-performance ──► 10-production ──► 11-real-world
                                                                      │
                                                                      ▼
                                                              12-staff-engineer
```

## Reading Strategy

**Active reading:** Every module has a lab or benchmark. Run it. The numbers are more memorable than prose.

**Cross-references:** Each module links to the theory that explains it. When you hit a concept you don't understand, follow the link to the foundational module and read that first.

**Revisit:** The first time you read a module, some parts will be abstract. After running the lab and writing production code for a few weeks, re-read the module. The second reading is always more valuable.

## Module Types

- **Theory modules** (`01-mathematics`, `03-os`, `04-networks`): Read carefully. The math and OS concepts underpin everything else.
- **Implementation modules** (`07-core-backend`): Read, then immediately run the code examples. Change the parameters. Break things.
- **Labs**: Runnable Python/Go/Node.js exercises. Time yourself. Aim to complete each in under 2 hours.
- **Benchmarks**: Run on your own machine. Compare your results to the expected values in the README.
