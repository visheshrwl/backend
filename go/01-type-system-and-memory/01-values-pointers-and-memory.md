---
title: Go Values, Pointers & Memory
description: Chapter 1 — Go passes everything by value, the compiler decides stack vs heap, and concurrent memory needs happens-before. Learn the one model that every other Go concept builds on, with runnable examples.
tags: ["go", "pointers", "memory", "stack", "heap", "value-semantics", "chapter-1"]
status: published
access: public
publishedAt: 2026-07-08
---

# Go Values, Pointers & Memory

> **Chapter 1 of the Go Engineering Handbook.** This is the foundation. Slices, maps, interfaces, the scheduler, and the garbage collector all build on the model in this chapter. Read it once, carefully, and the rest of the book gets easier.

## In this chapter you will learn

- The **one rule** for how Go passes data: everything is copied.
- The difference between a **value** and a **pointer**, and the `&` / `*` operators.
- Where your data actually lives: the **stack** vs the **heap**, and who decides.
- When to use a pointer — and when *not* to.
- The **memory model**: why concurrent code needs synchronization, and how to prove it does.

---

## Go Value Semantics

Go has exactly **one** rule for passing data to a function:

> **The Rule:** In Go, arguments are always **copied**. There is no pass-by-reference.

When you pass a value to a function, the function receives its **own copy**. Changing the copy does **not** change the original.

### Example

```go
package main

import "fmt"

func addTax(price float64) {
    price = price * 1.1 // changes the LOCAL copy only
}

func main() {
    price := 100.0
    addTax(price)
    fmt.Println(price) // the original is untouched
}
```

**Output:**

```
100
```

The `addTax` function got a *copy* of `price`. It changed its copy to `110`, then threw it away when it returned. The `price` in `main` never moved.

> **Note:** This is exactly like C. `func addTax(price float64)` is `void addTax(double price)`. If you come from Python or Java, resist the urge to think "objects are passed by reference" — in Go, they are not.

---

## Values vs Pointers

If a copy is passed, how does a function change the caller's data? You pass a **pointer** — the *memory address* of the value. Both the caller and the function then point at the **same** memory.

- `&x` means **"the address of x"** (a pointer to x).
- `*p` means **"the value that p points to"** (dereference).

### Example

```go
package main

import "fmt"

func addTaxByValue(price float64) {
    price = price * 1.1 // changes a copy
}

func addTaxByPointer(price *float64) {
    *price = *price * 1.1 // changes the ORIGINAL, through the pointer
}

func main() {
    a := 100.0
    addTaxByValue(a)
    fmt.Println("by value:  ", a) // unchanged

    b := 100.0
    addTaxByPointer(&b) // pass the ADDRESS of b
    fmt.Println("by pointer:", b) // changed
}
```

**Output:**

```
by value:   100
by pointer: 110.00000000000001
```

The second call passed `&b` (the address). Inside the function, `*price = ...` wrote through that address to the original `b`.

> **Note:** A pointer is just a copied address — 8 bytes on a 64-bit machine. When you pass `&b`, you copy the *address*, but both copies point at the same `b`. That is how the function reaches the caller's data.

### Value vs pointer at a glance

| | Value (`T`) | Pointer (`*T`) |
|---|---|---|
| What is passed | a **copy** of the whole value | a copy of the **address** (8 bytes) |
| Can change the caller's data? | ❌ No | ✅ Yes |
| Can be `nil`? | ❌ No | ✅ Yes (danger — see below) |
| Simpler / safer? | ✅ Yes | ⚠️ More care needed |

---

## The `&` and `*` Operators

Two operators, and they are mirror images of each other:

| Operator | Name | Reads as | Example |
|---|---|---|---|
| `&` | address-of | "give me the address of…" | `p := &x` |
| `*` (on a type) | pointer type | "pointer to…" | `var p *int` |
| `*` (on a value) | dereference | "the value at…" | `y := *p` |

### Example

```go
package main

import "fmt"

func main() {
    x := 42
    p := &x           // p is a *int, holding the address of x

    fmt.Println(x)    // 42  — the value
    fmt.Println(p)    // 0xc0000140a0 — an address (yours will differ)
    fmt.Println(*p)   // 42  — dereference: the value AT that address

    *p = 99           // write through the pointer
    fmt.Println(x)    // 99  — x changed!
}
```

**Output:**

```
42
0xc0000140a0
42
99
```

---

## Stack vs Heap — Who Decides?

Every value your program creates lives in one of two places:

- **The stack** — fast, automatic, per-function. Freed instantly when the function returns. **No garbage collector involved.**
- **The heap** — shared, longer-lived, managed by the **garbage collector (GC)**.

> **In C, you decide:** locals go on the stack, `malloc` goes on the heap, and returning `&local` is a bug.
> **In Go, the compiler decides.** This is called **escape analysis**.

The rule the Go compiler follows:

> **Escape rule:** A value stays on the **stack** if the compiler can prove it does not outlive the function. If a reference to it "escapes" the function, it moves to the **heap**.

This means returning the address of a local variable is **safe in Go** — the compiler sees the address escaping and puts the value on the heap for you.

### Example

```go
package main

import "fmt"

type Point struct{ X, Y int }

func newPoint() *Point {
    p := Point{1, 2} // looks like a stack local...
    return &p        // ...but its address escapes, so Go heap-allocates it
}

func main() {
    p := newPoint()
    fmt.Println(*p) // {1 2} — perfectly valid, no dangling pointer
}
```

**Output:**

```
{1 2}
```

### See the compiler's decision

You never have to guess where a value lives. Ask the compiler:

```
go build -gcflags='-m' main.go
```

**Output:**

```
./main.go:8:2: moved to heap: p
```

The compiler is telling you: `p` **escaped** to the heap because its address was returned.

```
   Stack (free, per-function)         Heap (managed by GC)
   ┌────────────────────────┐         ┌──────────────────────┐
   │ main:                  │         │  Point{1, 2}  ◄────┐  │
   │   p *Point ────────────┼─────────┼───────────────────┘  │
   └────────────────────────┘         └──────────────────────┘
                                        freed later by the GC
```

> **Why you care:** Stack allocation is nearly free and needs no GC. Heap allocation costs an allocation *plus* future GC work. In a hot loop, keeping values on the stack instead of the heap can be the difference between **0 allocations** and thousands. (Chapter on Escape Analysis goes deep — for now, just know the compiler chooses, and you can watch it.)

---

## When to Use a Pointer

Beginners often make everything a pointer "to be safe." **This is a mistake.** It forces heap allocations, adds GC work, and invites `nil` crashes.

> **The Tip:** Default to **values**. Reach for a pointer only for one of two reasons.

| Use a **pointer** when… | Use a **value** when… |
|---|---|
| The function must **modify** the caller's data | You only need to **read** the data |
| Several holders must **share** one mutable object | Each holder can have its own copy |
| The struct is **large** and copying it is costly | The struct is small (a few fields) |
| — | *Everything else (the common case)* |

> **Warning — "Pointer-itis":** Making every field and parameter a `*T` is an anti-pattern. Values are simpler, have no `nil`, are cache-friendly, and don't add GC pressure. Only introduce a pointer when you can name *which* of the two reasons above applies.

---

## nil Pointers

A pointer that points at nothing is `nil`. Dereferencing a `nil` pointer **panics** and crashes your program (unless recovered).

### Example

```go
package main

import "fmt"

type User struct{ Name string }

func main() {
    var u *User      // declared but not assigned → nil
    fmt.Println(u)   // <nil>
    fmt.Println(u.Name) // 💥 panic: runtime error: invalid memory address
}
```

**Output:**

```
<nil>
panic: runtime error: invalid memory address or nil pointer dereference
```

> **Note:** This is the price of pointers, and the reason to prefer values. A value type can never be `nil`, so it can never nil-panic. Every `*T` in your code is a place where a `nil` check might be needed.

---

## The Go Memory Model (happens-before)

Everything above is about a **single** goroutine. The moment **two goroutines** touch the same data, a new rule applies — and getting it wrong is one of the most dangerous bugs in Go.

> **The Rule:** If two goroutines access the same variable, and at least one **writes**, you **must** synchronize them. Otherwise the behavior is undefined. This is a **data race**.

Synchronization creates a *happens-before* relationship — a guarantee that one goroutine's write is visible to another's read. The tools that create it:

- **Channels** — a send happens-before the matching receive.
- **`sync.Mutex`** — an `Unlock` happens-before the next `Lock`.
- **`sync/atomic`** — atomic operations are ordered.
- **`sync.WaitGroup`, `sync.Once`** — carry their own guarantees.

Without one of these, there is **no ordering guarantee at all** — not even for a single `bool`.

### Example — a real bug

```go
package main

var ready bool
var data int

func main() {
    go func() {
        data = 42     // write 1
        ready = true  // write 2 — NO synchronization
    }()

    for !ready { }    // spin, waiting...
    println(data)     // may print 0, or spin forever
}
```

