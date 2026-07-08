---
title: Slice Internals — The Header, Growth, and Aliasing
description: The three-word slice header, how append grows capacity, the aliasing traps that cause real production bugs, and how to write zero-allocation slice code.
tags: ["go", "slices", "memory", "append", "performance"]
status: published
access: gated
publishedAt: 2026-07-08
---

# Slice Internals — The Header, Growth, and Aliasing

## Learning objectives

You will know exactly what a slice is at the machine level, predict when `append` allocates and copies, recognize the aliasing bugs that leak or corrupt data in production, and write slice code that does zero allocations on the hot path.

## Why this matters

Slices are the single most-used data structure in Go and the single most-misunderstood. Nearly every "why is this allocating" and a large fraction of "why is this data mysteriously changing" bugs trace back to not knowing that a slice is a *view* with three words of header over a shared backing array. This is table-stakes for reading `pgx`, the standard library, or any buffer-heavy code.

## The mechanics: a slice is a three-word header

A slice is **not** an array. It is a small struct — the *slice header* — that describes a window into a backing array:

```go
// runtime representation (src/runtime/slice.go)
type slice struct {
    array unsafe.Pointer // pointer to the first element of the window
    len   int            // number of elements in the window
    cap   int            // elements from `array` to the end of the backing array
}
```

On a 64-bit machine that is **24 bytes**: pointer + len + cap. When you pass a slice to a function, you copy those 24 bytes — the header — and both copies point at the *same backing array*. That is why a function can mutate a slice's elements and the caller sees it, but *appending* inside the function may or may not be visible (more below).

```
s := make([]int, 3, 6)

  slice header (24 bytes, on stack)        backing array (heap, cap=6)
  ┌──────────┬─────┬─────┐                 ┌───┬───┬───┬───┬───┬───┐
  │ array ───┼ len=3│cap=6│ ──────────────►│ 0 │ 0 │ 0 │ . │ . │ . │
  └──────────┴─────┴─────┘                 └───┴───┴───┴───┴───┴───┘
                                             └── len ──┘└─ spare ─┘
```

`len` is what indexing and `range` see; `cap` is how far the window *could* grow before needing a new backing array. `len(s)` and `cap(s)` read these fields directly — O(1), no scan.

## Compiler & runtime view: how append grows

`append` is where all the interesting behavior lives. The logic:

```
append(s, x):
  if len(s) < cap(s):
      # room in the backing array — write in place, no allocation
      s.array[len] = x; s.len++
      return s   # same backing array
  else:
      # full — allocate a NEW, larger backing array, COPY, then append
      newcap = growth(cap(s))
      new = allocate(newcap)
      copy(new, s)
      new[len] = x
      return slice{new, len+1, newcap}  # DIFFERENT backing array
```

The growth function (`runtime.growslice`) is the detail engineers get wrong. The current policy (Go 1.18+):

- If the required capacity is more than double the current, use the required capacity directly.
- Else if `cap < 256`, **double** it.
- Else grow by roughly **1.25×** each time (specifically `newcap += (newcap + 3*256) / 4`), a smooth transition from 2× toward 1.25× for large slices.

(Pre-1.18 it was a hard "2× under 1024, else 1.25×." The threshold moved to 256 with a smoother curve. The *engineering* consequence is unchanged: growth is geometric, so `append` is amortized O(1), but each growth **reallocates and copies the whole slice**.)

The one number to remember: **appending N elements to a nil slice does O(log N) allocations and copies a total of O(N) elements** — cheap asymptotically, but every reallocation is a `memmove` and a GC-tracked allocation you can eliminate by preallocating.

```go
// Allocates ~log2(n) times, copying repeatedly:
var s []int
for i := 0; i < n; i++ { s = append(s, i) }

// Allocates exactly once, zero copies:
s := make([]int, 0, n)
for i := 0; i < n; i++ { s = append(s, i) }
```

`make([]T, 0, n)` when you know the size is the single highest-frequency optimization in Go code, and its absence in a hot loop is a standard review comment.

## Production engineering: the aliasing traps

Because slices share backing arrays, two bugs recur in production. Both are worth being able to spot on sight.

### Trap 1: append that mutates a slice you thought you owned

```go
func addOne(base []int) []int {
    return append(base, 1)
}

s := make([]int, 3, 6) // len 3, cap 6 — spare capacity!
a := addOne(s)         // writes into s's spare cap, no realloc
b := addOne(s)         // writes into the SAME spare slot, clobbering a's
// a and b both see the last write — silent corruption
```

Because `s` had spare capacity, both `append`s wrote to the *same* backing slot without reallocating. This is the classic "append surprise." The defense when you need an independent copy is to force one, or to use the **full-slice expression** `base[low:high:max]` which caps the capacity so any append must reallocate:

```go
return append(base[:len(base):len(base)], 1) // cap == len forces a new array
```

The standard library uses this pattern deliberately anywhere it hands a slice to callers who might append.

### Trap 2: sub-slicing keeps the whole backing array alive (a memory leak)

```go
func firstLine(data []byte) []byte {
    i := bytes.IndexByte(data, '\n')
    return data[:i] // shares the backing array of the ENTIRE file
}
```

