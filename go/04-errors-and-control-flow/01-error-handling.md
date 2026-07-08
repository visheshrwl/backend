---
title: Error Handling — Values, Wrapping, and Failure Semantics
description: Why Go errors are values, sentinel vs typed errors, wrapping with %w and errors.Is/As, when to panic, and how to design error flow in production services.
tags: ["go", "errors", "error-handling", "panic", "production"]
status: published
access: gated
publishedAt: 2026-07-08
---

# Error Handling — Values, Wrapping, and Failure Semantics

## Learning objectives

You will understand why Go models errors as ordinary values, how to choose between sentinel errors, typed errors, and opaque errors, how wrapping with `%w` plus `errors.Is`/`errors.As` works, when `panic` is legitimate, and how to design error flow that produces diagnosable incidents.

## Why this matters

Error handling is the code you write most in Go — the `if err != nil` on nearly every call — and it is where production diagnosability is won or lost. A service whose errors are wrapped with context produces log lines that pinpoint the failure; one that swallows or stringifies errors produces 3 AM guesswork. The design choices here (sentinel vs typed, when to wrap, when to panic) directly determine how debuggable your service is under fire.

## The mechanics: errors are values

Go has no exceptions. An error is just a value that satisfies a one-method interface:

```go
type error interface {
    Error() string
}
```

Functions that can fail return an `error` as their last result, and the caller checks it:

```go
f, err := os.Open(path)
if err != nil {
    return fmt.Errorf("open config %q: %w", path, err)
}
```

This is a deliberate rejection of exceptions. The trade: verbosity (the ubiquitous `if err != nil`) in exchange for **explicit, visible control flow** — every failure path is right there in the code, there is no invisible non-local jump, and you cannot accidentally ignore an error without it being visible (`_ = f()`). Coming from C++/Java/Python exceptions, the adjustment is real, but the payoff is that reading Go code you can *see* exactly where and how each operation can fail.

Because errors are values, you can compare them, store them, wrap them, and inspect them with ordinary code — which is the whole basis of the patterns below.

## Sentinel, typed, and opaque errors

Three ways to model an error, each with a use:

**Sentinel errors** — a predefined error value you compare against:
```go
var ErrNotFound = errors.New("not found")
// caller:
if errors.Is(err, ErrNotFound) { ... }
```
Use for a small set of well-known conditions that callers branch on (`io.EOF`, `sql.ErrNoRows`). Downside: they couple caller and callee to a shared symbol, so use sparingly for a stable, public vocabulary of conditions.

**Typed errors** — a struct implementing `error`, carrying data:
```go
type ValidationError struct {
    Field string
    Msg   string
}
func (e *ValidationError) Error() string { return e.Field + ": " + e.Msg }
// caller:
var ve *ValidationError
if errors.As(err, &ve) { log.Println(ve.Field) }
```
Use when the caller needs *details* about the failure (which field, which status code). Richer than sentinels; the caller extracts structured data with `errors.As`.

**Opaque errors** — the caller only knows "it failed," not what specifically:
```go
if err != nil { return err } // just propagate; caller doesn't branch on the kind
```
The default and most common case. Don't invent sentinels or types unless a caller actually needs to *distinguish* the failure. Over-modeling errors is as much a smell as under-modeling them.

## Wrapping: %w, errors.Is, errors.As

Since **Go 1.13**, errors form a **chain** via wrapping. `fmt.Errorf` with the `%w` verb wraps an error, preserving the original while adding context:

```go
return fmt.Errorf("fetch user %d: %w", id, err) // wraps err, adds context
```

This builds a chain: `"fetch user 42: query failed: connection refused"`, where each layer added context on the way up. You then inspect the chain, never by string matching, but with two functions:

