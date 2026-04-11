The Request Lifecycle — What Actually Happens
Most engineers think a request lifecycle is: client sends HTTP request → server processes it → response comes back. That's not wrong. It's just the view from 30,000 feet, and at that altitude you can't see anything that actually matters.
Let's descend.

Layer 0: The Network Stack Before Your Code Exists
Before a single byte reaches your application, the operating system has already done an enormous amount of work that most backend engineers treat as magic.
A client wants to talk to your server. It initiates a TCP three-way handshake — SYN, SYN-ACK, ACK. This isn't free. Each step is a round trip, and each round trip is bounded by the speed of light. A client in Mumbai talking to a server in Virginia is paying ~200ms just to establish the connection, before a single byte of HTTP is transmitted. This is why connection pooling exists. This is why HTTP/2 multiplexing exists. This is why gRPC over a persistent connection beats REST over ephemeral ones in high-throughput systems.
After the handshake, if you're on HTTPS (and you should be), there's a TLS handshake on top of that. TLS 1.3 reduced this to one round trip. TLS 1.2 was two. Every version of TLS before 1.2 was a crime against latency.
Now you have an established connection. The OS kernel maintains this in a socket — a file descriptor pointing to a kernel buffer. Your application doesn't read from the network directly. It reads from kernel memory that the network stack has already populated. This distinction matters enormously when we get to concurrency models.

Layer 1: The Kernel's Buffer and the Accept Queue
Your server process called listen() on a port. The kernel is now maintaining two queues for that port:

The SYN queue (incomplete connections — handshake in progress)
The accept queue (completed connections waiting for your application to call accept())

If your application is slow to call accept() — because it's busy, because it's single-threaded, because a garbage collector just stopped the world — the accept queue fills up. New connections get dropped. The client sees a timeout. Your SRE sees a spike in the error dashboard and starts sweating.
This is the first place where your language runtime and concurrency model make a life-or-death difference. A Go server that spawns a goroutine per connection can drain that accept queue aggressively. A naive single-threaded Python server cannot.

Layer 2: Reading the Request — Syscalls and Context Switches
Your application calls read() on the socket file descriptor. This is a system call — a deliberate crossing of the boundary between user space and kernel space. That crossing is not free. It involves saving CPU registers, switching privilege levels, executing kernel code, then restoring state and returning to user space. On modern hardware this costs roughly 100–300 nanoseconds. That sounds trivial until you're doing it a hundred thousand times per second.
The kernel copies data from its receive buffer into your application's memory. Now you have raw bytes. Those bytes are an HTTP request — but your application doesn't know that yet. It just has bytes.
Parsing begins. The HTTP parser reads the request line (GET /api/users HTTP/1.1), then headers, then the body if present. This parsing is surprisingly expensive at scale. Nginx's HTTP parser is a hand-optimized state machine written in C. Most framework-level parsers are far slower. When you use Express.js or FastAPI, you're paying a parsing tax that Nginx doesn't pay.
Headers contain critical information your application will use: Content-Type, Authorization, Content-Length (so you know when to stop reading the body), Transfer-Encoding: chunked (so you know the body arrives in pieces and you need to reassemble it).

Layer 3: Your Framework's Middleware Stack
The raw request hits your framework. In Express, it runs through middleware functions in order. In Go's net/http, it's a chain of Handler implementations. In FastAPI, it's Starlette middleware. In every case, the structure is the same: a series of functions that each get to inspect and potentially modify the request before it reaches your business logic, and inspect and modify the response on the way back out.
What lives in this middleware stack in production?

Authentication — validating a JWT, checking an API key, calling an auth service
Rate limiting — checking a counter in Redis to see if this client has exceeded their quota
Request ID injection — generating a UUID and attaching it to the request so every log line downstream can be correlated
Body parsing — deserializing JSON from bytes into a language-native data structure
Input validation — ensuring the parsed data conforms to your schema before it reaches business logic

Each one of these is a potential failure point. Each one adds latency. The authentication check that calls your auth service adds a network round trip. The rate limiter that hits Redis adds another. A request that looks like it takes 10ms in your business logic might actually take 45ms when you add up every middleware hop.
This is why observability at the middleware layer is non-negotiable in serious production systems. You need to know which layer is eating your latency.

