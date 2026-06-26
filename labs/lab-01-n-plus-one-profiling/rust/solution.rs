// Lab 01: N+1 Query Profiling — Rust reference solution (PostgreSQL).
//
// Runs against real Postgres through labkit; labkit::db().query_count() proves
// 101 -> 1 -> 2. Creates and seeds its own tables (n1_users, n1_posts).
//   Run:  cargo run --bin solution
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

// The baseline you are fixing — 1 + N queries.
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

fn fetch_join() -> Vec<UserPosts> {
    let rows = labkit::db().query(
        "SELECT u.id AS user_id, u.name AS user_name, p.id AS post_id, p.title AS post_title \
         FROM n1_users u LEFT JOIN n1_posts p ON p.user_id = u.id ORDER BY u.id, p.id",
        &[],
    );
    let mut order: Vec<i32> = Vec::new();
    let mut by_id: HashMap<i32, UserPosts> = HashMap::new();
    for r in &rows {
        let uid: i32 = r.get("user_id");
        let up = by_id.entry(uid).or_insert_with(|| {
            order.push(uid);
            UserPosts { id: uid, name: r.get("user_name"), posts: Vec::new() }
        });
        let post_id: Option<i32> = r.get("post_id");
        if let Some(pid) = post_id {
            up.posts.push(Post { id: pid, title: r.get("post_title") });
        }
    }
    order.into_iter().map(|id| by_id.remove(&id).unwrap()).collect()
}

fn fetch_in_batch() -> Vec<UserPosts> {
    let users = labkit::db().query("SELECT id, name FROM n1_users ORDER BY id", &[]);
    let ids: Vec<i32> = users.iter().map(|u| u.get("id")).collect();
    let rows = labkit::db().query("SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY($1)", &[&ids]);
    let mut by_user: HashMap<i32, Vec<Post>> = HashMap::new();
    for r in &rows {
        let uid: i32 = r.get("user_id");
        by_user.entry(uid).or_default().push(Post { id: r.get("id"), title: r.get("title") });
    }
    users
        .iter()
        .map(|u| {
            let uid: i32 = u.get("id");
            UserPosts { id: uid, name: u.get("name"), posts: by_user.remove(&uid).unwrap_or_default() }
        })
        .collect()
}

// Part 2 — DataLoader. N threads call load(); the batch dispatches one query
// once all `expected` ids have arrived (deterministic query count).
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

    fn load(&self, user_id: i32) -> Vec<Post> {
        let mut state = self.state.lock().unwrap();
        state.ids.push(user_id);
        if state.ids.len() == self.expected {
            let ids = state.ids.clone();
            state.result = Some(dispatch(&ids));
            self.cond.notify_all();
        } else {
            while state.result.is_none() {
                state = self.cond.wait(state).unwrap();
            }
        }
        state.result.as_ref().unwrap().get(&user_id).cloned().unwrap_or_default()
    }
}

fn dispatch(ids: &[i32]) -> HashMap<i32, Vec<Post>> {
    let rows = labkit::db().query("SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY($1)", &[&ids.to_vec()]);
    let mut grouped: HashMap<i32, Vec<Post>> = HashMap::new();
    for r in &rows {
        let uid: i32 = r.get("user_id");
        grouped.entry(uid).or_default().push(Post { id: r.get("id"), title: r.get("title") });
    }
    grouped
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
