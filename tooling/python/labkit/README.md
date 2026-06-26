# labkit (Python)

Zero-setup Postgres + Redis platform shim for the backend labs.

```python
from labkit import db, cache

db.queryone("SELECT id, plan FROM users WHERE id = %s", (1,))
cache.set_json("user:1", {"id": 1}, ttl=60)
```

Connections come from `DATABASE_URL` / `REDIS_URL`, injected by the lab
environment — learners never write connection strings.

## API

- `db` — `query`, `queryone`, `execute`, `query_count`, `reset_counters`, `ping`
- `cache` — `get`, `set`, `get_json`, `set_json`, `delete`, `exists`, `incr`, `flush`, `ping`
- `labkit.ping()`, `labkit.reset()`

## Build / publish

```bash
python -m build
twine upload dist/*
```
