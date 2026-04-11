# The Client Presses Enter — What Happens in Silicon
The user hits Enter. Somewhere in the browser, a JavaScript event fires. But before we even get to the browser, let's talk about what "pressing Enter" actually is.
A key press closes a physical switch. That switch connects a circuit. The change in voltage — from ~0V (logic 0) to ~3.3V (logic 1) — propagates through the keyboard's controller chip. That chip is running firmware — a tiny program — that debounces the switch (because physical switches bounce, making multiple electrical contacts in microseconds that must be collapsed into a single logical keypress), encodes the key as a HID (Human Interface Device) scancode, and transmits it over USB.
USB transmits data differentially — two wires, D+ and D−, carrying opposite voltages, so electromagnetic interference cancels out. The host controller in the CPU receives the USB interrupt. This is a hardware interrupt — the USB controller raises an IRQ line, which signals the CPU's interrupt controller (the APIC — Advanced Programmable Interrupt Controller), which interrupts whatever the CPU is currently executing, saves the program counter and registers to the stack, and jumps to the interrupt service routine (ISR) registered for USB.
The ISR wakes the HID driver. The HID driver decodes the scancode into a keycode. The keycode travels up through the input subsystem, the display server (X11 or Wayland on Linux, Quartz on macOS), and eventually reaches the browser's event loop as a keydown event.
This entire chain — from finger contact to browser event — takes roughly 1–10ms depending on the keyboard's polling rate (most keyboards poll at 125Hz, meaning 8ms latency; gaming keyboards at 1000Hz, meaning 1ms).
We haven't sent a single network packet yet.

## DNS Resolution
The browser has a URL: https://api.example.com/users/123. Before it can open a TCP connection, it needs an IP address. It needs DNS.
### The DNS cache hierarchy:
First the browser checks its own DNS cache. Chrome maintains an internal DNS cache with a TTL it chooses (not always what the authoritative server specified — Chrome caps TTLs at 60 seconds for its own reasons). Cache hit → skip all of what follows.
Cache miss → the browser asks the operating system. The OS resolver checks /etc/hosts first (on Linux/macOS). Then it checks its own resolver cache. On Linux this is handled by nscd or systemd-resolved. Cache hit → return.
Cache miss → the OS resolver opens a UDP socket and sends a DNS query to the configured nameserver — typically your router, or a public resolver like 8.8.8.8.
### What a DNS query actually is at the wire level:
A DNS query is a UDP datagram. UDP because DNS queries are small (under 512 bytes historically, up to 4096 with EDNS0), and the overhead of TCP's three-way handshake is disproportionate for a single question-answer exchange.
The DNS message format (defined in RFC 1035) is a binary structure. Not JSON. Not XML. Raw bytes with specific bit-level meanings:
```text
Bytes 0-1:   Transaction ID (16 bits) — random, to match responses to queries
Bytes 2-3:   Flags (16 bits) — QR (query/response), opcode, AA, TC, RD, RA, Z, RCODE
Bytes 4-5:   QDCOUNT — number of questions
Bytes 6-7:   ANCOUNT — number of answers
Bytes 8-9:   NSCOUNT — number of authority records
Bytes 10-11: ARCOUNT — number of additional records
Bytes 12+:   Question section
```
The question section encodes the domain name in label format: each component of the domain is preceded by its length as a single byte. api.example.com becomes: \x03api\x07example\x03com\x00. The null byte terminates the name. Then 2 bytes for query type (A = 1, AAAA = 28, CNAME = 5...) and 2 bytes for query class (IN = 1 for Internet).
This entire structure fits in roughly 30 bytes. It travels as a UDP payload encapsulated in an IP packet. The IP packet has a 20-byte header (source IP, destination IP, TTL, protocol=UDP, checksum...). The UDP header adds 8 bytes (source port, destination port, length, checksum). The Ethernet frame wraps the IP packet with a 14-byte header (destination MAC, source MAC, EtherType) and a 4-byte CRC trailer.
So your "what is the IP address of api.example.com" question travels as roughly 76 bytes of electrical signals on a wire (or radio waves over WiFi, or light pulses in fiber).
### The recursive resolver:
Your local nameserver is probably not authoritative for api.example.com. It's a recursive resolver. It has to ask. The query travels the DNS hierarchy:

