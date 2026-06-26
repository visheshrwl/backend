// Lab 03: Cache-Aside with Postgres + Redis — Go reference solution.
//
// Postgres is the source of truth; Redis is a lazy-loaded cache in front of it.
// You never open a connection — labkit hands you ready DB and Cache handles.
//
//	Run:  go run ./solution
package main

import (
	"fmt"
	"sync"

	"labkit"
)

const cacheTTL = 60

type User struct {
	ID    int    `json:"id"`
	Name  string `json:"name"`
	Email string `json:"email"`
	Plan  string `json:"plan"`
}

func userKey(id int) string { return fmt.Sprintf("user:%d", id) }

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

func rowToUser(row map[string]any) User {
	return User{
		ID:    toInt(row["id"]),
		Name:  toStr(row["name"]),
		Email: toStr(row["email"]),
		Plan:  toStr(row["plan"]),
	}
}

// GetUserProfile — cache-aside read: cache first, then Postgres, then populate.
func GetUserProfile(userID int) *User {
	key := userKey(userID)
	var u User
	if labkit.Cache.GetJSON(key, &u) {
		return &u
	}
	row := labkit.DB.QueryOne("SELECT id, name, email, plan FROM users WHERE id = $1", userID)
	if row == nil {
		return nil
	}
	u = rowToUser(row)
	labkit.Cache.SetJSON(key, u, cacheTTL)
	return &u
}

// UpdateUserPlan — write to Postgres, then invalidate the cached entry.
func UpdateUserPlan(userID int, plan string) {
	labkit.DB.Exec("UPDATE users SET plan = $1, updated_at = now() WHERE id = $2", plan, userID)
	labkit.Cache.Delete(userKey(userID))
}

// ── single-flight (stampede protection) ──

var (
	locks      = map[string]*sync.Mutex{}
	locksGuard sync.Mutex
)

func keyLock(key string) *sync.Mutex {
	locksGuard.Lock()
	defer locksGuard.Unlock()
	if l, ok := locks[key]; ok {
		return l
	}
	l := &sync.Mutex{}
	locks[key] = l
	return l
}

// GetUserProfileSingleflight — only one goroutine queries Postgres on a cold
// hot-key miss; the rest wait on the per-key lock and reuse its result.
func GetUserProfileSingleflight(userID int) *User {
	key := userKey(userID)
	var u User
	if labkit.Cache.GetJSON(key, &u) {
		return &u
	}
	lock := keyLock(key)
	lock.Lock()
	defer lock.Unlock()
	if labkit.Cache.GetJSON(key, &u) { // double-check
		return &u
	}
	row := labkit.DB.QueryOne("SELECT id, name, email, plan FROM users WHERE id = $1", userID)
	if row == nil {
		return nil
	}
	u = rowToUser(row)
	labkit.Cache.SetJSON(key, u, cacheTTL)
	return &u
}

func assert(cond bool, msg string) {
	if !cond {
		panic("CHECK FAILED: " + msg)
	}
}

func main() {
	labkit.DB.Exec("UPDATE users SET plan = 'pro' WHERE id = 1")
	labkit.Cache.Flush()
	labkit.DB.ResetCounters()

	// 1. first read hits Postgres + populates cache
	u := GetUserProfile(1)
	assert(u != nil && u.ID == 1, "user 1 should be fetched")
	assert(labkit.DB.QueryCount() >= 1, "first read should hit Postgres")
	assert(labkit.Cache.Exists(userKey(1)), "first read should populate the cache")

	// 2. second read served from cache
	labkit.DB.ResetCounters()
	u2 := GetUserProfile(1)
	assert(u2 != nil && u2.ID == 1, "second read returns the user")
	assert(labkit.DB.QueryCount() == 0, "a cache hit must not query Postgres")

	// 3. update invalidates the cache
	UpdateUserPlan(1, "enterprise")
	assert(!labkit.Cache.Exists(userKey(1)), "update must invalidate the cache")
	assert(GetUserProfile(1).Plan == "enterprise", "refilled with the new plan")

	// 4. missing user
	assert(GetUserProfile(999999) == nil, "missing user returns nil")

	// 5. single-flight: 50 concurrent cold reads -> exactly one DB query
	labkit.Cache.Flush()
	labkit.DB.ResetCounters()
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			GetUserProfileSingleflight(1)
		}()
	}
	wg.Wait()
	assert(labkit.DB.QueryCount() == 1, "single-flight: only one DB query on a cold miss")

	fmt.Println("OK — cache-aside, invalidation, and single-flight (1 DB query for 50 concurrent reads)")
}