- **`errors.Is(err, target)`** — walks the chain looking for a value *equal to* `target`. For sentinels: `errors.Is(err, sql.ErrNoRows)` is true even if `err` wrapped `ErrNoRows` several layers down.
- **`errors.As(err, &target)`** — walks the chain looking for an error *assignable to* `target`'s type, and if found, sets it. For typed errors: `errors.As(err, &ve)` extracts the `*ValidationError` from anywhere in the chain.

```
err chain:  [handler: "save order"] ─%w─► [db: "insert"] ─%w─► [driver: ErrConnRefused]
                                                                        ▲
errors.Is(err, ErrConnRefused) walks ────────────────────────────────► finds it → true
```

The rule that prevents the most bugs: **never compare errors by string** (`strings.Contains(err.Error(), "not found")`). It is fragile (messages change), it defeats wrapping, and it breaks the moment someone rephrases a message. Use `errors.Is`/`errors.As` — they are the supported, wrapping-aware way to ask "what kind of error is this."

**When to wrap vs not:** wrap when you are adding context that helps diagnosis (`"fetch user %d: %w"`). Do *not* wrap if you would leak an internal error to an external boundary where callers shouldn't depend on it — there, translate to a boundary-appropriate error. And use `%w` (wrap, inspectable) vs `%v` (flatten to string, breaks the chain) deliberately: `%w` when callers may need to inspect the cause, `%v` when you want to *seal* the cause and expose only a message.

## When to panic

`panic` is Go's mechanism for **unrecoverable** situations — programmer bugs and truly impossible states — not for ordinary failure. The rule:

- **Return an error** for anything that can happen in normal operation: bad input, missing file, network failure, a full queue. These are *expected* and callers should handle them.
- **Panic** only for *programmer errors* and invariant violations: an impossible switch case, a nil that the code's contract guarantees is non-nil, a corrupt internal state where continuing is meaningless. `panic` here surfaces a bug loudly during development.

`recover` (in a deferred function) stops a panic from crashing the program. Its one legitimate production use is at a **boundary** where you must not let one unit take down the whole process — e.g. an HTTP server recovers per-request so one handler's panic returns a 500 instead of killing the server:

```go
func recoverMiddleware(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        defer func() {
            if rec := recover(); rec != nil {
                log.Printf("panic: %v\n%s", rec, debug.Stack())
                http.Error(w, "internal error", http.StatusInternalServerError)
            }
        }()
        next.ServeHTTP(w, r)
    })
}
```

Using `panic`/`recover` as general control flow (like exceptions) is non-idiomatic and fights the language. Remember: a concurrent map write and a few other runtime conditions are `fatal error`s that `recover` **cannot** catch — recover is not a safety net for everything.

## Production engineering: designing error flow

- **Add context on the way up, once per layer.** Each layer wraps with what *it* was doing (`"charge card: %w"`), so the final message reads as a trace. Don't wrap redundantly at every call — wrap at meaningful boundaries.
- **Decide the caller's needs before choosing sentinel/typed/opaque.** Most errors are opaque (just propagate). Add a sentinel or type only where a caller genuinely branches.
- **Translate at boundaries.** Internal errors (a DB constraint violation) should become boundary-appropriate errors at the API edge (a 409 Conflict), not leak internal details to clients or logs meant for users.
- **Never swallow silently.** Every `err` is handled, returned, or explicitly `_ =`'d with a reason. A dropped error is a latent incident.
- **Log at the top, not at every layer.** Wrapping carries context up; log once where the error is finally handled, or you get N duplicate log lines for one failure.

## Common mistakes

- **String-matching errors** instead of `errors.Is`/`errors.As`.
- **Flattening with `%v` when you meant `%w`**, breaking the inspectable chain.
- **Panicking for ordinary failures** (bad input, network errors) — return errors instead.
- **Logging and returning** the same error at every layer → duplicate, confusing logs.
- **Swallowing errors** (`f()` with the error dropped) — silent failure.
- **The typed-nil trap** (from the interfaces chapter): returning a typed nil pointer as `error`, making `err != nil` unexpectedly true.

## Best practices

