# labkit (Rust)

Zero-setup Postgres + Redis platform shim for the backend labs.

```rust
let row = labkit::db().query_one("SELECT id, plan FROM users WHERE id = $1", &[&1i32]);
labkit::cache().set("user:1", "{...}", Some(60));
```

Connections come from `DATABASE_URL` / `REDIS_URL`, injected by the lab
environment — learners never write connection strings.

## API

- `labkit::db()` — `query`, `query_one`, `exec`, `query_count`, `reset_counters`, `ping`
- `labkit::cache()` — `get`, `set`, `del`, `exists`, `flush`

## Build / publish

```bash
cargo publish
```