- Query the root nameservers (13 of them, operated by IANA, Verisign, etc., distributed globally via anycast). Ask: "who is authoritative for .com?"
- Get a referral to the .com TLD nameservers (operated by Verisign).
- Ask the .com TLD servers: "who is authoritative for example.com?"
- Get a referral to example.com's nameservers (wherever they registered their NS records).
- Ask those nameservers: "what is the A record for api.example.com?"
- Get the answer: 203.0.113.42 TTL 300.

This entire recursive chain, in the best case (warm caches at the recursive resolver), takes 1–50ms. Cold (all caches empty) it can take 100–300ms.
DNS over UDP has no encryption, no authentication, no integrity guarantees. DNSSEC adds cryptographic signatures to records. DNS over HTTPS (DoH) and DNS over TLS (DoT) encrypt the entire exchange. These add latency and computational overhead that matters at scale.

## The Network Interface
Now we have an IP address. The browser's networking stack initiates a TCP connection. But how does a software call become actual electricity?
### The call chain, from top to bottom:
- Browser → OS kernel: The browser calls `connect(sockfd, &server_addr, sizeof(server_addr))` — a syscall. The CPU executes a `syscall` instruction (x86-64) or `svc #0` (ARM). This instruction atomically switches the CPU from user mode (ring 3) to kernel mode (ring 0), saves user-space registers, and jumps to the kernel's syscall handler via the syscall dispatch table.
- Kernel TCP stack: The kernel's TCP implementation (`net/ipv4/tcp.c` in the Linux kernel) creates a TCP socket buffer. It constructs a SYN segment: TCP header (20+ bytes) with the SYN flag set, a random Initial Sequence Number (ISN), window size, and options (MSS, SACK, timestamps, window scaling).
The ISN is not truly random in modern kernels — it's a hash of source IP, source port, destination IP, destination port, and a secret key that rotates over time. This prevents sequence number prediction attacks (a class of TCP hijacking attacks that were common in the 90s).
- IP layer: The TCP segment becomes the payload of an IP packet. The kernel's routing table (`ip route show` in Linux) is consulted. What's the next hop for 203.0.113.42? If it's not on a directly connected subnet, it's the default gateway — your router. The kernel needs the router's MAC address. ARP (Address Resolution Protocol) resolves the router's IP to its MAC address, using its cache or sending an ARP broadcast if needed.
- Netfilter/iptables: On Linux, the packet passes through netfilter hooks — this is where firewalls, NAT, and connection tracking happen. `iptables` rules are evaluated. `conntrack` records this connection in the connection tracking table so that response packets can be matched to this flow. This is not free — the conntrack table has a maximum size, and exhausting it (under a SYN flood, for instance) causes packet drops.
- Network driver: The IP packet is handed to the network interface card (NIC) driver. On modern systems, this is DMA (Direct Memory Access) — the driver writes the packet to a ring buffer in kernel memory, and the NIC's DMA engine reads it from there without involving the CPU. The CPU just updates a tail pointer in the ring buffer to tell the NIC "there's new work here."
- The NIC: The NIC reads the packet, appends the Ethernet frame header (with the router's MAC address as destination), computes the Ethernet CRC (a hardware operation), and transmits the bytes onto the physical medium.
### The physical medium:
- On Ethernet: the bits are encoded using line coding (e.g., 4B/5B or 8B/10B encoding, which trades bandwidth efficiency for DC balance and clock recovery). For 1Gbps Ethernet, bits are transmitted at 1 billion bits per second. A 1500-byte frame takes 12 microseconds to transmit.
- On WiFi: the story is far more complex. WiFi uses CSMA/CA (Carrier Sense Multiple Access with Collision Avoidance) — the radio listens to see if the medium is busy before transmitting, and backs off with exponential random delay if it is. The radio signal is modulated using OFDM (Orthogonal Frequency Division Multiplexing) — the data is spread across multiple carrier frequencies simultaneously. The WiFi radio hardware performs FFT (Fast Fourier Transform) computations in dedicated silicon to encode and decode these signals. A 2.4GHz WiFi signal oscillates at 2.4 billion cycles per second. The wavelength is ~12cm. The antenna is physically sized as a fraction of this wavelength.
- On fiber: photons. A laser diode at the transmitter modulates on/off at the data rate. The light travels through the glass fiber core by total internal reflection — it bounces off the boundary between the core (higher refractive index) and the cladding (lower refractive index), unable to escape. Single-mode fiber (core diameter ~9 micrometers — we are now at the 9-micrometer scale) carries light from a laser with very narrow linewidth, allowing transmission over hundreds of kilometers without regeneration. Multi-mode fiber (50 or 62.5 micrometers) uses LEDs or vertical-cavity surface-emitting lasers (VCSELs) and is limited to ~300m for 10Gbps. The speed of light in fiber is approximately 2×10⁸ m/s (slower than in vacuum due to the refractive index of glass, which is ~1.5). This is why your latency to a server 10,000km away is bounded by physics at ~50ms minimum — light can't travel faster, regardless of how good your infrastructure is.

## Inside the Router — Switching Fabric and Silicon
Your SYN packet arrives at your router. At the router, the Ethernet frame is received, CRC-checked (corrupted packets are silently dropped — there is no error message, the sender must detect packet loss via timeout), the Ethernet header is stripped, and the IP packet is examined.
The router performs a longest prefix match in its routing table. This is not a linear search — routing tables can have hundreds of thousands of entries. Hardware routers use TCAM (Ternary Content-Addressable Memory) — a specialized memory type that can match a value against all entries in the table simultaneously, in a single memory cycle. TCAM stores three states per bit: 0, 1, and "don't care" (for wildcard matching in subnet masks). A TCAM lookup for routing takes ~1 nanosecond regardless of table size. This is why hardware routers can forward packets at line rate (10Gbps, 100Gbps) — the forwarding decision happens in silicon, not in software.
The packet's TTL is decremented. If TTL reaches 0, the packet is dropped and an ICMP "Time Exceeded" message is sent back — this is the mechanism that traceroute exploits. The IP checksum is recomputed (TTL change invalidates the old checksum). The destination MAC address is updated to the next hop's MAC. The packet enters the switching fabric of the router and exits on the correct interface.
This entire process — ingress, lookup, forwarding — takes nanoseconds in a hardware router. Software routers (like Linux doing IP forwarding) take microseconds.
Your packet traverses multiple routers. Each hop adds latency. Intercontinental traffic travels through submarine cables — fiber bundles on the ocean floor, in some cases the same physical routes as 19th-century telegraph cables, though the technology is unrecognizable. A trans-Atlantic cable carries hundreds of terabits per second through wavelength-division multiplexing (WDM) — multiple laser wavelengths, each carrying independent data, all traveling through the same fiber simultaneously.

## The Server's NIC — Interrupt Coalescing and the Ring Buffer
The SYN packet arrives at the server's NIC. At 10Gbps, a minimum-size Ethernet frame (64 bytes) arrives every 51.2 nanoseconds. If the NIC raised a hardware interrupt for every packet, the CPU would spend all its time handling interrupts and no time processing packets. This is called interrupt livelock — a real failure mode that brought down early high-throughput servers.
Modern NICs use interrupt coalescing: the NIC batches multiple packets before raising a single interrupt. The NIC's onboard DMA engine writes packets into a ring buffer in host memory (pinned, non-swappable, physically contiguous). The ring buffer is a circular array of descriptors, each pointing to a memory region where a packet can be written. The NIC writes packets into these memory regions via DMA — without CPU involvement — and updates the ring buffer's head pointer.
When the coalescing timer fires (typically every 50–200 microseconds), the NIC raises a single interrupt. The CPU's APIC receives it and dispatches it to a CPU core (RSS — Receive Side Scaling — distributes interrupts across multiple cores using a hash of the packet's 5-tuple, so packets from the same flow always go to the same core, preserving ordering). The interrupt handler runs. It schedules a softirq (software interrupt) — the NET_RX_SOFTIRQ — to process the received packets.
The softirq runs NAPI (New API), the Linux kernel's interrupt mitigation mechanism. NAPI switches the NIC to polling mode for the duration of packet processing: instead of waiting for more hardware interrupts, the kernel polls the ring buffer directly, processing packets in batches. This is the same insight as epoll for application-level I/O — polling is more efficient than interrupts when the workload is high enough.