This looks correct and is **broken**. With no synchronization, the compiler and CPU are free to reorder or cache the writes. The reader may never see `ready` become `true`, or may see `ready == true` but `data == 0`.

### The fix — and how to catch it

Use a channel (or mutex, or atomic):

```go
package main

func main() {
    done := make(chan int)

    go func() {
        data := 42
        done <- data // send happens-before the receive → data is visible
    }()

    result := <-done // safely receives 42
    println(result)
}
```

> **Warning:** Data races are invisible until they corrupt production. **Always** run your tests with the race detector:
> ```
> go test -race ./...
> ```
> It is the single highest-value tool in concurrent Go. Run it in CI. Treat any race it finds as a release blocker.

---

## Common Mistakes

- ❌ **Pointer-itis** — making everything `*T`. Forces heap allocations and invites nil panics. Default to values.
- ❌ **Assuming a copy is a reference** — passing a struct and expecting the caller to see the change. It won't; pass a pointer.
- ❌ **Dereferencing without a nil check** — every `*T` can be `nil`.
- ❌ **Sharing a variable across goroutines with no synchronization** — a data race, even for one `bool`.
- ❌ **Believing `&local` is dangerous** — in Go it is safe; the compiler heap-allocates escaping values.

---

## Best Practices

- ✅ **Prefer values.** Introduce a pointer only to mutate, to share a mutable object, or to avoid a large copy.
- ✅ **Let the compiler tell you where memory lives.** Use `go build -gcflags='-m'` on hot paths.
- ✅ **Never share mutable data across goroutines without a channel, mutex, or atomic.** If you can't name the synchronizer, it's a race.
- ✅ **Run `go test -race`** in tests and CI.

---

## Chapter Summary

You now know Go's memory foundation:

- Go passes **everything by value (a copy)**. A pointer (`&x`) is a copied address that shares the target; `*p` dereferences it.
- The **compiler** decides stack vs heap via **escape analysis**. Stack is free; heap is GC-managed. Returning `&local` is safe.
- Use a pointer to **mutate**, **share**, or **avoid a big copy** — otherwise prefer a **value**.
- A `nil` pointer dereference **panics**; value types can't be nil.
- Concurrent access to shared data needs a **happens-before** edge (channel / mutex / atomic). Without one it's a **data race** — find them with `go test -race`.

---

## Chapter 1 Quiz

Test yourself. Answers below.

**Q1.** What does this print?
```go
func f(n int) { n = 10 }
func main() { x := 5; f(x); println(x) }
```

**Q2.** True or false: returning `&x` where `x` is a local variable is a bug in Go.

**Q3.** You have a small `struct` with two `int` fields, and a function that only *reads* it. Value or pointer parameter?

**Q4.** Two goroutines share an `int` counter; one increments it. Is a plain `counter++` safe? What makes it safe?

**Q5.** Which command shows you whether a variable escapes to the heap?

### Answers

> **Try the questions first.** The answers are below — no peeking until you've committed to an answer for each.

- **A1.** `5`. `f` received a copy; changing `n` didn't touch `x`.
- **A2.** **False.** It is safe — the compiler detects the escape and allocates `x` on the heap.
- **A3.** **Value.** Small and read-only → no reason for a pointer. A pointer would add GC pressure and a nil hazard for nothing.
- **A4.** **Not safe** — it's a data race. Make it safe with a `sync.Mutex`, `sync/atomic` (e.g. `atomic.Int64`), or by owning the counter in a single goroutine you communicate with via a channel.
- **A5.** `go build -gcflags='-m'` (add a second `-m` for the reasoning).

---

## Exercises

1. Write `func swap(a, b *int)` that swaps two ints through pointers. Call it and confirm the originals changed.
2. Write a function that returns `*int` pointing to a local. Confirm with `-gcflags='-m'` that the local "moved to heap." Then rewrite it to return `int` and confirm it does **not** escape.
3. Reproduce the `ready`/`data` race from this chapter and confirm `go run -race main.go` reports it. Fix it three ways: with a channel, with a `sync.Mutex`, and with `sync/atomic`.
4. Take a 4-field struct; write it as both a value parameter and a pointer parameter; benchmark both with `go test -bench=. -benchmem`. Note that at this size, neither allocates and the difference is tiny — a lesson in *not* reaching for pointers prematurely.

---

Next chapter → [Building a Production HTTP Server](/backend-guide/go/06-production-services/01-http-servers-and-middleware)