If `data` is a 100 MB file and you keep the returned 20-byte slice, the GC **cannot** free the 100 MB — the small slice's header still points into it. This is a real, common leak. The fix is to copy out what you need so the large backing array becomes unreachable:

```go
line := append([]byte(nil), data[:i]...) // independent 20-byte allocation
```

### Trap 3: `range` copies the element

```go
for _, v := range items { v.Field = x } // mutates a COPY; items unchanged
for i := range items    { items[i].Field = x } // mutates in place
```

`range` binds `v` to a **copy** of each element (value semantics again). To mutate the backing array, index it. For large element structs this copy is also a per-iteration cost worth knowing about.

## Real open-source example

From the standard library's `strings.Builder` (`src/strings/builder.go`), the whole point is to avoid the append-realloc churn when building strings:

```go
type Builder struct {
    addr *Builder // detects copies-by-value
    buf  []byte
}

func (b *Builder) Grow(n int) {
    // ...
    if cap(b.buf)-len(b.buf) < n {
        b.grow(n)
    }
}

func (b *Builder) grow(n int) {
    buf := bytesGrowslice(b.buf, n) // one deliberate growth
    b.buf = buf
}

func (b *Builder) String() string {
    // zero-copy: reinterpret the []byte as a string without allocating
    return unsafe.String(unsafe.SliceData(b.buf), len(b.buf))
}
```

Two production lessons in a few lines: (1) `Grow(n)` lets the caller pre-size the backing array once instead of paying geometric reallocation — the same `make(..., 0, n)` idea, exposed as API. (2) `String()` uses `unsafe` to return the accumulated bytes *as a string with no copy*, which is safe here only because `Builder` guarantees the `buf` is never mutated again after `String()`. This is the kind of "why is it written this way" you can now answer: it is eliminating the copy that a naive `string(b.buf)` would force.

## Common mistakes

- Appending in a loop without `make([]T, 0, n)` when the size is known.
- Returning `data[:i]` from a huge buffer and pinning the whole thing in memory.
- Assuming `append` returns a new backing array — it only does so on growth. Sharing spare capacity clobbers data.
- Mutating `range` values expecting to change the slice.
- Passing a slice by pointer (`*[]int`) "to avoid copying" — the header is already tiny; you almost never need `*[]T`. You need it only to have the callee change the caller's *header* (e.g. reassign length), which is rare.

## Best practices

- Preallocate with `make([]T, 0, n)` (or `make([]T, n)`) whenever the size is known or estimable.
- Use the three-index slice `s[a:b:c]` to cap capacity when handing sub-slices to code that may append.
- Copy out small slices of large buffers to let the big backing array be collected.
- Reset and reuse slices (`s = s[:0]`) to keep the backing array and avoid reallocation in loops; pair with `sync.Pool` for cross-call reuse.

## Performance analysis

```
$ go test -bench=Build -benchmem
BenchmarkAppendNoPrealloc-8    30000    45123 ns/op   357626 B/op   19 allocs/op
BenchmarkAppendPrealloc-8     200000     6187 ns/op    81920 B/op    1 allocs/op
```

Same result, 19 allocations vs 1, ~7× faster. The 19 allocations are the geometric growth steps (log₂ of the final size), each a `memmove` of everything so far. Preallocation collapses them to a single up-front allocation. This benchmark is worth running yourself once — watching `allocs/op` drop from 19 to 1 makes the slice header real.

## Production case study

A JSON API decoding large arrays saw p99 latency spikes correlated with GC. Profiling showed the decoder appending into a nil slice per request, each request triggering a cascade of growslice reallocations and feeding the garbage collector. Two changes fixed it: (1) size the destination slice from the JSON array length hint where available, and (2) pool the decode buffers with `sync.Pool` and reset with `buf = buf[:0]` between uses so the backing arrays were reused rather than re-allocated and re-collected. Allocations per request dropped by an order of magnitude and the GC-correlated p99 spikes disappeared. No algorithm changed — only the awareness that a slice is a header over a reused backing array.

## Exercises

1. Write a function that appends 1,000 ints to a nil slice and prints `len`/`cap` after each append. Identify every growth point and confirm the geometric pattern.
2. Reproduce Trap 1 (the shared-spare-capacity clobber) and fix it with the three-index slice.
3. Reproduce Trap 2: read a large file, return a 1-line sub-slice, and use `runtime.ReadMemStats` (or `pprof` heap) to show the whole file stays resident. Fix it with a copy.
4. Benchmark building a 10-KB string with `+=`, with `bytes.Buffer`, and with `strings.Builder` + `Grow`. Rank them by `allocs/op` and explain the ordering.

## Summary

- A slice is a **24-byte header** `{array, len, cap}` over a shared backing array. Passing a slice copies the header, not the data.
- `append` writes in place if `len < cap`, otherwise **reallocates and copies** with geometric growth (2× under 256, ~1.25× above). Amortized O(1), but each growth is a real allocation + copy.
- Preallocate with `make([]T, 0, n)` — the highest-frequency Go optimization.
- Aliasing is the hazard: shared spare capacity clobbers data (use `s[a:b:c]`), sub-slices pin whole backing arrays (copy out), and `range` values are copies (index to mutate).

Next → [Map internals: buckets, load factor, and Swiss tables](/backend-guide/go/01-type-system-and-memory/03-maps-internals)
