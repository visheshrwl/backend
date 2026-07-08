---
title: Interface Representation — itab, eface, and Dynamic Dispatch
description: What an interface value actually is (two words), how method dispatch works, when interfaces allocate, the nil-interface trap, and how to place interfaces idiomatically.
tags: ["go", "interfaces", "dispatch", "itab", "performance"]
status: published
access: gated
publishedAt: 2026-07-08
---

# Interface Representation — itab, eface, and Dynamic Dispatch

## Learning objectives

You will know the exact two-word layout of an interface value, how method calls dispatch through the `itab`, when assigning to an interface allocates, why the "typed nil in an interface" trap bites everyone once, and where to define interfaces so your code stays idiomatic and fast.

## Why this matters

Interfaces are Go's only polymorphism mechanism, so understanding them is understanding how `io.Reader`, `error`, `http.Handler`, and every plugin boundary in every Go codebase work. Two practical payoffs: you will stop the accidental heap allocations that come from boxing values into interfaces on hot paths, and you will never again ship the `(*T)(nil)`-in-an-interface bug that makes `if err != nil` true when the error is "nil."

## The mechanics: an interface is two words

A non-empty interface value is a two-word struct, `iface`:

```go
// runtime representation (src/runtime/runtime2.go)
type iface struct {
    tab  *itab          // type + method table
    data unsafe.Pointer // pointer to the concrete value
}
```

The empty interface `any` (`interface{}`) is a slightly smaller two-word struct, `eface`, which needs no method table:

```go
type eface struct {
    _type *_type         // the concrete type descriptor
    data  unsafe.Pointer // pointer to the concrete value
}
```

The `itab` ("interface table") is the heart of dispatch. It is computed once per (interface type, concrete type) pair and cached:

```go
type itab struct {
    inter *interfacetype // the interface's method set
    _type *_type         // the concrete type
    hash  uint32
    fun   [1]uintptr     // variable-length: pointers to the concrete methods
}
```

```
var r io.Reader = myFile   // myFile is *os.File

 iface value (2 words)          itab (built once, cached)
 ┌────────┬────────┐            ┌──────────────────────────┐
 │ tab ───┼─ data ─┼──► *File   │ inter = io.Reader        │
 └───┼────┴────────┘            │ _type = *os.File         │
     └────────────────────────► │ fun[0] = (*File).Read ───┼──► method code
                                 └──────────────────────────┘
```

A method call `r.Read(p)` compiles to: load `r.tab.fun[0]`, call it with `r.data` as the receiver. That is **dynamic dispatch** — one pointer load plus an indirect call. It is cheap, but it is not free, and critically it is **not inlinable**: the compiler cannot see through an interface call to inline the method, which also blocks downstream optimizations.

## Compiler & runtime view: when interfaces allocate

Here is the cost that surprises people. The `data` word is a **pointer**. So when you assign a *value* (not already a pointer) to an interface, the value must live somewhere the pointer can point at — and if it does not already, it **escapes to the heap**:

```go
var x any
x = 42        // boxing: the int must be heap-allocated so `data` can point at it
              // (small-int optimization aside; large/arbitrary values allocate)
```

Assigning a value to an interface is called **boxing**, and it frequently allocates. Two important optimizations soften this:

- Assigning a value that is *already a pointer* (`x = myPtr`) does not allocate — `data` just holds the pointer.
- The runtime caches interface values for small integers and a few other cases, so `x = 0` may not allocate. Do not rely on the exact boundary; verify with `-gcflags='-m'`.

The production consequence: **`any`/`interface{}` on a hot path is an allocation red flag.** `fmt.Println(x)`, `[]any` slices, and reflection-based APIs box their arguments and allocate. This is a big reason `fmt.Sprintf` is slow and why logging libraries like `zerolog` and `zap` go to great lengths to avoid `interface{}` in their hot paths (typed methods like `.Str()`, `.Int()` instead of `.Interface()`).

```go
// Allocates: each arg is boxed into interface{} for the variadic ...any
log.Printf("user %d did %s", id, action)

// zerolog avoids boxing with typed field methods:
logger.Info().Int("user", id).Str("action", action).Msg("did")
```

## The nil-interface trap

This one catches every Go engineer exactly once, and it causes real production incidents. An interface is nil **only if both words are nil** — both the type and the data. A `nil` *pointer* stored in an interface is a **non-nil interface** (it has a type):

```go
func doThing() error {
    var e *MyError = nil // typed nil
    // ... e stays nil ...
    return e             // returns an interface with tab=*MyError, data=nil
}

if err := doThing(); err != nil {
    // TRUE! err is non-nil: it carries the type *MyError even though data is nil.
    // Calling a method may then nil-panic. Classic incident.
}
```

The interface `err` is `{tab: *MyError, data: nil}` — the `tab` is set, so `err != nil` is true, even though the underlying pointer is nil. The fix is to return a literal `nil`, never a typed nil pointer, from functions returning `error`:

```go
func doThing() error {
    if failed {
        return &MyError{...}
    }
    return nil // untyped nil — the whole interface is nil
}
```

`go vet` catches some cases; `staticcheck` catches more. But the durable defense is understanding *why*: an interface remembers the type even when the value is nil.

## Production engineering: where to put interfaces

The single most important idiom: **"accept interfaces, return structs," and define the interface at the consumer.** In Go, a type satisfies an interface *implicitly* — no `implements` keyword — which means the interface belongs to whoever *uses* it, not whoever *provides* it. Consequences:

- Define small interfaces next to the function that consumes them, listing only the methods that function actually calls. `io.Reader` is one method because that is all `io.Copy` needs.
- Do **not** pre-emptively define a big interface mirroring every method of a concrete type "for testability." That is a Java reflex. In Go you add the one-method interface at the seam where you actually need to substitute a fake.
- Returning concrete structs (not interfaces) from constructors lets callers see the full API and lets the compiler inline; it is the caller's job to narrow to an interface if they want polymorphism.

Over-interfacing is a real cost, not just a style preference: every interface boundary is a boxed value (possible allocation) and a non-inlinable dynamic call. A codebase that wraps every concrete type in an interface pays for polymorphism it never uses.

## Real open-source example

`io.Reader` and `io.Copy` from the standard library are the canonical demonstration:

```go
type Reader interface {
    Read(p []byte) (n int, err error)
}

func Copy(dst Writer, src Reader) (written int64, err error) {
    // If src also implements WriterTo, use it (avoids a buffer):
    if wt, ok := src.(WriterTo); ok {
        return wt.WriteTo(dst)
    }
    // If dst implements ReaderFrom, use that:
    if rt, ok := dst.(ReaderFrom); ok {
        return rt.ReadFrom(src)
    }
    // Fallback: copy through a buffer using the one-method interfaces.
    buf := make([]byte, 32*1024)
    for {
        nr, er := src.Read(buf)
        // ...
    }
}
```

Everything worth knowing about interfaces is here. `Copy` accepts the *smallest* interfaces that do the job (`Reader`, `Writer`), so any file, socket, buffer, or HTTP body works with it. The **type assertions** `src.(WriterTo)` and `dst.(ReaderFrom)` are a runtime feature-detection: at the cost of an `itab` lookup, `Copy` discovers richer capabilities and takes a faster path (e.g. `sendfile` under the hood for a file→socket copy), falling back to the generic buffered loop otherwise. This "small interface plus optional-capability assertions" pattern is *the* idiomatic Go extension mechanism, and you will see it throughout the standard library and Kubernetes.

## Common mistakes

- **Boxing on hot paths:** `interface{}`/`any` parameters, `[]any`, `fmt.Sprintf` in tight loops — each boxes and often allocates.
- **The typed-nil-in-interface trap:** returning a `(*T)(nil)` as an `error`, making `err != nil` unexpectedly true.
- **Over-interfacing:** defining large interfaces mirroring a single concrete type; defining interfaces at the producer instead of the consumer.
- **Assuming interface calls inline:** they do not; a hot dispatch through an interface blocks inlining and can be a measurable cost.
- **Storing large values in interfaces** expecting no copy — the value is boxed (copied to the heap).

## Best practices

- Accept interfaces, return concrete structs. Keep interfaces small and consumer-defined.
- Keep `any`/reflection off hot paths; prefer typed APIs (see structured logging).
- Return untyped `nil` from `error`-returning functions; never a typed nil pointer.
- Use type assertions / type switches for optional-capability detection, the idiomatic extension pattern.
- Run `staticcheck` — it flags the typed-nil trap and needless interface boxing.

## Performance analysis

```
$ go test -bench=Dispatch -benchmem
BenchmarkDirectCall-8      1000000000   0.30 ns/op   0 B/op   0 allocs/op  # inlined
BenchmarkIfaceCall-8        500000000   2.10 ns/op   0 B/op   0 allocs/op  # dynamic dispatch
BenchmarkIfaceBoxValue-8    50000000    22.0 ns/op   8 B/op   1 allocs/op  # boxing an int
```

A direct (inlinable) call is ~0.3 ns; the same call through an interface is ~2 ns — the dispatch itself is cheap, but it lost inlining. The killer is the third line: boxing a value into an interface allocates (8 B, 1 alloc). In a loop over millions of items, that allocation is what shows up as GC pressure in your profile. The rule: dynamic dispatch is affordable; *boxing values* is what you watch for.

## Production case study

A high-throughput logging path in a service used a generic `log(fields map[string]any)` signature. Under load, `pprof` showed a large fraction of allocations coming from boxing every field value into `any` and building the map per log line. Migrating to a typed builder API (`.Str()`, `.Int()`, `.Dur()` writing directly into a byte buffer, the `zerolog` model) removed the `any` boxing and the per-line map allocation entirely, cutting logging allocations to near zero and shaving measurable p99 latency off request handlers under high log volume. The lesson generalizes: **`interface{}` is the convenient, allocating choice; typed APIs are the fast one**, and on hot paths the typed API is worth the extra surface.

## Exercises

1. Assign an `int`, a `string`, a `*struct`, and a large value-struct to `any`; run `-gcflags='-m'` and record which ones escape/allocate. Explain each.
2. Reproduce the typed-nil trap: a function returning `error` via a `*MyError` variable that stays nil. Show `err != nil` is true, then fix it and confirm `go vet`/`staticcheck` opinions.
3. Benchmark a direct method call, the same call through a one-method interface, and the call after storing the receiver in `any`. Explain the three timings.
4. Read `io.Copy`'s source and enumerate every optional-capability assertion it makes and the fast path each unlocks.

## Summary

- An interface value is **two words**: `iface{tab, data}` (or `eface{_type, data}` for `any`). `tab.fun[...]` holds method pointers; a call is a pointer load plus an indirect (non-inlinable) call.
- Assigning a **value** to an interface **boxes** it and often **allocates** (the `data` word needs a pointer). `any` and reflection on hot paths are allocation red flags.
- An interface is nil only if **both** words are nil. A typed nil pointer in an interface is **non-nil** — the classic `error` trap. Return untyped `nil`.
- Idiom: **accept small, consumer-defined interfaces; return concrete structs.** Use type assertions for optional-capability detection. Over-interfacing costs allocations and inlining.

Next → [Goroutines and the GMP scheduler](/backend-guide/go/03-runtime/01-goroutines-and-the-scheduler)