## Inside the Kernel TCP Stack — State Machines and Memory
The kernel's TCP/IP stack processes the SYN packet.
### The TCP state machine:
TCP connections progress through states:
`CLOSED -> LISTEN -> SYN_RECEIVED -> ESTABLISHED -> FIN_WAIT_1 -> FIN_WAIT_2 -> TIME_WAIT -> CLOSED`
Each state transition is triggered by an event (packet received, timer fired, application call). The state machine is defined in `RFC 793`, and the Linux kernel implementation lives across dozens of files in `net/ipv4/`.
When the SYN arrives at a `LISTEN` socket, the kernel creates a request socket — a lightweight half-open connection object — and places it in the SYN queue (also called the incomplete connection queue). The kernel sends a `SYN-ACK`. If the client's `ACK` arrives, the request socket is promoted to a full `struct sock` — a socket object — and moved to the accept queue (the complete connection queue). The kernel's `inet_csk_accept()` is called when the application calls `accept()`, dequeuing the connection.
SYN cookies (enabled by default on Linux): If the SYN queue fills up (under a SYN flood attack), the kernel stops allocating request sockets and instead encodes the connection parameters into the ISN of the SYN-ACK, using a cryptographic hash of the 4-tuple and a timestamp. If the client's ACK arrives with the right acknowledgment number, the kernel can reconstruct the connection state without ever having stored it. This makes SYN flood attacks nearly harmless at the cost of losing some TCP options (SACK, window scaling) that were negotiated in the SYN.
### Kernel memory allocation:
Every `struct sock` is allocated from kernel memory using the SLAB allocator (or its successor SLUB). The SLAB allocator maintains per-CPU caches of pre-allocated objects of common sizes, avoiding expensive general-purpose `kmalloc()` calls for frequently created/destroyed objects. Each `struct sock` on Linux is roughly 1KB of kernel memory. 100,000 concurrent connections means ~100MB of kernel memory for socket objects alone, before counting the socket buffers.
Each socket has a send buffer and a receive buffer — regions of kernel memory that hold data in flight. The default size on Linux is 87KB for receive and 16KB for send. `net.core.rmem_max` and `net.core.wmem_max` control the maximum. TCP's flow control (the window size field in the header) is determined by how much space is available in the receive buffer. If your application is slow to read from the socket, the receive buffer fills, the window shrinks to zero, and the sender is forced to stop sending. This is TCP backpressure — and it propagates all the way up through your application's call stack.

