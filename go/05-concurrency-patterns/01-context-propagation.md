---
title: Context Propagation — Cancellation, Deadlines, and Request Scope
description: What context.Context is, how the cancellation tree works, correct deadline/timeout propagation, the rules that prevent goroutine leaks, and how context flows through a real request path.
tags: ["go", "context", "cancellation", "concurrency", "timeouts", "production"]
status: published
access: gated
publishedAt: 2026-07-08
---

# Context Propagation — Cancellation, Deadlines, and Request Scope

## Learning objectives

You will understand what `context.Context` is and why it exists, how the cancellation tree propagates cancellation and deadlines, the rules that make context prevent (rather than cause) goroutine leaks, and how context threads through a production request path from HTTP handler to database query.

## Why this matters

`context.Context` is the backbone of every production Go service's cancellation and timeout story. It is how a cancelled HTTP request stops the database query it triggered, how a 2-second deadline propagates from the edge down to every downstream call, and how you avoid the goroutine leaks that come from work that outlives the thing that requested it. Nearly every function in a real service takes a `ctx context.Context` as its first parameter — understanding why is understanding how Go services manage work lifetimes.

## The mechanics: what context is

`context.Context` is an interface with four methods:

```go
type Context interface {
    Done() <-chan struct{}          // closed when this context is cancelled/expired
    Err() error                     // why it's done: Canceled or DeadlineExceeded
    Deadline() (time.Time, bool)    // when it will auto-cancel, if ever
    Value(key any) any              // request-scoped value lookup (use sparingly)
}
```

You never implement it; you derive contexts from a root using the standard constructors, which build a **tree**:

```go
ctx := context.Background()                      // root, never cancelled
ctx, cancel := context.WithCancel(ctx)           // manual cancellation
ctx, cancel := context.WithTimeout(ctx, 2*time.Second) // auto-cancel after 2s
ctx, cancel := context.WithDeadline(ctx, t)      // auto-cancel at time t
ctx = context.WithValue(ctx, key, val)           // attach a request-scoped value
```

Each derivation creates a **child** context. The central property: **cancelling a parent cancels all its descendants**, recursively. This is the cancellation tree, and it is built on the `close(done)` broadcast primitive from the channels chapter.

```
context.Background()  (root)
        │
   WithTimeout(2s)  ──────────────► ctx for one HTTP request
        │
   ├── WithCancel  ──► DB query goroutine   ── cancel parent ──►  all Done() fire
   ├── WithCancel  ──► cache lookup goroutine        (close cascades down the tree)
   └── WithValue(traceID) ──► carries trace id downstream
```

When the request's 2-second timeout fires (or the client disconnects, or you call `cancel()`), every `Done()` channel in the subtree closes at once, and every goroutine selecting on `<-ctx.Done()` unblocks and returns. That cascade is the whole point.

## The rules that prevent leaks

Context prevents goroutine leaks *only if you follow its rules*. Violating them causes the very leaks it is meant to prevent:

**Rule 1: Always call `cancel`.** Every `WithCancel`/`WithTimeout`/`WithDeadline` returns a `cancel` function, and you **must** call it — normally via `defer cancel()` — even if the operation completed successfully. Not calling it leaks the context node (and its timer, for timeouts) until the parent is cancelled, which for a long-lived parent is forever. `go vet` warns about this; heed it.

```go
ctx, cancel := context.WithTimeout(parent, 2*time.Second)
defer cancel() // ALWAYS — even on the success path
result, err := doWork(ctx)
```

**Rule 2: Pass context as the first parameter, explicitly.** The convention is `func DoThing(ctx context.Context, args...)`. Do **not** store a context in a struct field — it hides the lifetime and outlives its scope. Thread it through call arguments so the cancellation relationship is visible.

**Rule 3: Select on `Done()` in every blocking operation of a long-lived goroutine.** A goroutine that does `<-someChan` without also selecting on `<-ctx.Done()` cannot be cancelled and will leak if the sender never comes. Respect cancellation at every blocking point:

```go
select {
case v := <-work:
    handle(v)
case <-ctx.Done():
    return ctx.Err() // cancelled or timed out — exit cleanly
}
```

**Rule 4: Propagate the context down, don't create fresh roots mid-chain.** Passing `context.Background()` to a downstream call *inside* a request path severs the cancellation tree — that call won't be cancelled when the request is. Always derive from the incoming `ctx`.

## Compiler & runtime view

There is little compiler magic here — context is ordinary library code — but two runtime facts matter. First, `WithTimeout`/`WithDeadline` arm a `time.Timer`; failing to `cancel()` leaves the timer live until it fires, which is a small but real leak in high-throughput code (thousands of orphaned timers). Second, cancellation propagation walks the child set under a mutex and `close()`s each done channel — O(number of descendants) — so pathologically deep or wide context trees have a (usually negligible) cancellation cost. The `context` source (shown in the channels chapter) is worth reading: it is a clean, production-grade example of the closed-channel broadcast and idempotent cancel.

## Production engineering: context through a request path

Here is how context threads through a real service, edge to database:

```go
func (s *Server) handleGetUser(w http.ResponseWriter, r *http.Request) {
    ctx := r.Context() // already carries client-disconnect cancellation

    // Impose a service-level deadline so a slow downstream can't hang the request.
    ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
    defer cancel()

    user, err := s.users.Get(ctx, userID(r)) // ctx flows into the DB layer
    if err != nil {
        if errors.Is(err, context.DeadlineExceeded) {
            http.Error(w, "timeout", http.StatusGatewayTimeout)
            return
        }
        http.Error(w, "error", http.StatusInternalServerError)
        return
    }
    writeJSON(w, user)
}

func (r *UserRepo) Get(ctx context.Context, id int64) (*User, error) {
    // pgx honors ctx: if the request is cancelled or times out, the query is
    // cancelled at the database (a CancelRequest is sent), freeing the connection.
    row := r.pool.QueryRow(ctx, "SELECT ... WHERE id=$1", id)
    // ...
}
```

