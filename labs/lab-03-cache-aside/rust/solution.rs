// Lab 03: Cache-Aside with Postgres + Redis — Rust reference solution.
//
// labkit gives you ready db() and cache() handles — no setup.
//   Run:  cargo run --bin solution
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::thread;

use serde_json::json;

const TTL: u64 = 60;

#[derive(Clone)]
struct User {
    id: i32,
    name: String,
    email: String,
    plan: String,
}

fn user_key(id: i32) -> String {
    format!("user:{id}")
}

fn user_from_row(row: &labkit::Row) -> User {
    User { id: row.get("id"), name: row.get("name"), email: row.get("email"), plan: row.get("plan") }
}

fn user_to_json(u: &User) -> String {
    json!({"id": u.id, "name": u.name, "email": u.email, "plan": u.plan}).to_string()
}

fn user_from_json(s: &str) -> User {
    let v: serde_json::Value = serde_json::from_str(s).unwrap();
    User {
        id: v["id"].as_i64().unwrap() as i32,
        name: v["name"].as_str().unwrap().to_string(),
        email: v["email"].as_str().unwrap().to_string(),
        plan: v["plan"].as_str().unwrap().to_string(),
    }
}

// Part 1 — cache-aside read.
fn get_user_profile(id: i32) -> Option<User> {
    let key = user_key(id);
    if let Some(s) = labkit::cache().get(&key) {
        return Some(user_from_json(&s));
    }
    let row = labkit::db().query_one("SELECT id, name, email, plan FROM users WHERE id = $1", &[&id])?;
    let u = user_from_row(&row);
    labkit::cache().set(&key, &user_to_json(&u), Some(TTL));
    Some(u)
}

fn update_user_plan(id: i32, plan: &str) {
    labkit::db().exec("UPDATE users SET plan = $1, updated_at = now() WHERE id = $2", &[&plan, &id]);
    labkit::cache().del(&user_key(id));
}

// Part 2 — single-flight (per-key lock + double-check).
fn locks() -> &'static Mutex<HashMap<String, Arc<Mutex<()>>>> {
    static L: OnceLock<Mutex<HashMap<String, Arc<Mutex<()>>>>> = OnceLock::new();
    L.get_or_init(|| Mutex::new(HashMap::new()))
}

fn key_lock(key: &str) -> Arc<Mutex<()>> {
    let mut m = locks().lock().unwrap();
    m.entry(key.to_string()).or_insert_with(|| Arc::new(Mutex::new(()))).clone()
}

fn get_user_profile_singleflight(id: i32) -> Option<User> {
    let key = user_key(id);
    if let Some(s) = labkit::cache().get(&key) {
        return Some(user_from_json(&s));
    }
    let lock = key_lock(&key);
    let _guard = lock.lock().unwrap();
    if let Some(s) = labkit::cache().get(&key) {
        return Some(user_from_json(&s)); // double-check
    }
    let row = labkit::db().query_one("SELECT id, name, email, plan FROM users WHERE id = $1", &[&id])?;
    let u = user_from_row(&row);
    labkit::cache().set(&key, &user_to_json(&u), Some(TTL));
    Some(u)
}

fn assert(cond: bool, msg: &str) {
    if !cond {
        panic!("CHECK FAILED: {msg}");
    }
}

fn main() {
    labkit::db().exec("UPDATE users SET plan = 'pro' WHERE id = 1", &[]);
    labkit::cache().flush();
    labkit::db().reset_counters();

    let u = get_user_profile(1);
    assert(u.as_ref().map(|x| x.id) == Some(1), "user 1 should be fetched");
    assert(labkit::db().query_count() >= 1, "first read should hit Postgres");
    assert(labkit::cache().exists(&user_key(1)), "first read should populate the cache");

    labkit::db().reset_counters();
    let u2 = get_user_profile(1).unwrap();
    assert(u2.id == 1, "second read returns the user");
    assert(labkit::db().query_count() == 0, "a cache hit must not query Postgres");

    update_user_plan(1, "enterprise");
    assert(!labkit::cache().exists(&user_key(1)), "update must invalidate the cache");
    assert(get_user_profile(1).unwrap().plan == "enterprise", "refilled with the new plan");

    assert(get_user_profile(999_999).is_none(), "missing user returns None");

    labkit::cache().flush();
    labkit::db().reset_counters();
    let handles: Vec<_> = (0..50).map(|_| thread::spawn(|| get_user_profile_singleflight(1))).collect();
    for h in handles {
        h.join().unwrap();
    }
    assert(labkit::db().query_count() == 1, "single-flight: only one DB query for 50 concurrent reads");

    println!("OK — cache-aside, invalidation, and single-flight (1 DB query for 50 concurrent reads)");
}
