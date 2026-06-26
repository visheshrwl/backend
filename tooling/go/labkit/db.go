// Package labkit is the zero-setup platform layer for the Go labs.
//
//	import "labkit"
//	rows := labkit.DB.Query("SELECT 1 AS ok")
//
// Connections come from the environment the lab platform injects
// (DATABASE_URL / REDIS_URL). Learners never write a connection string.
package labkit

import (
	"database/sql"
	"os"
	"sync/atomic"

	_ "github.com/jackc/pgx/v5/stdlib" // registers the "pgx" sql driver
)

type database struct {
	conn       *sql.DB
	queryCount int64
}

func newDB() *database {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		dsn = "postgres://labs:labs@localhost:5432/labs"
	}
	conn, err := sql.Open("pgx", dsn) // lazy: no connection until first use
	if err != nil {
		panic(err)
	}
	return &database{conn: conn}
}

// Query runs a SELECT and returns every row as a map of column -> value.
func (d *database) Query(query string, args ...any) []map[string]any {
	atomic.AddInt64(&d.queryCount, 1)
	rows, err := d.conn.Query(query, args...)
	if err != nil {
		panic(err)
	}
	defer rows.Close()

	cols, _ := rows.Columns()
	var out []map[string]any
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			panic(err)
		}
		m := make(map[string]any, len(cols))
		for i, c := range cols {
			m[c] = vals[i]
		}
		out = append(out, m)
	}
	return out
}

// QueryOne returns the first row of a SELECT, or nil if there are none.
func (d *database) QueryOne(query string, args ...any) map[string]any {
	rows := d.Query(query, args...)
	if len(rows) == 0 {
		return nil
	}
	return rows[0]
}

// Exec runs an INSERT/UPDATE/DELETE/DDL and returns affected rows.
func (d *database) Exec(query string, args ...any) int64 {
	atomic.AddInt64(&d.queryCount, 1)
	res, err := d.conn.Exec(query, args...)
	if err != nil {
		panic(err)
	}
	n, _ := res.RowsAffected()
	return n
}

// QueryCount lets labs prove a cache hit did not touch Postgres.
func (d *database) QueryCount() int64   { return atomic.LoadInt64(&d.queryCount) }
func (d *database) ResetCounters()      { atomic.StoreInt64(&d.queryCount, 0) }
func (d *database) Ping() bool          { return d.conn.Ping() == nil }

// DB is the ready Postgres handle.
var DB = newDB()
