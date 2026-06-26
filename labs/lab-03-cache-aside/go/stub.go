// Lab 03: Cache-Aside with Postgres + Redis — Go, YOUR TURN.
//
// labkit gives you ready DB (Postgres) and Cache (Redis) handles — no setup.
// Implement the three functions below.
//
//	Run / validate:  go run stub.go     (embedded checks must pass)
//	Reference:        go run ./solution
//
// labkit API:
//	labkit.DB.QueryOne(sql, args...) map[string]any   labkit.DB.Exec(sql, args...)
//	labkit.DB.QueryCount()                            (read counter)
//	labkit.Cache.GetJSON(key, &dest) bool             labkit.Cache.SetJSON(key, v, ttlSeconds)
//	labkit.Cache.Delete(keys...)                      labkit.Cache.Exists(key) bool
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
	return User{ID: toInt(row["id"]), Name: toStr(row["name"]), Email: toStr(row["email"]), Plan: toStr(row["plan"])}
}

// GetUserProfile — TODO: cache-aside read.
//
//	1. key := userKey(userID); if labkit.Cache.GetJSON(key, &u) -> return &u.
//	2. row := labkit.DB.QueryOne("SELECT id, name, email, plan FROM users WHERE id = $1", userID).
//	3. if row == nil return nil; else u = rowToUser(row); SetJSON(key, u, cacheTTL); return &u.
func GetUserProfile(userID int) *User {
	panic("TODO: implement GetUserProfile")
}

// UpdateUserPlan — TODO: write Postgres, then invalidate.
//
//	labkit.DB.Exec("UPDATE users SET plan = $1, updated_at = now() WHERE id = $2", plan, userID)
//	labkit.Cache.Delete(userKey(userID))
func UpdateUserPlan(userID int, plan string) {
	panic("TODO: implement UpdateUserPlan")
}

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

// GetUserProfileSingleflight — TODO: stampede-safe read.
//
//	Cache check; on a miss take keyLock(key), DOUBLE-CHECK the cache inside the
//	lock, then query Postgres once and populate. Only one goroutine should hit
//	the DB for a cold key.
func GetUserProfileSingleflight(userID int) *User {
	panic("TODO: implement GetUserProfileSingleflight")
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

	u := GetUserProfile(1)
	assert(u != nil && u.ID == 1, "user 1 should be fetched")
	assert(labkit.DB.QueryCount() >= 1, "first read should hit Postgres")
	assert(labkit.Cache.Exists(userKey(1)), "first read should populate the cache")

	labkit.DB.ResetCounters()
	u2 := GetUserProfile(1)
	assert(u2 != nil && u2.ID == 1, "second read returns the user")
	assert(labkit.DB.QueryCount() == 0, "a cache hit must not query Postgres")

	UpdateUserPlan(1, "enterprise")
	assert(!labkit.Cache.Exists(userKey(1)), "update must invalidate the cache")
	assert(GetUserProfile(1).Plan == "enterprise", "refilled with the new plan")

	assert(GetUserProfile(999999) == nil, "missing user returns nil")

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
