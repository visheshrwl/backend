// Lab 01: N+1 Query Profiling — Rust, YOUR TURN (PostgreSQL).
//
// labkit::db().query_count() is your proof. Baseline and setup are given.
//   Part 1: fetch_join (1 query), fetch_in_batch (2 queries, = ANY($1))
//   Part 2: PostLoader::load / dispatch — N load() calls -> ONE query
//
//   Run / validate:  cargo run --bin stub
//   Reference:        cargo run --bin solution
#![allow(dead_code)]
use std::collections::HashMap;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;

const USER_COUNT: i32 = 100;
const POSTS_PER_USER: i32 = 5;

#[derive(Clone, Default)]
struct Post {
    id: i32,
    title: String,
}

#[derive(Default)]
struct UserPosts {
    id: i32,
    name: String,
    posts: Vec<Post>,
}

fn setup_dataset() {
    let db = labkit::db();
    db.exec("DROP TABLE IF EXISTS n1_posts", &[]);
    db.exec("DROP TABLE IF EXISTS n1_users", &[]);
    db.exec("CREATE TABLE n1_users (id INT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)", &[]);
    db.exec("CREATE TABLE n1_posts (id INT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, user_id INT NOT NULL REFERENCES n1_users(id))", &[]);
    db.exec("CREATE INDEX idx_n1_posts_user_id ON n1_posts(user_id)", &[]);
    db.exec("INSERT INTO n1_users SELECT g, 'User ' || g, 'user' || g || '@example.com' FROM generate_series(1, $1) g", &[&USER_COUNT]);
    db.exec("INSERT INTO n1_posts SELECT u*10+p, 'Post ' || p || ' by User ' || u, 'body', u FROM generate_series(1, $1) u, generate_series(0, $2) p", &[&USER_COUNT, &(POSTS_PER_USER - 1)]);
    db.reset_counters();
}

// The baseline you are fixing. (given)
fn fetch_n_plus_one() -> Vec<UserPosts> {
    let users = labkit::db().query("SELECT id, name FROM n1_users ORDER BY id", &[]);
    users
        .iter()
        .map(|u| {
            let uid: i32 = u.get("id");
            let rows = labkit::db().query("SELECT id, title FROM n1_posts WHERE user_id = $1", &[&uid]);
            let posts = rows.iter().map(|r| Post { id: r.get("id"), title: r.get("title") }).collect();
            UserPosts { id: uid, name: u.get("name"), posts }
        })
        .collect()
}

// TODO: same shape in EXACTLY ONE query (LEFT JOIN, regroup in Rust).
fn fetch_join() -> Vec<UserPosts> {
    unimplemented!("Implement fetch_join")
}

// TODO: EXACTLY TWO queries; query 2 uses WHERE user_id = ANY($1) with a Vec<i32>.
fn fetch_in_batch() -> Vec<UserPosts> {
    unimplemented!("Implement fetch_in_batch")
}

// Part 2 — DataLoader.
struct LoaderState {
    ids: Vec<i32>,
    result: Option<HashMap<i32, Vec<Post>>>,
}

struct PostLoader {
    expected: usize,
    state: Mutex<LoaderState>,
    cond: Condvar,
}

impl PostLoader {
    fn new(expected: usize) -> PostLoader {
        PostLoader { expected, state: Mutex::new(LoaderState { ids: Vec::new(), result: None }), cond: Condvar::new() }
    }

    // TODO: under self.state, push user_id to ids. When ids.len() == self.expected,
    // call dispatch(&ids), store the result, notify_all. Otherwise wait on
    // self.cond until result is set. Return result[user_id] (or empty).
    fn load(&self, _user_id: i32) -> Vec<Post> {
        unimplemented!("Implement PostLoader::load")
    }
}

// TODO: run ONE query SELECT ... WHERE user_id = ANY($1) for all ids, group by
// user_id into a HashMap<i32, Vec<Post>>.
fn dispatch(_ids: &[i32]) -> HashMap<i32, Vec<Post>> {
    unimplemented!("Implement dispatch")
}

fn fetch_with_dataloader() -> Vec<UserPosts> {
    let users = labkit::db().query("SELECT id, name FROM n1_users ORDER BY id", &[]);
    let loader = Arc::new(PostLoader::new(users.len()));
    let mut handles = Vec::new();
    for u in &users {
        let uid: i32 = u.get("id");
        let name: String = u.get("name");
        let l = loader.clone();
        handles.push(thread::spawn(move || UserPosts { id: uid, name, posts: l.load(uid) }));
    }
    let mut out: Vec<UserPosts> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    out.sort_by_key(|u| u.id);
    out
}

fn count_posts(rows: &[UserPosts]) -> usize {
    rows.iter().map(|r| r.posts.len()).sum()
}

fn assert(cond: bool, msg: &str) {
    if !cond {
        panic!("CHECK FAILED: {msg}");
    }
}

fn main() {
    setup_dataset();
    println!("Seeded {USER_COUNT} users x {POSTS_PER_USER} posts in Postgres");

    labkit::db().reset_counters();
    let n1 = fetch_n_plus_one();
    assert(labkit::db().query_count() == 101, "N+1 should run 101 queries");

    labkit::db().reset_counters();
    let join = fetch_join();
    assert(labkit::db().query_count() == 1, "JOIN should run 1 query");

    labkit::db().reset_counters();
    let batch = fetch_in_batch();
    assert(labkit::db().query_count() == 2, "IN batch should run 2 queries");

    labkit::db().reset_counters();
    let dl = fetch_with_dataloader();
    assert(labkit::db().query_count() == 2, "DataLoader should run 2 queries (1 users + 1 batched)");

    for r in [&n1, &join, &batch, &dl] {
        assert(r.len() == 100, "all return 100 users");
        assert(count_posts(r) == 500, "all return 500 posts");
    }

    println!("OK — N+1=101, JOIN=1, IN batch=2, DataLoader=2 queries");
}
