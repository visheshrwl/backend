// Lab 02: Connection Pool Tuning — Rust, YOUR TURN.
//
// Implement Pool::acquire and Pool::release so the pool reuses idle connections,
// times out when exhausted (return Err), and never creates more than max_size.
//
//   Run / validate:  rustc -O stub.rs -o /tmp/lab02_stub && /tmp/lab02_stub
//   Reference:        rustc -O solution.rs -o /tmp/lab02_rs && /tmp/lab02_rs
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

    // TODO:
    //   Lock self.inner. While permits <= 0, wait on self.cond with a timeout
    //   derived from self.timeout; return Err once the deadline passes. Then
    //   take a permit, pop an idle Conn if any, otherwise bump created, capture
    //   the id, drop the lock, sleep(CREATE), and return Ok(Conn { id }).
    fn acquire(&self) -> Result<Conn, &'static str> {
        let _ = (&self.timeout, &self.inner, &self.cond);
        unimplemented!("Implement Pool::acquire")
    }

    // TODO:
    //   Lock self.inner, push conn to idle, give back a permit, notify_one.
    fn release(&self, _conn: Conn) {
        unimplemented!("Implement Pool::release")
    }

    fn created(&self) -> u64 {
        self.inner.lock().unwrap().created
    }
}

fn main() {
    let p = Pool::new(2, Duration::from_secs(5));
    let c1 = p.acquire().unwrap();
    let id1 = c1.id;
    p.release(c1);
    let c2 = p.acquire().unwrap();
    assert_eq!(id1, c2.id, "released connection should be reused");
    p.release(c2);

    let p2 = Pool::new(1, Duration::from_millis(200));
    let held = p2.acquire().unwrap();
    assert!(p2.acquire().is_err(), "second acquire should time out");
    p2.release(held);

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
