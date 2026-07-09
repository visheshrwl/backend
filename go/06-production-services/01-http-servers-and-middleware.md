---
title: Building a Production HTTP Server
description: Chapter 2 — every backend service is a loop that accepts connections and responds. Build one the way production demands, from the TCP accept queue up to graceful shutdown, with real code you copy into real services.
tags: ["go", "http", "production", "server", "middleware", "graceful-shutdown", "chapter-2"]
status: published
access: public
publishedAt: 2026-07-08
---

# Building a Production HTTP Server

> **Chapter 2 of the Go Engineering Handbook.** Chapter 1 was the foundation — how memory works. Now we build the thing that actually earns money: a server that accepts requests and does not fall over at 3 AM. Everything here is real production code — functions you will write, copy, and run in real services.

Let's start from first principles.

Strip away the frameworks and the buzzwords, and a backend service is one simple loop:

```
accept a connection  →  read a request  →  do work  →  write a response  →  repeat
```

That's it. That is the whole job. The reason this chapter is long is that **every one of those five steps has a way to hurt you in production** — a slow client that never finishes sending, a deploy that severs an in-flight payment, a panic in one handler that should not take down the process. Production engineering is knowing where each step can bleed, and closing the wound *before* it pages you.

## In this chapter you will learn

- What **actually happens** when a request arrives — from the TCP handshake and the kernel accept queue to the goroutine that runs your handler.
- Why the "hello world" server (`http.ListenAndServe`) is a **production incident waiting to happen**.
- The **real `http.Server` configuration** every service needs, timeout by timeout, each mapped to a failure mode.
- **Graceful shutdown** — the code that makes zero-downtime deploys possible.
- The **middleware chain** — request IDs, structured logging, panic recovery, per-request timeouts — as real, reusable functions.
- **Health and readiness** endpoints, body limits, and concurrency limiting.

---

## Part 1 — What Actually Happens When a Request Arrives

Before we write a single line, let's discuss what happens under the hood. This is the part most engineers skip, and it is exactly the part that explains every production setting later. Bear with the detail — it pays off.

### The connection lifecycle, step by step

When a client hits your server, here is the real sequence at the operating-system level:

```
CLIENT                          KERNEL (your box)                 GO RUNTIME
  │                                   │                               │
  │  1. TCP SYN  ───────────────────► │                               │
  │  ◄── 2. SYN-ACK ──────────────────│  (3-way handshake)            │
  │  3. ACK  ───────────────────────► │                               │
  │                                   │                               │
  │                          [ completed-connection queue ]           │
  │                          (the "accept queue", size = backlog)     │
  │                                   │                               │
  │                                   │ ◄── 4. Accept() pulls one ────│  listener.Accept()
  │                                   │        connection off the queue│
  │                                   │        → a new file descriptor │
  │                                   │                               │
  │  5. sends HTTP bytes ───────────► │  (socket receive buffer)      │
  │                                   │                               │ 6. go serve(conn)
  │                                   │                               │    ONE GOROUTINE
  │  ◄──────── 7. HTTP response ──────│◄──────────────────────────────│    per connection
```

Walk through it slowly, because each numbered step maps to something you will configure:

1. **The handshake (steps 1–3).** Before your Go code sees anything, the kernel completes the TCP three-way handshake. The connection now exists at the OS level, fully established, *whether or not your application has looked at it yet*.

2. **The accept queue (the backlog).** Completed connections sit in a kernel queue — the "accept queue" — waiting for your application to pick them up. This queue has a fixed size (the *backlog*). Here is the crux: **if your application accepts connections slower than they arrive, this queue fills up, and new connections get dropped or refused by the kernel.** This is a real production failure mode — "connection refused" errors under load often mean your accept loop can't keep up, not that your server is down.

3. **`Accept()` and the file descriptor.** Your server calls `Accept()`, which pulls one connection off the queue and hands you a **file descriptor** — an integer that represents the socket. This matters enormously: every open connection consumes a file descriptor, and your process has a **limit** (`ulimit -n`, often 1024 by default — far too low for production). Run out of file descriptors and your server stops accepting *anything*, including health checks. We will come back to this.