Layer 4: Business Logic, Database Calls, and the I/O Reality
Your handler runs. It probably talks to a database. This is where most backend engineers spend most of their time, and it's where most performance mistakes are made.
A database query over a local network takes roughly 0.5–2ms if the query is simple and the database is healthy. A query that triggers a full table scan on a 50 million row table takes however long it takes — and during that time, your thread or goroutine or event loop iteration is waiting. Not computing. Waiting.
This waiting is the central problem that every concurrency model in existence is trying to solve. We will come back to this with full force when we hit concurrency.
What I want you to notice here is the dependency graph of a typical request:

Call the database → wait
If cache miss, call the database again → wait
Maybe call an external service → wait
Serialize the result → CPU work
Write the response → wait for the kernel to accept the bytes

Most of the wall-clock time in a backend request is waiting. The CPU is idle. The thread is blocked. The event loop is spinning. And every language and runtime has a completely different answer for what to do with that idle time.

Layer 5: Writing the Response
Your handler returns. The framework serializes your data structure into bytes — JSON serialization is not free, and at scale it becomes measurable. The bytes go into a write buffer. Your application calls write() or send() on the socket — another syscall, another kernel crossing. The kernel copies the bytes into its send buffer and handles the actual transmission: TCP segmentation, acknowledgment, retransmission if packets are lost.
You don't control any of that. The kernel does. But you can influence it — TCP_NODELAY disables Nagle's algorithm, which buffers small packets to combine them (great for throughput, terrible for latency-sensitive applications). SO_SNDBUF controls the kernel send buffer size. These socket options are the levers that infrastructure engineers pull when squeezing the last milliseconds out of a system.

Layer 6: Connection Fate — Keep-Alive and What Comes Next
With HTTP/1.1, connections are kept alive by default. The TCP connection persists after the response is sent, waiting for the next request from this client. This is a resource — a file descriptor, kernel buffer space, memory in your application. A server handling 100,000 concurrent keep-alive connections is maintaining 100,000 open sockets. This is why ulimit on Linux file descriptors matters. This is why Nginx can handle more concurrent connections than Apache's old process-per-connection model — it's not holding a thread per connection.
HTTP/2 takes this further: multiple requests travel over a single TCP connection simultaneously via stream multiplexing. The client doesn't wait for response A before sending request B. Head-of-line blocking at the HTTP level is eliminated. (HTTP/3 goes further still, eliminating TCP head-of-line blocking by moving to QUIC over UDP — but that's a separate thesis.)

That's the request lifecycle. Not as a diagram. As a system under stress.
Now let's talk about what to do with all that waiting.

Concurrency Models — The Fundamental Bets
Every concurrency model is an answer to the same question: while we're waiting for I/O, what should the CPU be doing?
The answers fall into a few distinct philosophies, and each major backend language has placed its bet on a different one. Understanding why each bet was made — and what it costs — is what separates engineers who use frameworks from engineers who understand systems.

The Problem Space
A server has N CPU cores and is handling M concurrent requests. If M >> N (which is always true in any real system), you cannot give each request its own CPU core. You have to time-share.
When request A is waiting for a database response, you want the CPU to work on request B. When request B's external API call returns, you want to switch back. The question is: how do you manage this switching, and who pays the cost?
There are three fundamental answers:

OS threads — let the kernel manage the switching
Green threads / coroutines — manage the switching yourself in user space
Event loop — never switch at all; use a single thread and non-blocking I/O

Every language picks one of these, or a hybrid, and then builds its entire concurrency story on top of it.

Model 1: OS Threads (The Naive Approach)
The simplest mental model: one request, one OS thread. When the request blocks on I/O, the OS kernel preemptively context-switches to another thread that's ready to run.
This is how early Java servers worked. This is how Apache's prefork model works. This is how a naive Go server would work if Go didn't have goroutines.
What it costs:
An OS thread on Linux has a default stack size of 8MB. Ten thousand concurrent requests means 80GB of RAM just for stacks. You run out of memory long before you run out of CPU.
A context switch between OS threads involves saving the full CPU register set, switching page tables, flushing CPU caches, and updating kernel data structures. On modern hardware, a context switch costs roughly 1–10 microseconds. At 100,000 context switches per second, that's 100–1000ms of CPU time wasted on switching — not on actual work.
Thread creation is expensive. Thread destruction is expensive. Thread synchronization with mutexes and condition variables is a source of bugs that have destroyed careers.
OS threads work fine when M (concurrent requests) is small — hundreds. They collapse when M reaches tens of thousands. This is the C10K problem that defined server architecture in the early 2000s.

