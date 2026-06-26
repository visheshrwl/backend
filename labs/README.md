# Labs

Hands-on exercises with complete runnable code and a zero-setup environment.
Open any lab in **GitHub Codespaces** or **Gitpod** (or `docker compose up`
locally) and the required services are already running — no DB connections, no
cloud, no devops.

## Available Labs

| Lab | Module | Layers | What You'll Learn | Time |
|-----|--------|--------|-------------------|------|
| [lab-01-n-plus-one-profiling](lab-01-n-plus-one-profiling/) | N+1 Queries | stdlib | Observe N+1, fix with JOIN and IN batch, measure improvement | 30 min |
| [lab-02-connection-pool-tuning](lab-02-connection-pool-tuning/) | Connection Pooling | stdlib | Implement a pool; pool size impact on p50/p99/throughput | 45 min |
| [lab-03-cache-aside](lab-03-cache-aside/) | Caching Strategy | Postgres + Redis | Read-through cache, prove cache hits skip the DB, invalidation | 40 min |

## How a lab is structured

```
lab-XX-name/
  lab.json          manifest: module link, infra needed, languages, run/test commands
  README.md         instructions
  python/
    stub.py         the file you implement (TODOs)
    solution.py     reference solution
    test_lab.py     run to validate your work
```

To validate your work:

```bash
cd labs/<lab>/python
python -m unittest test_lab.py     # green OK = done
```

## The platform layer (labs that need persistence)

Labs with `infra` in their `lab.json` (e.g. `lab-03`) use real Postgres and
Redis — but you never write a connection string. Import the ready handles:

```python
from labkit import db, cache
```

`db` is Postgres (`db.query`, `db.queryone`, `db.execute`, `db.query_count`);
`cache` is Redis (`cache.get_json`, `cache.set_json`, `cache.delete`, ...).
The services and credentials are wired by the environment — see
[`.devcontainer/`](../.devcontainer/) and [`tooling/python/labkit`](../tooling/python/labkit).

## What Makes a Good Lab Session

1. **Run the solution unmodified first** — see the baseline behaviour
2. **Implement the stub** — make `test_lab.py` go green
3. **Change one parameter at a time** — observe the effect
4. **Explain the results to yourself** — connect to the theory in the parent module
