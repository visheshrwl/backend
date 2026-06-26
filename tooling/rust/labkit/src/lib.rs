//! labkit — zero-setup platform layer for the Rust labs.
//!
//! ```ignore
//! let rows = labkit::db().query("SELECT 1 AS ok", &[]);
//! ```
//!
//! Connections come from DATABASE_URL / REDIS_URL, injected by the lab platform.
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};

use postgres::types::ToSql;
use postgres::{Client, NoTls};
use redis::Commands;

pub use postgres::Row;

pub struct Db {
    client: Mutex<Client>,
    query_count: AtomicU64,
}

impl Db {
    fn new() -> Db {
        let url = std::env::var("DATABASE_URL")
            .unwrap_or_else(|_| "postgres://labs:labs@localhost:5432/labs".into());
        let client = Client::connect(&url, NoTls).expect("connect postgres");
        Db { client: Mutex::new(client), query_count: AtomicU64::new(0) }
    }

    pub fn query(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> Vec<Row> {
        self.query_count.fetch_add(1, Ordering::SeqCst);
        self.client.lock().unwrap().query(sql, params).expect("query failed")
    }

    pub fn query_one(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> Option<Row> {
        self.query(sql, params).into_iter().next()
    }

    pub fn exec(&self, sql: &str, params: &[&(dyn ToSql + Sync)]) -> u64 {
        self.query_count.fetch_add(1, Ordering::SeqCst);
        self.client.lock().unwrap().execute(sql, params).expect("exec failed")
    }

    pub fn query_count(&self) -> u64 {
        self.query_count.load(Ordering::SeqCst)
    }

    pub fn reset_counters(&self) {
        self.query_count.store(0, Ordering::SeqCst);
    }
}

pub struct Cache {
    conn: Mutex<redis::Connection>,
}

impl Cache {
    fn new() -> Cache {
        let url = std::env::var("REDIS_URL").unwrap_or_else(|_| "redis://localhost:6379/0".into());
        let client = redis::Client::open(url).expect("redis client");
        let conn = client.get_connection().expect("redis connection");
        Cache { conn: Mutex::new(conn) }
    }

    pub fn get(&self, key: &str) -> Option<String> {
        let r: redis::RedisResult<Option<String>> = self.conn.lock().unwrap().get(key);
        r.unwrap_or(None)
    }

    pub fn set(&self, key: &str, val: &str, ttl: Option<u64>) {
        let mut c = self.conn.lock().unwrap();
        match ttl {
            Some(t) => {
                let _: () = c.set_ex(key, val, t).unwrap();
            }
            None => {
                let _: () = c.set(key, val).unwrap();
            }
        }
    }

    pub fn del(&self, key: &str) {
        let _: () = self.conn.lock().unwrap().del(key).unwrap();
    }

    pub fn exists(&self, key: &str) -> bool {
        self.conn.lock().unwrap().exists(key).unwrap_or(false)
    }

    pub fn flush(&self) {
        let mut c = self.conn.lock().unwrap();
        let _: () = redis::cmd("FLUSHDB").query(&mut c).unwrap();
    }
}

static DB_INST: OnceLock<Db> = OnceLock::new();
static CACHE_INST: OnceLock<Cache> = OnceLock::new();

pub fn db() -> &'static Db {
    DB_INST.get_or_init(Db::new)
}

pub fn cache() -> &'static Cache {
    CACHE_INST.get_or_init(Cache::new)
}