Model 2: The Event Loop — Node.js's Bet
Node.js made a radical choice: one thread, non-blocking everything.
The insight is that most of a request's time is I/O wait — and during I/O wait, a thread is doing nothing useful. So why have multiple threads at all? Instead, have one thread that never blocks. Whenever an I/O operation would block, register a callback and move on. When the I/O completes, the kernel notifies you (via epoll on Linux, kqueue on macOS), and you run the callback.
This is the event loop. It's a while(true) loop that:

Checks for completed I/O events
Runs the callbacks associated with those events
Checks for timers that have expired
Goes back to step 1

What makes this powerful:
No thread switching overhead. No thread stack memory. One process handling 100,000 concurrent connections uses the same amount of CPU for scheduling as one handling 100. The event loop scales to connection count in a way that OS threads fundamentally cannot.
What makes this treacherous:
The event loop is a single thread. If your callback does CPU-intensive work — parsing a large JSON body, running a complex algorithm, doing cryptography — you block the event loop. Every other request in flight stops making progress. The server becomes unresponsive for the duration of your CPU-bound work.
This is not a theoretical concern. It is the most common performance bug in Node.js systems. A single slow synchronous operation — a regex that backtracks exponentially, a JSON.stringify on a deeply nested object, a badly written loop — can take down a Node.js server that handles thousands of requests per second.
The event loop model is the right bet when your workload is I/O bound: lots of waiting, little CPU work. It is the wrong bet when your workload is CPU bound: heavy computation, image processing, machine learning inference.
Node.js partially addresses this with Worker Threads (true OS threads for CPU-bound work) and the cluster module (multiple processes, each with their own event loop). But these are escape hatches from the model, not features of it.
The underlying mechanism:
Node.js uses libuv — a C library that provides a consistent event loop interface across platforms. Under the hood, libuv uses epoll on Linux. epoll is a Linux kernel interface that lets you watch thousands of file descriptors simultaneously and get notified only when one of them is ready for I/O. This is O(1) in the number of watched descriptors — watching 100,000 sockets costs the same as watching 1. Compare this to the older select() and poll() system calls, which were O(N) and couldn't scale past ~1024 file descriptors respectively.

Model 3: Green Threads / Goroutines — Go's Bet
Go looked at the tradeoffs and said: OS threads are too expensive, but the event loop programming model is too hostile to human cognition. Callbacks and promises and async/await are the language trying to paper over the fact that you're writing a state machine by hand.
Go's answer: goroutines — user-space threads managed by the Go runtime, not the OS kernel.
A goroutine starts with a stack of 2KB (not 8MB like an OS thread). The stack grows dynamically as needed. You can have a million goroutines in a process that would OOM with a million OS threads. The Go runtime maintains a scheduler — the M:N scheduler — that maps M goroutines onto N OS threads (where N is typically the number of CPU cores).
When a goroutine blocks on I/O, the Go runtime doesn't block the underlying OS thread. Instead, it parks the goroutine, runs another goroutine on that OS thread, and uses epoll under the hood to know when the I/O completes — at which point the original goroutine is made runnable again and eventually scheduled back onto an OS thread.
What this means for you as a programmer:
You write sequential, synchronous-looking code. No callbacks. No promises. No async/await. You write:
goresp, err := http.Get("https://api.example.com/data")
And the Go runtime handles the fact that this blocks — transparently parking the goroutine and unblocking the OS thread for other work. To you, it looks like a blocking call. To the runtime, it's a non-blocking operation with the goroutine suspended.
This is a profound achievement. You get the scalability of an event loop with the readability of sequential code.
The cost:
Go's scheduler is a preemptive scheduler with cooperative elements. Goroutines are preempted at function call boundaries (and since Go 1.14, asynchronously via signals). The scheduler adds overhead — not as much as OS thread context switches, but non-zero.
Go's garbage collector runs concurrently with your goroutines, but GC still causes stop-the-world pauses (very short ones in modern Go — sub-millisecond — but real). In latency-sensitive systems, these pauses matter. A 500-microsecond GC pause is invisible in a 100ms request but catastrophic in a 1ms trading system.
Go's memory model requires careful attention to data races. Multiple goroutines sharing memory need synchronization. The race detector (go test -race) is your friend and should run in CI always.

