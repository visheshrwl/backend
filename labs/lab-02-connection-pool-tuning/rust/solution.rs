// Lab 02: Connection Pool Tuning — Rust reference solution.
//
//   Run:  rustc -O solution.rs -o /tmp/lab02_rs && /tmp/lab02_rs
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const CREATE: Duration = Duration::from_millis(15);
const QUERY: Duration = Duration::from_millis(10);

struct Conn {
    id: u64,
}
impl Conn {
    fn execute(&self) {
        thread::sleep(QUERY);
    }
}

struct Inner {
    permits: i64,
    idle: Vec<Conn>,
    created: u64,
}

// Thread-safe bounded pool. `permits` (guarded by a Mutex + Condvar) caps
// concurrent holders at max_size, so the pool never creates more than max_size.
struct Pool {
    timeout: Duration,
    inner: Mutex<Inner>,
    cond: Condvar,
}

impl Pool {
    fn new(max_size: i64, timeout: Duration) -> Self {
        Pool {
            timeout,
            inner: Mutex::new(Inner { permits: max_size, idle: Vec::new(), created: 0 }),
            cond: Condvar::new(),
        }
    }

    fn acquire(&self) -> Result<Conn, &'static str> {
        let mut guard = self.inner.lock().unwrap();
        let deadline = Instant::now() + self.timeout;
        while guard.permits <= 0 {
            let now = Instant::now();
            if now >= deadline {
                return Err("pool exhausted: acquire timed out");
            }
            let (g, _) = self.cond.wait_timeout(guard, deadline - now).unwrap();
            guard = g;
        }
        guard.permits -= 1;
        if let Some(c) = guard.idle.pop() {
            return Ok(c);
        }
        guard.created += 1;
        let id = guard.created;
        drop(guard); // create outside the lock
        thread::sleep(CREATE);
        Ok(Conn { id })
    }

    fn release(&self, conn: Conn) {
        let mut guard = self.inner.lock().unwrap();
        guard.idle.push(conn);
        guard.permits += 1;
        self.cond.notify_one();
    }

    fn created(&self) -> u64 {
        self.inner.lock().unwrap().created
    }
}

fn main() {
    // 1. reuse
    let p = Pool::new(2, Duration::from_secs(5));
    let c1 = p.acquire().unwrap();
    let id1 = c1.id;
    p.release(c1);
    let c2 = p.acquire().unwrap();
    assert_eq!(id1, c2.id, "released connection should be reused");
    p.release(c2);

    // 2. timeout when exhausted
    let p2 = Pool::new(1, Duration::from_millis(200));
    let held = p2.acquire().unwrap();
    assert!(p2.acquire().is_err(), "second acquire should time out");
    p2.release(held);

    // 3. never exceed max_size under concurrent load
    let p3 = Arc::new(Pool::new(10, Duration::from_secs(30)));
    let ok = Arc::new(Mutex::new(0));
    let mut handles = Vec::new();
    for _ in 0..100 {
        let p3 = Arc::clone(&p3);
        let ok = Arc::clone(&ok);
        handles.push(thread::spawn(move || {
            let c = p3.acquire().unwrap();
            c.execute();
            p3.release(c);
            *ok.lock().unwrap() += 1;
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
    assert_eq!(*ok.lock().unwrap(), 100, "all 100 requests should succeed");
    assert!(p3.created() <= 10, "pool must never create more than max_size connections");

    println!(
        "OK — reuse, timeout, and bound (created={} for 100 requests, max=10)",
        p3.created()
    );
}