## The TLS Handshake — Mathematics in Microseconds
Before HTTP/1.1 or HTTP/2 data flows, TLS must be established. Let's go into what TLS 1.3 actually does, at the cryptographic level.
### The TLS 1.3 handshake (one round trip):
- Client Hello: The client sends its TLS version, a random nonce (32 bytes of cryptographically secure randomness), its list of supported cipher suites, and — critically — key shares. In TLS 1.3, the client preemptively generates a key pair for the key exchange algorithms it thinks the server will support (typically X25519 — Elliptic Curve Diffie-Hellman on Curve25519), and sends its public key in the ClientHello. This is why TLS 1.3 can complete the handshake in one round trip — the client doesn't wait to see which algorithm the server picks before generating its key material.
- Server Hello: The server selects a cipher suite (e.g., TLS_AES_256_GCM_SHA384), sends its own key share (its public key for X25519), and computes the shared secret.
### The key exchange — Elliptic Curve Diffie-Hellman:
Curve25519 is an elliptic curve defined over the prime field GF(2²⁵⁵ - 19). Points on this curve satisfy the equation y² = x³ + 486662x² + x. The key exchange works as follows:

- Client generates a random 256-bit private key a and computes A = a × G (G is the curve's base point, × is elliptic curve point multiplication)
- Server generates a random 256-bit private key b and computes B = b × G
- Client computes S = a × B = a × b × G
- Server computes S = b × A = b × a × G
- Both arrive at the same shared secret S

An eavesdropper who sees A and B cannot compute S without solving the elliptic curve discrete logarithm problem — believed to be computationally infeasible. The best known algorithms require time exponential in the key size. Breaking Curve25519 would require roughly 2¹²⁸ operations — more than the number of atoms in the observable universe raised to some power.
Elliptic curve point multiplication — the a × G operation — involves hundreds of point additions and doublings on 256-bit integers. On modern hardware with dedicated integer multiplication units, this takes roughly 100–200 microseconds. Hardware acceleration (Intel's ADX instruction set extension for multi-precision arithmetic) reduces this further.
### Key derivation:
The shared secret S is fed into HKDF (HMAC-based Key Derivation Function) to derive multiple keys: one for encrypting client→server traffic, one for server→client traffic, one for the handshake, one for the finished message MAC. HKDF uses HMAC-SHA256 or HMAC-SHA384 underneath — iterative SHA2 hash computations.
SHA-256 processes data in 512-bit (64-byte) blocks. Each block requires 64 rounds of operations involving 32-bit bitwise operations, additions, and table lookups. On a modern CPU, a SHA-256 computation of a small message takes ~300ns. Intel's SHA-NI instruction set extension (available since Goldmont microarchitecture) provides hardware-accelerated SHA-256, running at roughly 4 cycles per byte — meaning SHA-256 of a 64-byte block takes ~16 cycles, or ~6ns at 3GHz.
### AEAD encryption — AES-256-GCM:
Application data is encrypted using AES-256-GCM — an Authenticated Encryption with Associated Data (AEAD) scheme. AES (Advanced Encryption Standard) is a substitution-permutation network that transforms 128-bit plaintext blocks into 128-bit ciphertext blocks using a 256-bit key through 14 rounds of operations (SubBytes, ShiftRows, MixColumns, AddRoundKey).
Modern CPUs implement AES in hardware via the AES-NI instruction set (Intel since Westmere, 2010). The AESENC instruction performs one round of AES in ~1 CPU cycle. AES-128-GCM throughput on a modern CPU with AES-NI is roughly 4 GB/s per core. For a 10KB HTTP response body, AES encryption takes roughly 2.5 microseconds. GCM (Galois/Counter Mode) adds an authentication tag computed via GHASH — a multiplication in GF(2¹²⁸) — which is accelerated by the PCLMULQDQ instruction (carry-less multiplication).
Without hardware acceleration — on an embedded processor, or in a language that doesn't use hardware acceleration — these same operations would take 10–100× longer. This is why cryptographic library choice matters, and why using openssl (which has extensive hardware acceleration) beats a pure-Python implementation by orders of magnitude.

## The Kernel's accept() → Your Process
The server's application code has called `accept()` on the listening socket. This is a blocking syscall. The process/thread/goroutine is sleeping in the kernel, waiting for a connection.
When the accept queue has an entry (a completed three-way handshake), the kernel:

- Dequeues the struct sock from the accept queue
- Creates a new file descriptor in the calling process's file descriptor table
- Associates the file descriptor with the socket
- Returns the file descriptor to user space

The file descriptor is just an integer — an index into the process's file descriptor table (`struct files_struct`), which points to a `struct file`, which points to the `struct socket`, which contains the `struct sock`. Five pointer dereferences from your `int fd` to the actual kernel socket object.
### The file descriptor limit:
Each process has a limit on open file descriptors. The soft limit (`ulimit -n`) is typically 1024 on older systems, but should be set to 1,000,000 or more for servers handling high concurrency. This limit is enforced by the kernel — `accept()` will return `EMFILE` if you hit it. The system-wide limit is controlled by `/proc/sys/fs/file-max`.

## epoll — The Heart of Scalable I/O
Your server isn't using one thread per connection (that doesn't scale). It's using epoll — the Linux kernel's scalable I/O event notification mechanism.
### How epoll works internally:
`epoll_create1()` creates an epoll instance — a kernel object that contains:

- A red-black tree of monitored file descriptors (for O(log n) insertion and deletion)
- A linked list of ready events (file descriptors that have data available)
- An internal wait queue for processes blocked in `epoll_wait()`

`epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &event)` adds a file descriptor to the epoll instance's red-black tree. The event structure specifies which events to watch (`EPOLLIN`, `EPOLLOUT`, `EPOLLERR`, `EPOLLHUP`) and can carry 64 bits of user data (typically a pointer to a context struct).
When the NIC receives data for a monitored socket, the kernel's networking code calls `sk_data_ready()` on the socket, which calls `sock_def_readable()`, which wakes up the epoll wait queue, which adds the socket to the ready list. This path is executed in softirq context — the same path as NAPI packet processing. The time from packet arriving at the NIC to the event appearing in the epoll ready list is ~1–10 microseconds on a modern server.
`epoll_wait(epfd, events, maxevents, timeout)` returns ready events. If there are none, the process is put to sleep on the epoll wait queue. When events arrive, the process is woken by the kernel scheduler and `epoll_wait()` returns.
### Edge-triggered vs level-triggered:
`EPOLLET` (edge-triggered): the event fires once when the file descriptor transitions from not-ready to ready. If you don't read all available data, the event doesn't fire again until new data arrives. This is more efficient but requires non-blocking I/O and reading in a loop until you get `EAGAIN`.
Without `EPOLLET` (level-triggered): the event fires every time `epoll_wait()` is called, as long as the file descriptor is ready. Simpler programming model, slightly more overhead.

## Reading the HTTP Request — Parse Trees and State Machines
Your application calls `read(fd, buf, len)` — syscall, kernel crossing. The kernel copies bytes from the socket's receive buffer into your user-space buffer. `memcpy()`. Physically, this means reading from one region of DRAM and writing to another — traversing the memory bus, which on a modern server with DDR4 runs at ~50 GB/s bandwidth but has a latency of ~70 nanoseconds per access (DRAM is fast in bulk but slow per access). For a 1500-byte TCP segment, the `memcpy()` takes perhaps 200–400 nanoseconds.
Now you have bytes in user space. HTTP parsing begins.
### HTTP/1.1 parsing:
The HTTP/1.1 request format (`RFC 7230`) is a text protocol. The request line is `METHOD SP Request-URI SP HTTP-Version CRLF`. Headers are `field-name ":" OWS field-value OWS CRLF`. The header section ends with an empty line (`CRLF CRLF`).
A naive parser might use `\r\n` splitting and string matching. A production parser (like `llhttp`, used by Node.js, or Nginx's parser) is a hand-written state machine where each state represents a position in the parse — reading the method, reading the URI, reading a header name, reading a header value, etc. Each byte is processed by a single switch statement or lookup table. No memory allocation, no string copying where avoidable. SIMD (Single Instruction Multiple Data) instructions can process 16 or 32 bytes simultaneously — looking for `\r\n` in 16 bytes at once using SSE2 `_mm_cmpeq_epi8`.
llhttp processes HTTP at roughly 800 MB/s on modern hardware. A 2KB HTTP request (request line + headers) takes ~2.5 microseconds to parse.
Nginx's parser is more aggressive: it uses computed gotos (`goto *table[state]`) instead of switch statements, avoiding branch prediction overhead. On a CPU with a good branch predictor (like modern Intel), the difference is small. On CPUs with simpler branch predictors (embedded, some ARM), it's significant.
### HTTP/2 parsing:
HTTP/2 is a binary framing protocol. No text parsing. Frames have a fixed header (9 bytes: 3-byte length, 1-byte type, 1-byte flags, 4-byte stream ID). HPACK header compression uses a static table (61 common header field entries, like :method: GET, :status: 200) and a dynamic table (recently seen headers), with Huffman coding for string values. Parsing HTTP/2 is faster than HTTP/1.1 for the same logical content — less data on the wire, binary format, no ambiguous whitespace rules.

## Your Framework, Your Code — Cache Lines and Branch Prediction
Your application code runs. Let's talk about what "running code" means at the silicon level, because this is where the nanosecond-level analysis lives.
### The CPU pipeline:
A modern out-of-order superscalar CPU (Intel's Golden Cove, AMD's Zen 4, ARM's Cortex-X3) doesn't execute one instruction at a time. It maintains a pipeline of hundreds of instructions in flight simultaneously. It can execute 4–6 instructions per clock cycle, across multiple execution units (integer ALUs, floating point units, load/store units, branch units). At 4GHz, that's potentially 16–24 billion operations per second.
But only if the pipeline doesn't stall. Pipeline stalls come from:

- Cache misses — the data you need isn't in the CPU cache, so you have to wait for DRAM
- Branch mispredictions — the CPU guessed which branch would be taken, started executing down that path, and was wrong; all that speculative work must be discarded (a 15–20 cycle penalty per misprediction)
- Data dependencies — instruction B needs the result of instruction A, so B cannot start until A completes

### CPU caches:
Modern CPUs have a cache hierarchy:

- L1 cache: 32–64KB, 4 cycle access latency (~1ns)
- L2 cache: 256KB–1MB, 12 cycle latency (~3ns)
- L3 cache: 8–64MB (shared across cores), 40–60 cycle latency (~15ns)
- DRAM: gigabytes, 200–300 cycle latency (~70ns)

Caches operate on cache lines — 64 bytes on x86, 64 bytes on ARM. When you read a single byte from DRAM, the CPU fetches the entire 64-byte cache line containing that byte. This is why sequential memory access (array traversal) is fast — each cache line fetch gives you 64 bytes of data, and you use all of them. Random memory access (pointer chasing through a linked list) is slow — each pointer dereference is a potential cache miss, fetching a cache line for just 8 bytes (the pointer), then following the pointer to another random location, another cache miss.
This is why hash maps are slower than arrays despite `O(1)` lookup. It's why `struct` layout matters in hot code paths. It's why Rust's emphasis on data-oriented design produces faster code than object-oriented design with virtual dispatch.
### False sharing:
If two CPU cores both cache the same cache line, and both modify it, the cache coherence protocol (MESI — Modified, Exclusive, Shared, Invalid) forces them to serialize. Core A invalidates Core B's copy when it writes, Core B has to refetch. Two variables that are logically independent but happen to sit in the same 64-byte cache line will cause cores to fight over it. This is false sharing, and it can reduce multi-core performance to worse than single-core. The fix is padding structs to cache line boundaries — `__attribute__((aligned(64)))` in C, or `#[repr(align(64))]` in Rust.
### Speculative execution:
The CPU doesn't wait to know which branch will be taken. It predicts (using a Branch Target Buffer, Branch History Table, and Return Address Stack — sophisticated pattern-matching hardware) and starts executing speculatively. If correct (modern predictors achieve >99% accuracy on regular code), the results are committed. If wrong, the pipeline is flushed — this is the vulnerability that Spectre and Meltdown exploited. Speculative execution could read data from kernel memory into CPU registers, and even though those registers were flushed on misprediction, the data left traces in the cache that could be measured via timing. The fixes (kernel page-table isolation, retpoline) add overhead that affected system call performance by 5–30% on workloads with many syscalls.

## The Database Call — Crossing the Network Again
Your handler needs data from the database. You call a query. What happens?
The database driver takes your query string (or prepared statement parameters), serializes them into the database wire protocol (PostgreSQL uses the "extended query" protocol — a binary protocol with message types for Parse, Bind, Execute, Describe, Sync). The serialized bytes go into the socket's send buffer. The kernel sends them.
Network traversal again — this time to the database server, which is (hopefully) on the same local network, meaning ~0.1–0.5ms round-trip time. If it's on the same physical host (Unix domain socket instead of TCP), the latency is ~20 microseconds — data is copied directly between kernel buffers without leaving the machine.
The database receives the query. PostgreSQL parses it into an abstract syntax tree (AST), runs it through the query planner (which uses statistics about table sizes, column cardinality, and index availability to estimate costs and choose a query plan), generates executable code for the plan (PostgreSQL's executor is a tree of nodes — SeqScan, IndexScan, HashJoin, Sort, etc.), and executes it.
### The query planner — cost estimation:
The planner knows that a sequential scan of a 10-million-row table costs approximately seq_page_cost * pages. seq_page_cost is 1.0 by default (an arbitrary unit). If the table has 100,000 8KB pages and they're all in the shared buffer pool (PostgreSQL's in-process cache), the cost is 100,000 — fast. If they have to come from disk, the actual cost is 100,000 pages * 8KB/page / disk_throughput. On NVMe SSD at 5GB/s, that's 160ms. On spinning HDD at 100MB/s, that's 8 seconds. This is why "the same query" that ran fine in development (warm cache, small dataset) can grind to a halt in production (cold cache, real dataset).
An index scan has a different cost model: random_page_cost * index_pages + cpu_tuple_cost * rows. Random I/O on SSDs costs much less relative to sequential I/O than it did on spinning disks — random_page_cost should be set to 1.1–2.0 on SSD systems, vs the default of 4.0 designed for spinning disks. Misconfiguring this causes the planner to prefer sequential scans over index scans when indexes would be faster.
### The B-tree index:
If an index is used, PostgreSQL traverses a B-tree. A B-tree of 10 million rows with a branching factor of ~400 has height ~3. Three I/Os (each an 8KB page read) to find the target row, plus one more to read the heap page containing the actual row. If all four pages are in the buffer pool: ~4 cache misses in DRAM, ~280ns total. If none are cached: 4 disk reads, ~100–200 microseconds on NVMe.

## Serialization — Turning Data Structures into Wire Bytes
Your handler has the data. It needs to serialize it to JSON (or protobuf, or MessagePack). Let's look at JSON.
JSON serialization walks your data structure and emits bytes. For a `struct {name: "Alice", age: 30}`, it emits: `{"name":"Alice","age":30}`. Every field name is a string. Every string requires: `"`, the UTF-8 bytes of the content (with special characters escaped), `"`. Every integer is converted to decimal ASCII digits.
Integer-to-string conversion: the number 123456789 requires 9 decimal digits. The standard algorithm repeatedly divides by 10 and extracts digits, requiring 9 divisions. A division instruction on x86 takes 20–100 cycles (it's one of the most expensive arithmetic operations). Branchless SIMD-based integer-to-string algorithms (used by libraries like itoa) can convert 4 integers in parallel, reducing average cost to ~5 cycles per integer.
UTF-8 validation (ensuring strings are valid UTF-8 before emitting them) can be SIMD-accelerated: checking 16 bytes for ASCII (all bytes < 0x80) with a single SSE2 comparison. Most JSON content in practice is pure ASCII, so this fast path dominates.
Protobuf serialization is faster: varint encoding for integers (no decimal conversion, just VLQ binary encoding), length-prefixed byte strings (no escaping), no field names on the wire (only field numbers). A protobuf message that would be 200 bytes as JSON might be 50 bytes as protobuf, and serialized 3–5× faster.

## Writing the Response — The Last Mile
Your application calls `write()` with the serialized response. Syscall. Kernel crossing. The kernel copies the bytes into the socket's send buffer.
The kernel's TCP implementation packetizes the data: it takes bytes from the send buffer and creates TCP segments of up to MSS (Maximum Segment Size) bytes — typically 1460 bytes for Ethernet (1500 byte MTU minus 20 byte IP header minus 20 byte TCP header). If `TCP_NODELAY` is set, segments are sent immediately. Without `TCP_NODELAY`, Nagle's algorithm coalesces small writes: it won't send a segment smaller than MSS unless there are no outstanding unacknowledged segments. For request-response protocols where the server sends a response only after receiving a complete request, Nagle is harmless. For interactive protocols (SSH, game servers), Nagle adds up to 200ms of latency and must be disabled.
The kernel signs off on sending, the NIC DMA-reads the segment, the Ethernet frame is built, the bits go onto the wire, photons travel through fiber, the client's NIC receives them, interrupt, softirq, TCP ACK, data delivered to the browser.
The browser receives the bytes, feeds them through its HTTP parser, through its HTML parser (if HTML), builds the DOM, fires layout, paints pixels, and activates GPU shaders to composite the result to the screen.
### The screen refresh:
The monitor refreshes at 60Hz (16.67ms per frame) or 144Hz (6.94ms). The GPU renders the frame into a framebuffer. The display controller reads the framebuffer via DMA and sends pixels to the monitor over HDMI/DisplayPort — a digital signal at multi-gigabit rates. Inside the monitor, the signal drives the backlight (LCD) or the organic compounds (OLED) or the micro-LEDs, emitting photons at specific wavelengths that hit your retina.
Your retina's cone cells respond to photons in roughly 10 milliseconds. The signal travels through the optic nerve to the visual cortex. Your brain processes it. Somewhere around 150–200ms after the first photon, you consciously perceive the response.