- Return errors for expected failures; reserve `panic` for bugs/invariants; `recover` only at process boundaries.
- Wrap with `%w` and meaningful context at layer boundaries; inspect with `errors.Is`/`errors.As`.
- Model errors as opaque by default; add sentinels/types only where callers branch.
- Translate internal errors to appropriate errors at external boundaries.
- Handle every error explicitly; log once, at the top.

## Real open-source example

The standard library's `io.EOF` and `os` errors show the sentinel + typed pattern working together. `io.EOF` is a sentinel (`errors.Is(err, io.EOF)`), while `os.PathError` is a typed error carrying the operation and path (`errors.As(err, &pathErr)` yields `pathErr.Op`, `pathErr.Path`, `pathErr.Err`). A single `os.Open` failure can be inspected both ways: `errors.Is(err, os.ErrNotExist)` to branch on the *condition*, and `errors.As(err, &pe)` to extract the *details*. This dual design — a stable vocabulary of sentinel conditions plus typed errors carrying context, all wrapping-aware — is the model to copy in your own packages: expose the conditions callers branch on as sentinels, carry the details as typed errors, and wrap with `%w` so both survive propagation.

## Performance analysis

Creating an error with `errors.New` on the failure path is cheap and off the hot path by definition. The costs to watch: `fmt.Errorf` allocates (formatting + wrapping), so avoid it in tight *success* paths that call it speculatively; and building a stack trace (some third-party error libraries capture stacks on every error) is expensive — fine at true error boundaries, wasteful if done per call in hot code. The `if err != nil` check itself is a branch the CPU predicts near-perfectly (errors are rare), so the *checking* is effectively free; it is *constructing rich errors on hot paths that would never fail* that costs. Keep error construction on the actual failure path.

## Production case study

A payments service diagnosed incidents slowly because its errors were stringified early: the DB layer returned `errors.New("query failed")`, the service layer did `fmt.Errorf("charge failed: %v", err)` (note `%v`, not `%w`), and the handler logged the flattened string. When a specific failure — a unique-constraint violation that should map to "already charged" (409) — needed to be distinguished, there was no way to, because the typed driver error had been flattened three layers down. The refactor threaded `%w` through every layer, kept the driver's typed error inspectable, and added `errors.As` at the handler to map the constraint violation to a 409 and everything else to a 500. Incident diagnosis time dropped because the wrapped chain now read as a trace, and the idempotency bug was fixable because the specific error was finally reachable. The lesson: **`%w` and `errors.Is/As` are not style — they are what make production errors actionable.**

## Exercises

1. Build a three-layer call chain that wraps with `%w` at each layer; at the top, use `errors.Is` to detect a sentinel defined at the bottom and `errors.As` to extract a typed error from the middle.
2. Change one layer from `%w` to `%v` and show that `errors.Is`/`errors.As` can no longer find the cause. Explain why.
3. Write a `recover` middleware for an HTTP handler; trigger a panic in a handler and confirm the server returns 500 and stays up. Then trigger a concurrent map write and confirm `recover` does *not* save you.
4. Reproduce the typed-nil `error` trap and fix it; confirm `staticcheck` flags it.

## Summary

- Go errors are **values** implementing `error`; there are no exceptions. The trade is verbosity for **explicit, visible failure paths**.
- Model errors as **opaque** by default; use **sentinels** for conditions callers branch on and **typed** errors for failures carrying details.
- **Wrap with `%w`** to build an inspectable chain; inspect with **`errors.Is`** (value match) and **`errors.As`** (type match). **Never string-match** errors.
- **Panic only for bugs/invariants**; return errors for expected failures; **`recover` only at boundaries** (and it can't catch `fatal error`s).
- Design flow: add context per layer, translate at boundaries, handle every error, log once at the top. This is what makes incidents diagnosable.

Next → [Context propagation: cancellation, deadlines, and request scope](/backend-guide/go/05-concurrency-patterns/01-context-propagation)
