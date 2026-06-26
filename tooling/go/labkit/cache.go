package labkit

import (
	"context"
	"encoding/json"
	"os"
	"time"

	"github.com/redis/go-redis/v9"
)

type cacheHandle struct {
	client *redis.Client
	ctx    context.Context
}

func newCache() *cacheHandle {
	url := os.Getenv("REDIS_URL")
	if url == "" {
		url = "redis://localhost:6379/0"
	}
	opt, err := redis.ParseURL(url)
	if err != nil {
		panic(err)
	}
	return &cacheHandle{client: redis.NewClient(opt), ctx: context.Background()}
}

// GetJSON unmarshals the cached value for key into dest. Returns false on miss.
func (c *cacheHandle) GetJSON(key string, dest any) bool {
	val, err := c.client.Get(c.ctx, key).Result()
	if err == redis.Nil {
		return false
	}
	if err != nil {
		panic(err)
	}
	if err := json.Unmarshal([]byte(val), dest); err != nil {
		panic(err)
	}
	return true
}

// SetJSON stores value as JSON with an optional TTL (seconds; 0 = no expiry).
func (c *cacheHandle) SetJSON(key string, value any, ttlSeconds int) {
	b, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	var exp time.Duration
	if ttlSeconds > 0 {
		exp = time.Duration(ttlSeconds) * time.Second
	}
	if err := c.client.Set(c.ctx, key, b, exp).Err(); err != nil {
		panic(err)
	}
}

func (c *cacheHandle) Delete(keys ...string) {
	if len(keys) > 0 {
		c.client.Del(c.ctx, keys...)
	}
}

func (c *cacheHandle) Exists(key string) bool {
	n, _ := c.client.Exists(c.ctx, key).Result()
	return n > 0
}

func (c *cacheHandle) Flush()    { c.client.FlushDB(c.ctx) }
func (c *cacheHandle) Ping() bool { return c.client.Ping(c.ctx).Err() == nil }

// Cache is the ready Redis handle.
var Cache = newCache()
