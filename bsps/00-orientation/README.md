# Module 00: Orientation

## Purpose

This module answers three questions before you start:

1. **What is BSPS and how is it structured?**
2. **What do you already need to know?**
3. **What is the most efficient path through the material for your goal?**

---

## Contents

| File | Description |
|------|-------------|
| `how-to-use-this-system.md` | Navigation guide, module dependency map |
| `learning-path.md` | Detailed paths for each experience level |
| `prerequisites.md` | What to know before starting each module |

---

## The Core Idea

Most backend performance problems are not framework problems. They are **computer science problems** that manifest at the framework layer.

When an API is slow because of N+1 queries, the fix is not "upgrade your ORM". The fix is understanding why O(N) queries is worse than O(1) — an algorithms problem — combined with understanding that each query crosses the network — a networking problem. Both of those root causes are covered in BSPS.

## Start Here

Read `how-to-use-this-system.md` first. Then `prerequisites.md` to assess where you are. Then `learning-path.md` to choose your path.