Model 4: Async/Await — Python's Bet (asyncio) and Rust's Bet (tokio)
Python and Rust both converged on an explicit async/await model, though for completely different reasons and with completely different guarantees.
Python's asyncio:
Python has the GIL — the Global Interpreter Lock — a mutex that ensures only one thread executes Python bytecode at a time. This makes Python's threading model nearly useless for CPU-bound concurrency (two threads cannot genuinely run Python code in parallel). For I/O-bound concurrency, the GIL is released during I/O operations, so threads can overlap on I/O — but the overhead of OS threads still applies.
asyncio bypasses the GIL problem for I/O concurrency by using a single thread with an event loop, similar to Node.js. async def functions are coroutines — they can suspend themselves at await points and yield control back to the event loop.
pythonasync def fetch_user(user_id):
    user = await db.get(user_id)          # suspend here, event loop runs other coroutines
    profile = await cache.get(user_id)    # suspend here again
    return merge(user, profile)
The await keyword is the explicit marker where a coroutine might be suspended. This is simultaneously the model's strength and its weakness. Strength: you can read the code and know exactly where concurrency can happen. Weakness: if you call a blocking function without await — a synchronous database driver, a blocking HTTP client, a CPU-heavy computation — you block the entire event loop, exactly like Node.js.
Python's async ecosystem is fragmented. Synchronous code and asynchronous code don't compose easily. Libraries written for asyncio don't work with trio. Mixing sync and async code requires bridges (asyncio.run_in_executor runs sync code in a thread pool). This fragmentation is a real tax on Python async codebases.
Rust's tokio:
Rust's async model is architecturally similar to Python's — explicit async/await, cooperative scheduling — but the implementation is radically different and the guarantees are stronger.
In Rust, async fn returns a Future — a value representing a computation that hasn't completed yet. Futures are lazy: they don't run until you await them or hand them to an executor. The executor (tokio is the dominant one) is a work-stealing, multi-threaded runtime that runs futures across all CPU cores simultaneously.
This is the key difference from Python's asyncio: tokio runs on multiple OS threads simultaneously. There's no GIL. A CPU-bound future on one thread doesn't block I/O futures on other threads. You get both I/O concurrency and genuine CPU parallelism.
Rust's ownership system means that data races across async tasks are caught at compile time. You cannot accidentally share mutable state between two tasks without the compiler forcing you to acknowledge it. The entire class of race condition bugs that destroys Node.js and Python codebases simply cannot exist in safe Rust.
The cost: Rust's async model is notoriously complex. Futures are state machines generated by the compiler, and understanding their behavior under composition requires understanding those state machines. Lifetime rules interact with async in ways that produce error messages that can humble experienced engineers. The expressiveness comes at the cost of a very steep learning curve.

The Comparison That Actually Matters
OS ThreadsEvent Loop (Node)Goroutines (Go)Async/Await (Python/Rust)Memory per concurrent task~8MB~KB (just callback state)~2KB growing~KB (future state machine)Context switch costHigh (kernel)Near zeroLow (user space)Near zeroProgramming modelSequentialCallback/Promise hellSequentialExplicit async markersCPU parallelismYesNo (single thread)YesNo (Python) / Yes (Rust)I/O concurrencyYes (expensive)Yes (cheap)Yes (cheap)Yes (cheap)Blocking code penaltyNoneCatastrophicNone (runtime handles)CatastrophicData race safetyLanguage-dependentSingle-threaded (safe by default)Race detector neededRust: compile-time; Python: GIL partial

The Underlying Insight
Every concurrency model is a different trade-off between:

Expressiveness — how easy is it to write correct concurrent code
Efficiency — how much overhead does the model impose
Safety — how hard is it to introduce data races or deadlocks
CPU parallelism — can you use multiple cores simultaneously

No model wins on all four dimensions. Go wins on expressiveness and efficiency at the cost of requiring explicit attention to data races. Rust wins on safety and efficiency at the cost of expressiveness. Node.js wins on simplicity and I/O efficiency at the cost of CPU parallelism and fragility around blocking code. Python's asyncio wins on nothing in particular — it's a compromise that inherits the GIL's limitations while adding async complexity.
Understanding these trade-offs isn't academic. When you're deciding whether to use a goroutine pool or tokio for a service that needs to do image processing on inbound requests, you're deciding whether your CPU-bound work will saturate a single event loop thread or parallelize across cores. That decision changes your P99 latency by an order of magnitude.