---
title: Channels — The Runtime Structure and Correct Usage
description: How hchan works, the difference buffering makes, send/receive/close semantics, select, the patterns that are idiomatic vs the ones that deadlock or leak, and when a mutex is the better tool.
tags: ["go", "channels", "concurrency", "select", "hchan"]
status: published
access: gated
publishedAt: 2026-07-08
---

# Channels — The Runtime Structure and Correct Usage

## Learning objectives

You will understand the `hchan` structure behind a channel, exactly what send/receive/close do (including the nil and closed cases), how `select` chooses, the idiomatic ownership rules that prevent panics and leaks, and when channels are the wrong tool and a mutex is right.

## Why this matters

Channels are Go's headline concurrency feature — "don't communicate by sharing memory; share memory by communicating." But used carelessly they cause the two worst Go concurrency bugs: **deadlocks** (goroutines blocked forever) and **panics** from sending on or closing a closed channel. Knowing the exact semantics — and knowing when a plain mutex is simpler and faster — is what separates robust concurrent Go from the kind that wedges in production.

## The mechanics: what a channel is

A channel is a pointer to a runtime `hchan` struct — a lock-protected queue with two wait lists:

```go
// simplified from src/runtime/chan.go
type hchan struct {
    qcount   uint           // items currently in the buffer
    dataqsiz uint           // buffer capacity (0 for unbuffered)
    buf      unsafe.Pointer // ring buffer (only if buffered)
    elemsize uint16
    closed   uint32
    sendx    uint           // ring buffer send index
    recvx    uint           // ring buffer receive index
    recvq    waitq          // goroutines blocked in receive
    sendq    waitq          // goroutines blocked in send
    lock     mutex          // protects everything above
}
```

The key insight: a channel is **a mutex-guarded ring buffer plus two queues of parked goroutines** (`sudog`s). It is not lock-free magic; every operation takes `hchan.lock`. What makes it powerful is that it integrates with the scheduler: a blocked send/receive **parks** the goroutine (freeing its M to do other work) and a matching operation **unparks** it.

```
 buffered channel, cap 4, holds 2 items:

   buf (ring):  [ _ ][ x ][ y ][ _ ]
                       ▲recvx        ▲sendx
   sendq: (empty)   recvq: (empty)

 unbuffered channel: no buf; a send must rendezvous directly with a receive.
```

## Send, receive, and close semantics

Memorize this table — it defines every channel behavior, including the panics:

```
Operation        nil channel      open channel                closed channel
─────────────────────────────────────────────────────────────────────────────
send  ch<-v      blocks forever   buffered: enqueue or block  PANIC
                                  unbuffered: rendezvous
recv  <-ch       blocks forever   dequeue or block            returns zero, ok=false
close(ch)        PANIC            marks closed, wakes waiters  PANIC (double close)
```

Three of these cause real incidents:

- **Send on a closed channel panics.** This is why the rule is *only the sender closes, and only when it is the sole sender.* If multiple goroutines send, none of them may close (they can't know the others are done); coordinate closure separately.
- **Close of a nil or already-closed channel panics.** Double-close is a common bug in cleanup code; guard with `sync.Once` if closure can race.
- **Receive on a closed channel returns immediately** with the zero value and `ok == false`. This is the *feature* that makes `close` a broadcast: every receiver, now and future, is unblocked. That is why closing a `done` channel is the idiomatic cancellation broadcast.

```go
v, ok := <-ch   // ok == false means the channel is closed AND drained
for v := range ch { ... } // ranges until the channel is closed and drained
```

## Buffered vs unbuffered — the semantic difference

This is not just "size." It changes the *synchronization guarantee*:

- **Unbuffered** (`make(chan T)`): a send **blocks until a receiver takes the value** — a rendezvous. The send happening-before the receive means it is also a synchronization point: you know the receiver has the value when the send returns. Use unbuffered channels for *handoff with acknowledgement* and for signaling.
- **Buffered** (`make(chan T, n)`): a send blocks only when the buffer is full; it decouples sender and receiver by up to `n` items. Use buffering to absorb bursts or to allow a known number of sends to proceed without a receiver ready — but a buffer does **not** make concurrency bugs go away; it just changes when they surface.

A common anti-pattern is picking a buffer size to "fix" a deadlock. If code deadlocks with an unbuffered channel, a buffer usually just delays the deadlock until the buffer fills. Fix the ownership, not the buffer.

## select

`select` waits on multiple channel operations and proceeds with one that is ready (choosing **randomly** among several ready cases, to avoid starvation):

```go
select {
case v := <-in:      // receive ready
    handle(v)
case out <- x:       // send ready
case <-ctx.Done():   // cancellation
    return ctx.Err()
case <-time.After(d):// timeout (note: leaks a timer until it fires — see below)
default:             // if present, makes the select non-blocking
}
```

Two production-critical idioms live in `select`:

- **Cancellation/timeout:** a `<-ctx.Done()` case lets any blocking channel operation be interrupted. Every channel operation in a long-lived goroutine should be in a `select` with a done/ctx case, or it can block forever (a leak).
- **Non-blocking try:** a `default` case turns a potentially-blocking send/receive into a try-once. Useful for "drop if full" (shedding load) or "poll without waiting."

Watch out: `time.After(d)` in a `select` inside a loop allocates a timer that lives until `d` elapses even if another case wins — a slow leak in hot loops. Use a reused `time.Timer` (with `Stop`/`Reset`) or `context.WithTimeout` instead.

## Production engineering: ownership rules

Channels are safe when ownership is clear. The rules that keep them safe:

1. **The sender closes, never the receiver.** Receivers cannot know if more sends are coming; closing from the receive side risks a send-on-closed panic.
2. **With multiple senders, no one closes on the send path.** Use a separate coordination signal (a `context`, a `sync.WaitGroup` that a *single* owner waits on before closing, or a dedicated done channel) so exactly one goroutine performs the close after all senders finish.
3. **Every long-lived goroutine's channel ops sit in a `select` with a cancellation case.** This is how you guarantee a goroutine can always exit — the antidote to leaks.
4. **Prefer directional channel types in signatures** (`chan<- T` send-only, `<-chan T` receive-only). They document ownership and let the compiler enforce it.

## When NOT to use a channel

Channels are for **transferring ownership, distributing work, and signaling events**. They are the *wrong* tool for protecting simple shared state, and reaching for them there produces slower, more complex code than a mutex. The Go team's own guidance: **use a mutex for guarding shared state; use channels for orchestrating goroutines.**

```go
// Overwrought: a channel to guard a counter
// (a goroutine + channel per increment). Slow and complex.

// Right: a mutex (or atomic) for a counter.
type Counter struct {
    mu sync.Mutex
    n  int
}
func (c *Counter) Inc() { c.mu.Lock(); c.n++; c.mu.Unlock() }
// or simply: atomic.Int64
```

A channel operation that parks/unparks a goroutine is ~100 ns and involves the scheduler; an uncontended mutex `Lock/Unlock` is a couple of atomic operations, far cheaper. If the job is "let one goroutine touch this field at a time," a mutex wins on both speed and clarity.

## Real open-source example

Go's `context` package uses a closed channel as a broadcast cancellation signal — the canonical channel idiom (`src/context/context.go`, simplified):

```go
type cancelCtx struct {
    mu   sync.Mutex
    done atomic.Value // of chan struct{}, lazily created
    // ...
}

func (c *cancelCtx) Done() <-chan struct{} {
    d := c.done.Load()
    if d != nil { return d.(chan struct{}) }
    // ... lazily create the done channel ...
}

func (c *cancelCtx) cancel(removeFromParent bool, err error) {
    // ...
    c.mu.Lock()
    if c.err != nil { c.mu.Unlock(); return } // already cancelled — idempotent
    c.err = err
    close(c.done)  // <-- broadcast: unblocks EVERY <-ctx.Done() everywhere
    for child := range c.children { child.cancel(false, err) } // propagate
    c.mu.Unlock()
}
```

Every lesson is here: `Done()` returns a **receive-only** channel (`<-chan struct{}`) — receivers can't close it. Cancellation is a single `close(c.done)`, which unblocks *all* current and future receivers at once — that's why `close` is the broadcast primitive. The `if c.err != nil` guard plus the mutex make cancel **idempotent**, avoiding the double-close panic even when parent and child cancel race. And it uses `chan struct{}` (zero-size element) because the *signal* matters, not any value. This is production channel code done exactly right.

## Common mistakes

- **Send on / close of a closed channel** → panic. Usually from unclear closer ownership or double-close in cleanup.
- **Closing from the receiver** or from one of many senders.
- **Goroutine blocked on a channel with no cancellation case** → leak.
- **`time.After` in a hot `select` loop** → timer leak. Reuse a `Timer` or use `context`.
- **Using channels to guard shared state** where a mutex is simpler and ~10× faster.
- **Buffering to paper over a deadlock** instead of fixing ownership.

## Best practices

- Sender closes; with multiple senders, a single coordinator closes after all finish (WaitGroup/context).
- Put every long-lived channel op in a `select` with `<-ctx.Done()`.
- Use directional channel types in signatures to encode and enforce ownership.
- Use `chan struct{}` for pure signals; `close` for broadcast.
- Choose mutex vs channel by intent: guard state → mutex/atomic; orchestrate goroutines → channel.

## Performance analysis

```
$ go test -bench=. -benchmem
BenchmarkMutexInc-8        90000000    13 ns/op   0 allocs/op
BenchmarkAtomicInc-8      250000000     4 ns/op   0 allocs/op
BenchmarkChanInc-8         10000000   112 ns/op   0 allocs/op   # send+recv handoff
```

Guarding a counter with a channel is ~8× slower than a mutex and ~28× slower than an atomic, because each increment parks/unparks through the scheduler. This quantifies the "channels for orchestration, mutex for state" rule: when the work per operation is tiny, the channel's scheduler involvement dominates. Channels earn their cost when they are *moving work between goroutines*, not guarding a field.

## Production case study

A pipeline fanned work out to N worker goroutines over a shared channel and closed that channel from a worker when it saw the last item — occasionally panicking with `send on closed channel` when another worker was mid-send. The root cause was rule #2: multiple senders, and one of them closed. The fix separated concerns: workers only send; a single coordinator `WaitGroup.Wait()`s for all workers, then closes the results channel exactly once. Separately, a long-lived consumer goroutine had a bare `for v := range results` with no cancellation path, so on shutdown it blocked forever waiting on a channel that never closed — a leak caught by the climbing `go_goroutines` metric. Adding a `select` with `<-ctx.Done()` let it exit cleanly. Both bugs are direct violations of the ownership rules; both are invisible until concurrency and shutdown timing expose them.

## Exercises

1. Build the send/receive/close truth table empirically: write tiny programs that trigger each panic and each blocking case, including nil-channel blocks.
2. Implement a fan-out/fan-in pipeline with N workers, correct single-closer coordination, and full context cancellation. Verify with `-race` and by checking `NumGoroutine()` returns to baseline after shutdown.
3. Reproduce the `time.After` timer leak in a tight `select` loop; observe it in a heap profile; fix it with a reused `Timer`.
4. Benchmark a bounded semaphore implemented as a buffered channel vs `x/sync/semaphore`. Compare throughput and allocations.

## Summary

- A channel is an `hchan`: a **mutex-guarded ring buffer plus two queues of parked goroutines**, integrated with the scheduler (blocked ops park; matching ops unpark).
- Semantics that cause incidents: **send/close on a closed channel panics**; **receive on closed returns zero, ok=false** (the basis of `close`-as-broadcast); **nil channel blocks forever**.
- **Unbuffered = rendezvous** (synchronizing handoff); **buffered = decoupling up to n**. Don't buffer to hide deadlocks.
- Ownership rules: **sender closes**, single coordinator closes with multiple senders, every long-lived op has a **cancellation case**, use **directional types**.
- Channels orchestrate goroutines; **use a mutex/atomic to guard shared state** — it is simpler and much faster.

Next → [Garbage collection: the concurrent tri-color collector](/backend-guide/go/03-runtime/03-garbage-collection)
