// Lab 01: N+1 Query Profiling — Go reference solution (PostgreSQL).
//
// Runs against real Postgres through labkit; labkit.DB.QueryCount() proves the
// fixes drive 101 -> 1 -> 2 round trips. The lab creates and seeds its own
// tables (n1_users, n1_posts).
//
//	Run:  go run ./solution
package main

import (
	"fmt"
	"sync"

	"labkit"
)

const (
	userCount    = 100
	postsPerUser = 5
)

type Post struct {
	ID    int    `json:"id"`
	Title string `json:"title"`
}

type UserPosts struct {
	ID    int
	Name  string
	Posts []Post
}

func toInt(v any) int {
	switch n := v.(type) {
	case int64:
		return int(n)
	case int32:
		return int(n)
	case int:
		return n
	case float64:
		return int(n)
	}
	return 0
}

func toStr(v any) string {
	switch s := v.(type) {
	case string:
		return s
	case []byte:
		return string(s)
	}
	return ""
}

// setupDataset creates and seeds this lab's own tables. (given)
func setupDataset() {
	labkit.DB.Exec("DROP TABLE IF EXISTS n1_posts")
	labkit.DB.Exec("DROP TABLE IF EXISTS n1_users")
	labkit.DB.Exec("CREATE TABLE n1_users (id INT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)")
	labkit.DB.Exec("CREATE TABLE n1_posts (id INT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, user_id INT NOT NULL REFERENCES n1_users(id))")
	labkit.DB.Exec("CREATE INDEX idx_n1_posts_user_id ON n1_posts(user_id)")
	labkit.DB.Exec("INSERT INTO n1_users SELECT g, 'User ' || g, 'user' || g || '@example.com' FROM generate_series(1, $1) g", userCount)
	labkit.DB.Exec("INSERT INTO n1_posts SELECT u*10+p, 'Post ' || p || ' by User ' || u, 'body', u FROM generate_series(1, $1) u, generate_series(0, $2) p", userCount, postsPerUser-1)
	labkit.DB.ResetCounters()
}

// fetchNPlusOne — the baseline you are fixing: 1 + N queries. (given)
func fetchNPlusOne() []UserPosts {
	users := labkit.DB.Query("SELECT id, name FROM n1_users ORDER BY id")
	out := make([]UserPosts, 0, len(users))
	for _, u := range users {
		uid := toInt(u["id"])
		rows := labkit.DB.Query("SELECT id, title FROM n1_posts WHERE user_id = $1", uid)
		posts := make([]Post, 0, len(rows))
		for _, r := range rows {
			posts = append(posts, Post{toInt(r["id"]), toStr(r["title"])})
		}
		out = append(out, UserPosts{uid, toStr(u["name"]), posts})
	}
	return out
}

// fetchJoin — 1 query with a LEFT JOIN, regrouped in Go.
func fetchJoin() []UserPosts {
	rows := labkit.DB.Query("SELECT u.id AS user_id, u.name AS user_name, p.id AS post_id, p.title AS post_title FROM n1_users u LEFT JOIN n1_posts p ON p.user_id = u.id ORDER BY u.id, p.id")
	var order []int
	byID := map[int]*UserPosts{}
	for _, r := range rows {
		uid := toInt(r["user_id"])
		up, ok := byID[uid]
		if !ok {
			up = &UserPosts{ID: uid, Name: toStr(r["user_name"])}
			byID[uid] = up
			order = append(order, uid)
		}
		if r["post_id"] != nil {
			up.Posts = append(up.Posts, Post{toInt(r["post_id"]), toStr(r["post_title"])})
		}
	}
	out := make([]UserPosts, 0, len(order))
	for _, id := range order {
		out = append(out, *byID[id])
	}
	return out
}

