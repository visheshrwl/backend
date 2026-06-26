# labkit (Go)

Zero-setup Postgres + Redis platform shim for the backend labs.

```go
rows := labkit.DB.Query("SELECT id, plan FROM users WHERE id = $1", 1)
labkit.Cache.SetJSON("user:1", user, 60)
```

Connections come from `DATABASE_URL` / `REDIS_URL`, injected by the lab
environment — learners never write connection strings.

## API

- `labkit.DB` — `Query`, `QueryOne`, `Exec`, `QueryCount`, `ResetCounters`, `Ping`
- `labkit.Cache` — `GetJSON`, `SetJSON`, `Delete`, `Exists`, `Flush`, `Ping`

## Publish

Go modules are git-based: push this folder to its own repo
(`github.com/visheshrwl/labkit-go`), set `module github.com/visheshrwl/labkit-go`
in `go.mod`, and `git tag v0.1.0`. Labs then `go get` it and
`import "github.com/visheshrwl/labkit-go"` (package stays `labkit`).
In this monorepo the labs use a local `replace` directive instead.