4. **A goroutine per connection.** Go's `net/http` then does something beautiful in its simplicity: it launches **one goroutine per connection** (`go c.serve(...)`). Your handler runs on that goroutine. Because goroutines are cheap (Chapter on the scheduler covers why — ~8 KB stacks, user-space scheduling), one process comfortably handles tens of thousands of concurrent connections. A thread-per-connection C or Java-without-Loom server would have collapsed long before.

> **Note — why this is beautiful:** You write plain, blocking, top-to-bottom handler code. Under the hood, when a handler blocks reading the request body, the Go runtime *parks* that goroutine and hands the CPU to another one, registering the socket with the OS event system (`epoll` on Linux). You get the scalability of an async event loop with the readability of synchronous code. You did nothing to earn it. That is the entire design win of Go for backends.

### Why you must care about all this

Two production truths fall directly out of the lifecycle above:

- **Connections cost file descriptors, and file descriptors are finite.** A slow or malicious client that opens a connection and *just sits there* holds a file descriptor hostage. Enough of them and you hit the limit — new connections fail. This is the mechanism behind the "slow loris" attack and behind many an outage during a network blip.
- **Your handler runs concurrently with every other in-flight request.** Two requests hitting the same shared `map` at the same time is a `fatal error: concurrent map writes` (Chapter 1's memory model — happens-before is not optional). Handler state must be immutable or synchronized.

Hold these two truths. Everything below is a response to them.

---

## Part 2 — The Naive Server (and Why It's Dangerous)

Here is the server you see in every tutorial:

### Example

```go
package main

import (
    "fmt"
    "net/http"
)

func main() {
    http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
        fmt.Fprintln(w, "hello")
    })
    http.ListenAndServe(":8080", nil) // ⚠️ do not ship this
}
```

**What you'll see:**

```
$ curl localhost:8080
hello
```

It works. It compiles. It responds. And it is **dangerous in production**. Let's discuss exactly why.

`http.ListenAndServe(":8080", nil)` creates an `http.Server` with **every timeout set to zero — meaning infinite.** Think back to Part 1. A client can:

- Open a connection (consume a file descriptor), then send its request headers **one byte per second**, forever. Your server waits. Patiently. Forever. This is the **slow-loris attack** — a handful of clients can exhaust your file descriptors and take the service down with almost no bandwidth.
- Start reading a response and then stall, holding the connection and its resources open indefinitely.
- Keep an idle keep-alive connection open forever, accumulating until you run out of descriptors.

> **Warning:** `http.ListenAndServe(addr, nil)` with no timeouts is the single most common production mistake in Go. It passes every test, survives every code review that doesn't know to look, and then falls over the first time the network gets ugly or someone points a slow-loris script at it. **Never ship it.**

There is also the ignored return value — `http.ListenAndServe` returns an `error`, and the naive version drops it. In production, a server that fails to bind its port should crash loudly, not fail silently.

Put plainly: this code is optimized for the *demo*, and production is not a demo. At 10 requests per second on your laptop, nothing here matters. At 10,000 requests per second with real clients on real flaky networks, every one of these omissions becomes an incident. So let's build the real thing.

---

## Part 3 — The Production Server, Configured Properly

Here is a production-grade server. Read it once, then we'll go setting by setting.

### Example

```go
package main

import (
    "net/http"
    "time"
)

func newServer(handler http.Handler) *http.Server {
    return &http.Server{
        Addr:    ":8080",
        Handler: handler,

        // --- Timeouts: the difference between a demo and a service ---
        ReadHeaderTimeout: 5 * time.Second,   // time allowed to read request HEADERS
        ReadTimeout:       15 * time.Second,  // time allowed to read the ENTIRE request
        WriteTimeout:      15 * time.Second,  // time allowed to write the response
        IdleTimeout:       60 * time.Second,  // keep-alive idle connection lifetime

        // --- Limits ---
        MaxHeaderBytes: 1 << 20,              // 1 MB cap on request headers
    }
}
```

Now let's enumerate what each one *actually protects* — because a setting you don't understand is a setting you'll delete the first time it's inconvenient.

| Setting | What it bounds | The attack / failure it closes |
|---|---|---|
| `ReadHeaderTimeout` | Time to receive the request line + headers | **Slow loris.** A client dribbling headers is cut off after 5s instead of holding a connection forever. |
| `ReadTimeout` | Time to receive the *entire* request (headers + body) | A slow or stalled body upload can't pin a connection indefinitely. |
| `WriteTimeout` | Time to *write* the full response | A client that reads the response one byte per second can't stall your handler forever. |
| `IdleTimeout` | How long an idle keep-alive connection stays open | Prevents idle connections from accumulating and exhausting file descriptors. |
| `MaxHeaderBytes` | Maximum size of request headers | A client sending gigabytes of headers can't exhaust your memory. |

> **Tip — the mental model for timeouts:** every timeout answers one question: *"how long am I willing to hold a resource for a client that may never finish?"* In production the answer is never "forever." Pick numbers based on your real traffic — an internal API might use 5s reads; a file-upload endpoint needs a longer `ReadTimeout` (or per-route handling), and a streaming/SSE endpoint may need `WriteTimeout` disabled *for that route only*. Defaults are a starting point, not gospel.

> **Note — the subtle one, `WriteTimeout`:** `WriteTimeout` starts counting from the *end of reading the request headers*, so it bounds your handler's total time to produce and send the response. If you have long-running handlers, this is the setting that will "mysteriously" cut them off. That's usually a signal to move the long work off the request path (a job queue), not to disable the timeout.

### There is no `HandlerTimeout` — use `http.TimeoutHandler`

A common question: "how do I limit how long a *handler* runs?" The server-level timeouts bound I/O, not your handler's CPU/wait time. For that, wrap the handler:

```go
handler = http.TimeoutHandler(handler, 10*time.Second, "request timed out")
```

`http.TimeoutHandler` gives the handler a `context` with a deadline and, if it exceeds it, writes a `503` with your message. We'll prefer doing this in middleware (Part 5) so it's part of the chain — but know this exists.

---

## Part 4 — Graceful Shutdown (Zero-Downtime Deploys)

This is the section that separates services that deploy cleanly from services that spray errors on every rollout. Let's understand the problem first.

### The problem

You deploy ten times a day. Each deploy, your orchestrator (Kubernetes, ECS, whatever) sends your process a **`SIGTERM`** and, after a grace period, a `SIGKILL`. Now ask the first-principles question: **what happens to the requests that are in-flight at the moment `SIGTERM` arrives?**

With the naive server: the process just dies. Every in-flight request — including that user's checkout — is severed mid-flight. The client sees a connection reset. Multiply by every deploy and you have a steady drip of 5xx errors that correlates suspiciously with your release schedule.

Graceful shutdown fixes this. The sequence we want:

```
  SIGTERM arrives
       │
       ▼
  1. STOP accepting new connections   (close the listener)
       │
       ▼
  2. LET in-flight requests finish    (drain, up to a deadline)
       │
       ▼
  3. Close idle keep-alive connections
       │
       ▼
  4. Exit cleanly (0 dropped requests)
```

`http.Server.Shutdown(ctx)` does exactly steps 1–3. Our job is to wire it to the signal and give it a deadline.

### Example — the real `run()` pattern

This is the idiomatic production entry point. Notice `main` does almost nothing — it delegates to `run()` which returns an `error`. This is a widely-used Go pattern (popularized by Mat Ryer) because it makes the startup/shutdown path testable and keeps `os.Exit` in exactly one place.

```go
package main

import (
    "context"
    "errors"
    "log"
    "net/http"
    "os"
    "os/signal"
    "syscall"
    "time"
)

func main() {
    if err := run(); err != nil {
        log.Fatalf("server exited with error: %v", err)
    }
    log.Println("server stopped cleanly")
}

func run() error {
    mux := http.NewServeMux()
    mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
        w.Write([]byte("hello"))
    })

    srv := newServer(mux) // the configured server from Part 3

    // ctx is cancelled when SIGINT (Ctrl-C) or SIGTERM (deploy) arrives.
    ctx, stop := signal.NotifyContext(context.Background(),
        os.Interrupt, syscall.SIGTERM)
    defer stop()

    // Start serving in a background goroutine so we can wait for the signal.
    serverErr := make(chan error, 1)
    go func() {
        log.Printf("listening on %s", srv.Addr)
        // ListenAndServe returns ErrServerClosed on a clean Shutdown —
        // that is SUCCESS, not an error. Anything else is a real failure.
        if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
            serverErr <- err
        }
    }()

    // Block until either the server dies on its own, or we get a signal.
    select {
    case err := <-serverErr:
        return err // failed to bind, etc. — crash loudly
    case <-ctx.Done():
        log.Println("shutdown signal received, draining...")
    }

    // Give in-flight requests up to 30s to finish. The orchestrator's grace
    // period should be LONGER than this, or it will SIGKILL us mid-drain.
    shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
    defer cancel()

    if err := srv.Shutdown(shutdownCtx); err != nil {
        // Drain didn't finish in time — force close and report it.
        _ = srv.Close()
        return err
    }
    return nil
}
```

**What you'll see on a deploy:**

```
2026/07/08 10:00:01 listening on :8080
2026/07/08 10:03:47 shutdown signal received, draining...
2026/07/08 10:03:47 server stopped cleanly
```

Let's discuss the three details that make this correct, because each is a place people get it wrong:

1. **`http.ErrServerClosed` is not an error.** When `Shutdown` runs, `ListenAndServe` returns `http.ErrServerClosed`. That is the *success* signal. If you treat every non-nil error from `ListenAndServe` as a crash, you'll log a scary error on every clean shutdown. The `errors.Is(err, http.ErrServerClosed)` check (Chapter on errors) is how you tell "clean stop" from "failed to bind port 8080."

2. **`signal.NotifyContext` (Go 1.16+)** turns OS signals into context cancellation — a clean, modern idiom. The older `signal.Notify(ch, ...)` with a channel still works, but tying it to a context means the same cancellation flows naturally into everything else that takes a `ctx`.

3. **The shutdown deadline vs the orchestrator's grace period.** This is the killer subtlety. Kubernetes gives you `terminationGracePeriodSeconds` (default 30s) between `SIGTERM` and `SIGKILL`. Your `Shutdown` timeout **must be shorter** than that, or Kubernetes will `SIGKILL` you mid-drain — defeating the entire point. Set the drain to, say, 25s if the grace period is 30s.

> **Tip — the pre-shutdown delay you'll eventually need:** In Kubernetes there's a race: the moment your pod gets `SIGTERM`, it may still be receiving new traffic for a second or two because endpoint removal propagates asynchronously. Mature setups add a short `time.Sleep` (a few seconds) *before* calling `Shutdown`, or flip a readiness flag to false first, so the load balancer stops routing before you stop accepting. Know that this race exists; you'll meet it.

---

## Part 5 — Middleware: The Production Chain

Every real service needs the same cross-cutting concerns on *every* request: a request ID for tracing, a log line, panic recovery so one bad handler doesn't take down the process, a timeout. You do not write these into each handler. You write them **once**, as middleware.

### What middleware is (first principles)

Middleware is a function that takes a handler and returns a *wrapped* handler. Because the wrapper is itself an `http.Handler`, wrappers compose — you stack them like an onion.

```go
type Middleware func(http.Handler) http.Handler
```

The execution model is the key mental picture — an onion, where each layer runs code *on the way in* and *on the way out*:

```
   request
      │
      ▼
 ┌──────────────────────────────────────────┐
 │ Recover   (defer recover)                 │  ← outermost: catches everything
 │ ┌────────────────────────────────────────┐│
 │ │ RequestID  (attach id to context)      ││
 │ │ ┌──────────────────────────────────────┐│
 │ │ │ Logging  (start timer)               │││
 │ │ │ ┌────────────────────────────────────┐│
 │ │ │ │ Timeout  (ctx deadline)            │││
 │ │ │ │ ┌──────────────────────────────────┐│
 │ │ │ │ │      YOUR HANDLER                │││
 │ │ │ │ └──────────────────────────────────┘│
 │ │ │ └────── log line (on the way out) ───┘││
 │ │ └────────────────────────────────────────┘│
 │ └──────────────────────────────────────────┘│
 └──────────────────────────────────────────────┘
      │
      ▼
   response
```

### A tiny chain helper

First, a helper to apply middleware in readable order. Without it, nesting reads inside-out (`Recover(RequestID(Logging(mux)))`); with it, it reads top-to-bottom.

```go
// chain applies middleware so the FIRST listed runs OUTERMOST.
func chain(h http.Handler, mw ...Middleware) http.Handler {
    for i := len(mw) - 1; i >= 0; i-- {
        h = mw[i](h)
    }
    return h
}

// usage:
handler := chain(mux,
    Recover,     // outermost — catches panics from everything inside
    RequestID,   // then attach a request id
    Logging,     // then log (sees the id, times the inner handlers)
    Timeout(10*time.Second),
)
```

Now the real middleware. Every one of these is a function you will paste into a real service.

### Middleware 1 — Request ID (correlation)

Every request gets a unique ID, stored in the context, so all logs for one request can be correlated — indispensable the moment you have more than one log line per request.

```go
type ctxKey int
const requestIDKey ctxKey = 0

func RequestID(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        // Respect an upstream/proxy-provided id, else generate one.
        id := r.Header.Get("X-Request-ID")
        if id == "" {
            id = newID() // e.g. a UUID or random hex; keep it cheap
        }
        ctx := context.WithValue(r.Context(), requestIDKey, id)
        w.Header().Set("X-Request-ID", id) // echo it back for the client
        next.ServeHTTP(w, r.WithContext(ctx))
    })
}

// RequestIDFrom pulls the id out anywhere downstream (handlers, DB layer, logs).
func RequestIDFrom(ctx context.Context) string {
    id, _ := ctx.Value(requestIDKey).(string)
    return id
}
```

> **Note — the typed context key.** `ctxKey` is an *unexported* type used as the key. This is the idiomatic way to avoid key collisions in `context.Value` (Chapter 1 warned that context values are untyped `any`). Never use a plain string like `"requestID"` as a context key — two packages could pick the same string and clobber each other. A private type makes collisions impossible.

### Middleware 2 — Logging (and the `ResponseWriter` wrapper you must know)

Here's a real production gotcha. You want to log the response **status code** and **byte count**, but `http.ResponseWriter` doesn't expose them after the fact. The standard solution — and a function every Go engineer ends up writing — is a small wrapper that *records* what the handler wrote:

```go
// statusRecorder wraps http.ResponseWriter to capture the status and size.
type statusRecorder struct {
    http.ResponseWriter
    status int
    bytes  int
}

func (r *statusRecorder) WriteHeader(code int) {
    r.status = code
    r.ResponseWriter.WriteHeader(code)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
    if r.status == 0 {
        r.status = http.StatusOK // Write without WriteHeader implies 200
    }
    n, err := r.ResponseWriter.Write(b)
    r.bytes += n
    return n, err
}

func Logging(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        start := time.Now()
        rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}

        next.ServeHTTP(rec, r) // pass the RECORDER down, not w

        log.Printf("%s %s %d %dB %s id=%s",
            r.Method, r.URL.Path, rec.status, rec.bytes,
            time.Since(start), RequestIDFrom(r.Context()))
    })
}
```

**What you'll see:**

```
GET /users/42 200 118B 3.214ms id=a1b2c3d4
POST /orders 500 41B 852µs id=e5f6a7b8
```

> **Tip — this wrapper has a real limitation.** Wrapping `ResponseWriter` can *hide* optional interfaces the underlying writer implements — `http.Flusher` (for streaming/SSE), `http.Hijacker` (for WebSockets). If your wrapped handler needs those and your recorder doesn't forward them, streaming breaks. In production you either forward those interfaces explicitly or use `httpsnoop` (a well-known library that preserves them). This is exactly the kind of subtle thing that works in tests and breaks the first WebSocket connection — worth knowing before it bites you.

### Middleware 3 — Recover (never let one handler kill the process)

A `nil` pointer dereference in a handler (Chapter 1) will `panic`. Without recovery, that panic unwinds the connection's goroutine and the client gets a dropped connection with no response. Worse, an unrecovered panic in some code paths can crash the whole process. Recovery middleware turns a handler panic into a clean `500`:

```go
func Recover(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        defer func() {
            if err := recover(); err != nil {
                // Log the panic WITH a stack trace and the request id.
                log.Printf("PANIC id=%s %s %s: %v\n%s",
                    RequestIDFrom(r.Context()), r.Method, r.URL.Path,
                    err, debug.Stack())
                // The client gets a clean 500, the server stays up.
                w.WriteHeader(http.StatusInternalServerError)
                w.Write([]byte(`{"error":"internal server error"}`))
            }
        }()
        next.ServeHTTP(w, r)
    })
}
```

> **Warning — recover is not a catch-all.** `recover` only catches `panic`s on the *same goroutine*. If your handler does `go doWork()` and *that* goroutine panics, this middleware will **not** save you — that panic crashes the whole process. Rule: any goroutine you spawn must have its **own** recover (or, better, not panic). And remember from Chapter 1: some runtime conditions (`concurrent map writes`, out-of-memory) are `fatal error`s that `recover` cannot catch at all. Recovery middleware is a safety net for handler bugs, not a license to be careless with goroutines.

### Middleware 4 — Timeout (bound every request)

Attach a deadline to the request's context so slow downstream work (a hung DB query) can't pin the request forever. Because we pass the context down (Chapter on context propagation), this deadline reaches all the way to your database driver.

```go
func Timeout(d time.Duration) Middleware {
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            ctx, cancel := context.WithTimeout(r.Context(), d)
            defer cancel() // ALWAYS — or you leak the timer (Chapter 1's rule)
            next.ServeHTTP(w, r.WithContext(ctx))
        })
    }
}
```

> **Note:** this bounds work that *respects context* — a `pgx` query, an outbound HTTP call with the context attached. It does **not** forcibly kill a CPU-bound loop that never checks `ctx.Err()`. Well-behaved handlers thread `r.Context()` into every blocking call so the deadline actually does something. That's the discipline that makes timeouts real instead of decorative.

---

## Part 6 — Health and Readiness Endpoints

Your orchestrator needs to ask two different questions, and conflating them causes outages. Let's be precise — this is one people get exactly backwards.

- **Liveness (`/healthz`): "Are you alive, or should I restart you?"** Answer this **cheaply and locally** — do *not* check the database here. Why? Because if your database has a blip and your liveness probe checks the DB, Kubernetes will conclude your (perfectly healthy) pods are dead and **restart all of them at once** — turning a brief DB hiccup into a full outage. Liveness must only fail when the *process itself* is unrecoverable.

- **Readiness (`/readyz`): "Are you ready to receive traffic *right now*?"** *This* one checks dependencies — can I reach the database, is the connection pool healthy. If not ready, the orchestrator stops routing traffic to this pod but **does not** restart it, giving it time to recover.

### Example

```go
// Liveness: cheap, local, no dependencies. If the process runs, it's alive.
func healthz(w http.ResponseWriter, r *http.Request) {
    w.WriteHeader(http.StatusOK)
    w.Write([]byte("ok"))
}

// Readiness: check the things you NEED to serve a request.
func readyz(db *pgxpool.Pool) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
        defer cancel()

        if err := db.Ping(ctx); err != nil {
            w.WriteHeader(http.StatusServiceUnavailable) // 503 → stop routing here
            w.Write([]byte("db unreachable"))
            return
        }
        w.WriteHeader(http.StatusOK)
        w.Write([]byte("ready"))
    }
}
```

> **Tip — wire them out of the middleware chain.** Health checks should be *fast* and *unlogged* (they fire every few seconds; you don't want them drowning your logs or being subject to auth/timeout middleware). Register them on the mux directly, or exempt them in your logging middleware. Also: the readiness check having its own short timeout is deliberate — a readiness probe that hangs is as bad as one that fails.

---

## Part 7 — Limits: Don't Let One Client Sink the Ship

Two more real protections you'll want.

### Limit request body size

An endpoint that reads a body should cap it, or one client uploading a 10 GB body can exhaust your memory. `http.MaxBytesReader` enforces it *and* returns a clean `413` when exceeded:

```go
func handleUpload(w http.ResponseWriter, r *http.Request) {
    r.Body = http.MaxBytesReader(w, r.Body, 10<<20) // 10 MB cap
    var payload Payload
    if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
        http.Error(w, "payload too large or malformed", http.StatusRequestEntityTooLarge)
        return
    }
    // ...
}
```

### Limit concurrency (a semaphore in front of expensive work)

Say a route does something expensive (image resizing, a heavy report). Unbounded concurrency means a traffic spike spawns unbounded work and OOMs the box. A buffered channel used as a **counting semaphore** is the idiomatic Go bound:

```go
// LimitConcurrency allows at most n requests through at once; the rest wait
// briefly (bounded by their own context) or get shed with 503.
func LimitConcurrency(n int) Middleware {
    tokens := make(chan struct{}, n) // n tokens = n concurrent slots
    return func(next http.Handler) http.Handler {
        return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
            select {
            case tokens <- struct{}{}: // acquire a slot
                defer func() { <-tokens }() // release it when done
                next.ServeHTTP(w, r)
            case <-r.Context().Done(): // client gave up while waiting
                http.Error(w, "timeout", http.StatusServiceUnavailable)
            }
        })
    }
}
```

> **Note — this is load shedding, and it's a feature.** Under overload, a service that *sheds* excess load (returns 503 fast) stays healthy for the requests it does accept. A service that tries to serve *everything* under overload serves *nothing* — it grinds to a halt as every request slows down together. Bounding concurrency and shedding the overflow is how you degrade gracefully instead of collapsing. This is one of the most important production instincts, and Go's buffered-channel-as-semaphore makes it a five-line function.

---

## Putting It All Together

The complete production skeleton — this is genuinely close to what a real service's `main` looks like:

```go
func run() error {
    db := mustConnectDB()         // pgxpool, covered in a later chapter
    defer db.Close()

    mux := http.NewServeMux()
    mux.HandleFunc("GET /healthz", healthz)         // liveness — unlogged, no deps
    mux.Handle("GET /readyz", readyz(db))           // readiness — checks the db
    mux.HandleFunc("GET /users/{id}", getUser(db))  // 1.22+ path-param routing

    handler := chain(mux,
        Recover,                    // outermost: catch all panics
        RequestID,                  // correlation id
        Logging,                    // structured access log
        Timeout(10*time.Second),    // per-request deadline
        LimitConcurrency(256),      // load shedding
    )

    srv := newServer(handler)       // configured timeouts from Part 3
    // ... signal.NotifyContext + Shutdown from Part 4 ...
    return serveWithGracefulShutdown(srv)
}
```

Notice how little of this is "business logic." That's the point: production readiness is mostly *cross-cutting infrastructure* done once and done right, so your handlers can stay small and focused.

---

## Common Mistakes

- ❌ **`http.ListenAndServe` with no timeouts** — the #1 production Go mistake. Slow-loris bait, file-descriptor exhaustion.
- ❌ **No graceful shutdown** — every deploy severs in-flight requests. A drip of 5xx correlated with releases.
- ❌ **Treating `http.ErrServerClosed` as a failure** — logs a scary error on every clean shutdown.
- ❌ **Shutdown timeout ≥ orchestrator grace period** — Kubernetes `SIGKILL`s you mid-drain.
- ❌ **Checking the database in the liveness probe** — a DB blip triggers a mass restart, turning a hiccup into an outage.
- ❌ **Spawning a goroutine in a handler with no recover** — its panic crashes the whole process; the request-level `Recover` won't catch it.
- ❌ **Unbounded request bodies / unbounded concurrency** — one client can OOM the box.
- ❌ **Shared handler state without synchronization** — concurrent requests race; a shared `map` is a `fatal error`.

## Best Practices

- ✅ Always configure `ReadHeaderTimeout`, `ReadTimeout`, `WriteTimeout`, `IdleTimeout`, `MaxHeaderBytes`; tune per real traffic.
- ✅ Implement graceful shutdown with `signal.NotifyContext` + `srv.Shutdown(ctx)`, drain shorter than the orchestrator grace period.
- ✅ Use the `run() error` pattern so startup/shutdown is testable and `os.Exit` lives in one place.
- ✅ Build the standard middleware chain: Recover → RequestID → Logging → Timeout → (limits). Order matters.
- ✅ Separate **liveness** (cheap, local) from **readiness** (checks dependencies).
- ✅ Cap bodies with `MaxBytesReader`; bound expensive routes with a semaphore and shed overflow.
- ✅ Raise the file-descriptor limit (`ulimit -n` / container limits) for real concurrency.
- ✅ Stay close to `net/http`. Routers like `chi` are just this pattern composed — you can read them in an afternoon.

## Production Case Study

A service ran happily for months on `http.ListenAndServe` with default timeouts. Then an upstream network incident caused thousands of clients to hang mid-request. Each hung request held a connection and a **file descriptor**; with no `ReadTimeout` to cut them off, the descriptors piled up until the process hit its `ulimit` and could no longer `Accept` *anything* — including its own health checks, so the orchestrator saw it as dead and restarted it, into the same storm. The fix was three changes, all from this chapter: set the read/write/idle timeouts (so hung clients are dropped, not held), raise the fd limit to a production value, and separate liveness (cheap) from readiness (DB-aware) so a dependency blip stops routing instead of triggering restarts. Separately, every deploy had been emitting a small burst of client errors; adding `signal.NotifyContext` + `srv.Shutdown` with a 25-second drain (grace period was 30s) took that to zero. None of it touched business logic — it was all the standard production hardening that the default server leaves to you. That is the lesson of this chapter in one paragraph: **`net/http` gives you a *correct* server; you are responsible for making it a *safe* one.**

## Chapter Summary

- A server is a loop: **accept → read → work → write → repeat.** Each step has a production failure mode; hardening is closing each one.
- Under the hood: TCP handshake → **kernel accept queue (backlog)** → `Accept()` → a **file descriptor** → **one goroutine per connection**. Connections cost file descriptors, and they're finite.
- The naive `http.ListenAndServe` has **no timeouts** — slow-loris bait and fd-exhaustion bait. Never ship it.
- Configure the **five timeouts/limits**, each mapped to a specific attack or failure.
- **Graceful shutdown** (`signal.NotifyContext` + `srv.Shutdown`, drain < grace period, treat `ErrServerClosed` as success) is what makes deploys drop zero requests.
- Build cross-cutting concerns **once** as composable middleware: **Recover, RequestID, Logging (with a `ResponseWriter` wrapper), Timeout**, and limits.
- Separate **liveness** (cheap, local) from **readiness** (checks dependencies) — conflating them causes outages.
- Bound **bodies** and **concurrency**; shed overload with `503` instead of collapsing.

## Chapter 2 Quiz

**Q1.** Why is `http.ListenAndServe(":8080", nil)` dangerous in production?

**Q2.** During a deploy, Kubernetes sends `SIGTERM`. What must your `Shutdown` timeout be, relative to `terminationGracePeriodSeconds`, and why?

**Q3.** Your liveness probe (`/healthz`) checks the database. The DB has a 30-second blip. What happens to your pods, and why is it bad?

**Q4.** A handler does `go sendMetrics()`, and `sendMetrics` panics. Does your `Recover` middleware catch it?

**Q5.** What is `http.MaxBytesReader` for, and what status should you return when it triggers?

### Answers

> **Try the questions first** — the answers are below.

- **A1.** It sets all timeouts to zero (infinite), so a slow client can hold a connection and file descriptor open forever (slow loris), leading to fd exhaustion. It also ignores the returned error.
- **A2.** **Shorter** than the grace period. If your drain takes longer than `terminationGracePeriodSeconds`, Kubernetes `SIGKILL`s the process mid-drain, severing the very requests you were trying to save.
- **A3.** Kubernetes sees the liveness probe fail and **restarts all the pods** — turning a brief, recoverable DB blip into a full restart storm/outage. Liveness must be cheap and local; dependency checks belong in **readiness**.
- **A4.** **No.** `recover` only catches panics on the *same goroutine*. A panic in a spawned goroutine crashes the whole process. Give every spawned goroutine its own recover, or don't spawn unmanaged ones.
- **A5.** It caps the size of the request body so a huge upload can't exhaust memory; return **`413 Request Entity Too Large`** when it triggers.

## Exercises

1. Take the naive server and, using `curl --limit-rate` or a tiny script that sends one header byte per second, hold a connection open. Confirm the naive server waits forever; add `ReadHeaderTimeout` and confirm it drops the connection after the timeout.
2. Implement the full `run()` with graceful shutdown. Start a handler that sleeps 5 seconds, fire a request, then send `SIGTERM` mid-request. Confirm the in-flight request **completes** while new connections are refused.
3. Write the `statusRecorder` + `Logging` middleware and produce access logs with status, byte count, duration, and request ID. Then break streaming by wrapping a handler that uses `http.Flusher` and observe the failure — then fix it by forwarding the `Flusher`.
4. Add `LimitConcurrency(10)` in front of a slow handler. Fire 100 concurrent requests and observe that at most 10 run at once and the rest either wait or shed with `503`. Tune the number and watch the behavior change.
5. Set your process `ulimit -n` low (e.g. 64), then open many idle connections and watch `Accept` start failing. Raise the limit and confirm the difference.

---

Next chapter → [Database Connections & Pooling](/backend-guide/go/06-production-services/02-database-connections-and-pooling)

Back to → [The Go Engineering Handbook](/backend-guide/go/README)
