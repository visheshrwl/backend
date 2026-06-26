# labkit (Ruby)

Zero-setup Postgres + Redis platform shim for the backend labs.

```ruby
require 'labkit'

Labkit::DB.query_one('SELECT id, plan FROM users WHERE id = $1', [1])
Labkit::CACHE.set_json('user:1', { id: 1 }, 60)
```

Connections come from `DATABASE_URL` / `REDIS_URL`, injected by the lab
environment — learners never write connection strings.

## API

- `Labkit::DB` — `query`, `query_one`, `exec`, `query_count`, `reset_counters`, `ping`
- `Labkit::CACHE` — `get_json`, `set_json`, `delete`, `exists?`, `flush`, `ping`

## Build / publish

```bash
gem build labkit.gemspec
gem push labkit-0.1.0.gem
```