// fetchInBatch — 2 queries: users, then posts via = ANY($1).
func fetchInBatch() []UserPosts {
	users := labkit.DB.Query("SELECT id, name FROM n1_users ORDER BY id")
	ids := make([]int, 0, len(users))
	for _, u := range users {
		ids = append(ids, toInt(u["id"]))
	}
	rows := labkit.DB.Query("SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY($1)", ids)
	byUser := map[int][]Post{}
	for _, r := range rows {
		uid := toInt(r["user_id"])
		byUser[uid] = append(byUser[uid], Post{toInt(r["id"]), toStr(r["title"])})
	}
	out := make([]UserPosts, 0, len(users))
	for _, u := range users {
		uid := toInt(u["id"])
		out = append(out, UserPosts{uid, toStr(u["name"]), byUser[uid]})
	}
	return out
}

// ── Part 2 — DataLoader ──
//
// Coalesces N concurrent Load() calls into ONE batched query. It flushes when
// the batch is full (the maxBatchSize trigger real dataloaders use), which keeps
// the query count deterministic regardless of goroutine scheduling.

type PostLoader struct {
	mu       sync.Mutex
	expected int
	ids      []int
	waiters  []chan map[int][]Post
}

func NewPostLoader(expected int) *PostLoader {
	return &PostLoader{expected: expected}
}

func (l *PostLoader) Load(userID int) []Post {
	l.mu.Lock()
	l.ids = append(l.ids, userID)
	ch := make(chan map[int][]Post, 1)
	l.waiters = append(l.waiters, ch)
	full := len(l.waiters) == l.expected
	l.mu.Unlock()

	if full {
		l.dispatch()
	}
	return (<-ch)[userID]
}

func (l *PostLoader) dispatch() {
	l.mu.Lock()
	ids := l.ids
	waiters := l.waiters
	l.mu.Unlock()

	rows := labkit.DB.Query("SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY($1)", ids)
	grouped := map[int][]Post{}
	for _, r := range rows {
		uid := toInt(r["user_id"])
		grouped[uid] = append(grouped[uid], Post{toInt(r["id"]), toStr(r["title"])})
	}
	for _, ch := range waiters {
		ch <- grouped
	}
}

func fetchWithDataloader() []UserPosts {
	users := labkit.DB.Query("SELECT id, name FROM n1_users ORDER BY id")
	loader := NewPostLoader(len(users))
	out := make([]UserPosts, len(users))
	var wg sync.WaitGroup
	for i, u := range users {
		wg.Add(1)
		go func(i int, u map[string]any) {
			defer wg.Done()
			uid := toInt(u["id"])
			out[i] = UserPosts{uid, toStr(u["name"]), loader.Load(uid)}
		}(i, u)
	}
	wg.Wait()
	return out
}

func assert(cond bool, msg string) {
	if !cond {
		panic("CHECK FAILED: " + msg)
	}
}

func countPosts(rows []UserPosts) int {
	n := 0
	for _, r := range rows {
		n += len(r.Posts)
	}
	return n
}

func main() {
	setupDataset()
	fmt.Printf("Seeded %d users x %d posts in Postgres\n", userCount, postsPerUser)

	labkit.DB.ResetCounters()
	n1 := fetchNPlusOne()
	assert(labkit.DB.QueryCount() == 101, "N+1 should run 101 queries")

	labkit.DB.ResetCounters()
	join := fetchJoin()
	assert(labkit.DB.QueryCount() == 1, "JOIN should run 1 query")

	labkit.DB.ResetCounters()
	batch := fetchInBatch()
	assert(labkit.DB.QueryCount() == 2, "IN batch should run 2 queries")

	labkit.DB.ResetCounters()
	dl := fetchWithDataloader()
	assert(labkit.DB.QueryCount() == 2, "DataLoader should run 2 queries (1 users + 1 batched)")

	assert(len(n1) == 100 && len(join) == 100 && len(batch) == 100 && len(dl) == 100, "all return 100 users")
	assert(countPosts(n1) == 500 && countPosts(join) == 500 && countPosts(batch) == 500 && countPosts(dl) == 500, "all return 500 posts")

	fmt.Println("OK — N+1=101, JOIN=1, IN batch=2, DataLoader=2 queries")
}
