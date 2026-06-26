# labkit (Node)

Zero-setup Postgres + Redis platform shim for the backend labs (JavaScript & TypeScript).

```js
const { db, cache } = require('labkit');

await db.queryOne('SELECT id, plan FROM users WHERE id = $1', [1]);
await cache.setJSON('user:1', { id: 1 }, 60);
```

Connections come from `DATABASE_URL` / `REDIS_URL`, injected by the lab
environment — learners never write connection strings.

## API

- `db` — `query`, `queryOne`, `exec`, `queryCount`, `resetCounters`, `ping`, `close`
- `cache` — `getJSON`, `setJSON`, `del`, `exists`, `flush`, `ping`, `close`

## Build / publish

```bash
npm publish --access public
```