Two things make this production-grade. First, `r.Context()` already fires when the client disconnects — so if a user closes their browser, the cancellation flows all the way to `pgx`, which cancels the in-flight query at PostgreSQL and returns the connection to the pool instead of wasting it. Second, the explicit `WithTimeout` bounds the work so one slow dependency can't pin a request (and its resources) indefinitely. This chain — client disconnect / deadline → context → DB query cancellation → freed connection — is the mechanism behind a service that sheds work gracefully under pressure instead of piling up stuck requests.

## context.Value — use sparingly

`WithValue` attaches request-scoped data (a trace ID, an authenticated user, a request-scoped logger). The strong guidance: use it **only** for values that cross API boundaries and are genuinely request-scoped, never for passing optional function parameters. Reasons:

- It is untyped (`any` keys and values), so it is not type-safe and costs a boxing allocation.
- It hides data flow — a function's real dependencies should be in its signature, not smuggled through context.
- Lookup is a linear walk up the parent chain — O(depth).

Idiomatic uses: request/trace IDs for correlation, auth principals, deadlines (implicitly). Non-idiomatic: passing a database handle, config, or a required argument through `Value`. Use typed keys (unexported key types) to avoid collisions:

```go
type ctxKey int
const traceIDKey ctxKey = 0
func WithTraceID(ctx context.Context, id string) context.Context {
    return context.WithValue(ctx, traceIDKey, id)
}
func TraceID(ctx context.Context) string {
    id, _ := ctx.Value(traceIDKey).(string)
    return id
}
```

## Common mistakes

- **Not calling `cancel()`** — leaks context nodes and timers. Always `defer cancel()`.
- **Storing context in a struct** instead of passing it as the first argument.
- **Passing `context.Background()` mid-request**, severing cancellation propagation.
- **Blocking without a `Done()` case** in long-lived goroutines → leaks.
- **Overusing `context.Value`** for parameters that belong in the function signature.
- **Ignoring `ctx.Err()`** — not distinguishing `Canceled` from `DeadlineExceeded` when the caller cares (e.g. mapping to 499 vs 504).

## Best practices

- `ctx` is the first parameter of every function that does I/O or can block; thread it everywhere.
- `defer cancel()` immediately after every `WithCancel`/`WithTimeout`/`WithDeadline`.
- Impose deadlines at the edge and let them propagate; bound every external call.
- Select on `<-ctx.Done()` in every blocking op of a long-lived goroutine.
- Use `context.Value` only for request-scoped, boundary-crossing values, with typed keys.
- Check `errors.Is(err, context.DeadlineExceeded)` / `context.Canceled` to react correctly.

## Performance analysis

Deriving a context is cheap (a small struct + possibly a timer); the cost that matters is the *timer* armed by `WithTimeout` — leaking those (Rule 1 violation) inflates the runtime timer heap and shows up under high QPS. `context.Value` lookups are O(chain depth) linear walks and box their values, so a deep chain queried in a hot loop is measurable; cache the looked-up value in a local rather than calling `ctx.Value` repeatedly. In practice context is not a hot-path bottleneck when used correctly; the performance story is really about *correctness* — cancelling promptly frees expensive resources (DB connections, upstream calls), which is worth far more than the microscopic cost of the context machinery itself.

## Production case study

A gateway service suffered connection-pool exhaustion under partial downstream outages: when a backend slowed to a crawl, requests piled up, each holding a database connection while waiting, until the pool was empty and *healthy* requests failed too. The root cause was that the internal call chain created a fresh `context.Background()` before calling the database, severing it from the request's context — so when clients gave up and disconnected, the now-orphaned queries kept running, holding connections. The fix was to thread `r.Context()` through unbroken and add a `WithTimeout` at the edge, so client disconnects and the deadline both propagated to `pgx`, which cancelled the queries at PostgreSQL and returned connections immediately. Pool exhaustion under downstream slowness disappeared. The lesson: **context is only as good as its weakest link — one `context.Background()` in the middle of a request path breaks the whole cancellation cascade.**

## Exercises

1. Build a three-level call chain that passes `ctx` down; cancel the top and confirm every level's `<-ctx.Done()` fires. Then break one level with `context.Background()` and show cancellation no longer reaches the bottom.
2. Write a `WithTimeout` call without `defer cancel()`; use `go vet` and a timer count (`runtime` metrics) to observe the leak. Fix it.
3. Implement a worker that selects on both a work channel and `<-ctx.Done()`; verify with `NumGoroutine()` that it exits on cancellation and does not leak.
4. Add a typed-key trace ID to context in an HTTP middleware and read it in a downstream function; confirm no key collisions and that it does not appear in the function signature.

## Summary

- `context.Context` carries **cancellation, deadlines, and request-scoped values** through a call tree. Derived contexts form a **tree**; cancelling a parent cancels all descendants via `close(done)` broadcast.
- The leak-prevention rules: **always `defer cancel()`**, **pass `ctx` as the first arg** (never store it), **derive from the incoming ctx** (never a fresh `Background()` mid-request), and **select on `Done()`** in every blocking op.
- Threading `ctx` from the HTTP edge to the database lets client disconnects and deadlines **cancel in-flight queries and free resources** — the mechanism for graceful behavior under pressure.
- Use `context.Value` only for request-scoped, boundary-crossing data with typed keys — not for ordinary parameters.

Next → [HTTP servers and middleware](/backend-guide/go/06-production-services/01-http-servers-and-middleware)
