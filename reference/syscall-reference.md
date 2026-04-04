# Linux Syscall Reference

## Common Syscalls in Backend Engineering

| Syscall | Cost | Description | When it appears |
|---------|------|-------------|-----------------|
| `read()` | ~100–500ns | Read from fd (returns if data ready, blocks otherwise) | Every network recv, file read |
| `write()` | ~100–500ns | Write to fd | Every network send, log write |
| `accept()` | ~500ns–1μs | Accept new connection from listen socket | New connection per request (no pool) |
| `connect()` | ~500ns + RTT | Initiate TCP connection | New connection creation |
| `epoll_wait()` | ~500ns | Wait for events on registered fds | Event loop at rest |
| `epoll_ctl()` | ~500ns | Add/modify/remove fd from interest list | On every new connection (async server) |
| `socket()` | ~1μs | Create new socket fd | Connection creation |
| `close()` | ~500ns | Close fd + optional TCP teardown | Connection close |
| `fork()` | ~1–5ms | Create new process | PostgreSQL: per connection |
| `mmap()` | ~1μs | Map file/anonymous memory into address space | Buffer pool creation |
| `brk()/sbrk()` | ~500ns | Extend heap | malloc() for large allocations |
| `futex()` | ~50–200ns | Fast user-space mutex (contended case) | Any mutex contention |

## Syscall Overhead with Security Mitigations

Post-Spectre/Meltdown (2018), syscalls are more expensive due to KPTI (Kernel Page Table Isolation):

```
Pre-KPTI syscall:  ~100ns
Post-KPTI syscall: ~200-500ns (TLB flush on every syscall boundary)

At 1M syscalls/second: 200ms CPU overhead from syscalls alone
→ Batching and buffering reduce syscall count
→ io_uring (Linux 5.1+) enables async syscalls with reduced overhead
```

## TCP Socket Tuning Parameters

```bash
# View current settings
sysctl net.ipv4.tcp_keepalive_time       # default: 7200 (2 hours)
sysctl net.ipv4.tcp_fin_timeout          # default: 60 seconds
sysctl net.core.somaxconn                # listen backlog (default: 128)
sysctl net.ipv4.ip_local_port_range      # ephemeral ports (default: 32768-60999)

# Tuning for high-connection-rate servers
sysctl -w net.ipv4.tcp_keepalive_time=300  # detect dead connections faster
sysctl -w net.core.somaxconn=65535         # larger accept queue
sysctl -w net.ipv4.ip_local_port_range="1024 65535"  # more ephemeral ports
```
