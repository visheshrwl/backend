---
title: The Go Engineering Cheat Sheet
description: A dense, production-oriented reference — the cost model, the internals, the concurrency rules, the tooling, and the review checklist from the whole Go guide on one page.
tags: ["go", "cheatsheet", "reference", "performance", "concurrency"]
status: published
access: public
publishedAt: 2026-07-08
---

# The Go Engineering Cheat Sheet

The one-page distillation of the guide. Keep it open while building or reviewing.

## The cost model (what allocates, what's free)

```
FREE (stack, no GC)                     COSTS (heap, GC-tracked)
─────────────────────                   ────────────────────────
values that don't escape                returning &local
fixed-size arrays kept local            storing a value in an interface (boxing)
small struct copies                     any / interface{} / fmt.* on hot paths
preallocated slices/maps                append past cap (realloc + copy)
reused buffers (sync.Pool, s[:0])       make([]T, n) with dynamic n that escapes
                                        closures capturing escaping vars
Verify: go build -gcflags='-m'   |   Confirm: go test -bench=. -benchmem
```

## Data structure internals (one line each)

| Type | Representation | The thing that bites you |
|---|---|---|
| slice | `{ptr, len, cap}` 24 B header over shared array | append aliasing; sub-slice pins whole array; preallocate! |
| map | bucketed / Swiss table (1.24+) | concurrent write = `fatal error`; never shrinks; random order |
| string | `{ptr, len}` immutable bytes | `[]byte(s)`/`string(b)` copies unless optimized |
| interface | `{itab, data}` 2 words | boxing a value allocates; typed-nil ≠ nil |
| channel | `hchan`: mutex + ring buffer + wait queues | send/close on closed = panic; sender closes |
| goroutine | ~8 KB growable stack, user-scheduled | leaks if no stop condition; bound concurrency |

## Pointers vs values

```
Use a POINTER for:   mutation / shared identity, or avoiding a large copy.
Use a VALUE for:     everything else (simpler, no nil, cache-friendly, no escape).
Default to values. "Pointer everywhere for safety" causes escapes + nil panics.
```

## Concurrency rules (non-negotiable)

```
1. Shared mutable data needs a happens-before edge (channel/mutex/atomic).
   No edge = data race = undefined behavior. Run `go test -race`.
2. Mutex/atomic to GUARD state; channels to ORCHESTRATE goroutines.
   (channel-as-counter is ~8x slower than a mutex)
3. Sender closes a channel; with N senders, one coordinator closes after all done.
4. Every long-lived goroutine: select { case <-ctx.Done(): return }.
5. Bound concurrency — worker pool or semaphore, never unbounded `go`.
6. context: first param, always defer cancel(), derive from incoming ctx,
   never store in a struct, never a fresh Background() mid-request.
```

## The scheduler (G-M-P) in five lines

```
G = goroutine, M = OS thread, P = scheduling context (GOMAXPROCS of them).
An M needs a P to run Go. Idle P steals half of another P's run queue.
Blocking syscall  → P handed off to another M (costs a thread).
Network I/O       → goroutine parked on netpoller (epoll); M runs other work.
Async preemption (1.14+) stops hot loops so they can't starve GC/others.
```

## Garbage collection

```
Concurrent tri-color mark-sweep. Non-generational, non-compacting.
Two brief STW pauses/cycle (sub-ms). Write barrier on during marking.
Knobs:  GOGC=100        throughput vs footprint (heap growth target)
        GOMEMLIMIT      soft cap — set to ~90-95% of container limit (stops OOM)
Real fix for GC cost = ALLOCATE LESS (stack, pool, prealloc), not tune knobs.
Diagnose: GODEBUG=gctrace=1  +  /debug/pprof/heap
```

## Errors

```
Return errors for expected failures; panic only for bugs/invariants;
recover only at boundaries (can't catch fatal errors like concurrent map write).
Wrap: fmt.Errorf("context: %w", err)      (%w wraps, %v flattens/seals)
Inspect: errors.Is(err, Sentinel)   errors.As(err, &typedErr)
NEVER string-match errors. Return untyped nil (typed-nil-in-interface trap).
Model opaque by default; sentinel/typed only where a caller branches.
```

## Production HTTP server (don't ship without these)

```go
srv := &http.Server{
    Handler:           h,
    ReadHeaderTimeout: 5 * time.Second,   // slow-loris defense
    ReadTimeout:       15 * time.Second,
    WriteTimeout:      15 * time.Second,
    IdleTimeout:       60 * time.Second,
    MaxHeaderBytes:    1 << 20,
}
// + http.MaxBytesReader on bodies
// + graceful shutdown: srv.Shutdown(ctx) on SIGTERM with a drain deadline
// middleware order (outer→inner): Recover → RequestID/Obs → Auth → handler
```

## Tooling (muscle memory)

```
go build -gcflags='-m'         escape analysis + inlining (add -m -m for reasons)
go test -bench=. -benchmem     ns/op, B/op, allocs/op
go test -race                  data race detector (run in CI)
go tool pprof <profile>        CPU / heap / block / mutex
go tool trace trace.out        scheduler + GC + netpoll timeline
GODEBUG=gctrace=1              per-cycle GC log
GODEBUG=schedtrace=1000        scheduler state every 1s
go vet / staticcheck           static analysis (typed-nil, lost cancels, ...)
```

## Code-review checklist (the five questions)

```
[ ] Allocation:   does this need to allocate? (any/fmt/append-in-loop on hot path?)
[ ] Ownership:    every goroutine has a stop condition (ctx/done/WaitGroup)?
[ ] Interfaces:   small, consumer-defined? not boxing values on a hot path?
[ ] Errors:       handled/returned/explicitly ignored? wrapped with %w? logged once?
[ ] Concurrency:  shared state synchronized on EVERY access? -race clean?
```

## Version-specific facts worth knowing

```
1.8   hybrid write barrier (no stack rescans)
1.13  error wrapping (%w, errors.Is/As)
1.14  asynchronous preemption
1.19  GOMEMLIMIT; formalized memory model for sync/atomic
1.21  built-in min/max/clear; log/slog structured logging
1.22  per-iteration loop variables (fixes the classic capture bug)
1.24  map implementation → Swiss tables
1.25  container-aware GOMAXPROCS by default; experimental Green Tea GC
```

## Idiom quick-reference

```
Accept interfaces, return structs.        Interfaces at the consumer, small.
make([]T, 0, n) when size is known.        s = s[:0] to reuse backing array.
Copy small sub-slices out of big buffers.  Three-index s[a:b:c] to cap capacity.
Errors are values; wrap with %w.           context first param; defer cancel().
Don't communicate by sharing memory;       ...but use a mutex to guard state.
  share memory by communicating —          Profile before optimizing; measure, don't guess.
```

---

Back to → [The Go Engineering Handbook](/backend-guide/go/README)
