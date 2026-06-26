# Lab 03: Cache-Aside with Postgres + Redis — Ruby, YOUR TURN.
#
#   Run / validate:  ruby stub.rb
#   Reference:        ruby solution.rb
#
# labkit API: Labkit::DB.query_one(sql, params), Labkit::DB.exec(sql, params),
#             Labkit::DB.query_count, Labkit::CACHE.get_json(key),
#             Labkit::CACHE.set_json(key, value, ttl), Labkit::CACHE.delete(key),
#             Labkit::CACHE.exists?(key)
require_relative '../../../tooling/ruby/labkit/lib/labkit'

TTL = 60
def user_key(id) = "user:#{id}"

# TODO: cache-aside read — CACHE.get_json first; on a miss query Postgres
# ("SELECT id, name, email, plan FROM users WHERE id = $1"), populate the cache,
# return nil if the user does not exist.
def get_user_profile(user_id)
  raise NotImplementedError, 'Implement get_user_profile'
end

# TODO: UPDATE users SET plan = $1, updated_at = now() WHERE id = $2; then
# CACHE.delete(user_key(user_id)).
def update_user_plan(user_id, plan)
  raise NotImplementedError, 'Implement update_user_plan'
end

LOCKS = {}
LOCKS_GUARD = Mutex.new

def key_lock(key)
  LOCKS_GUARD.synchronize { LOCKS[key] ||= Mutex.new }
end

# TODO: stampede-safe read. Cache check; on a miss take key_lock(key).synchronize,
# DOUBLE-CHECK the cache inside the lock, then query Postgres once and populate.
# Only one thread should hit the DB for a cold key.
def get_user_profile_singleflight(user_id)
  raise NotImplementedError, 'Implement get_user_profile_singleflight'
end

def assert(cond, msg)
  raise "CHECK FAILED: #{msg}" unless cond
end

Labkit::DB.exec("UPDATE users SET plan = 'pro' WHERE id = 1")
Labkit::CACHE.flush
Labkit::DB.reset_counters

u = get_user_profile(1)
assert(u && u['id'].to_i == 1, 'user 1 should be fetched')
assert(Labkit::DB.query_count >= 1, 'first read should hit Postgres')
assert(Labkit::CACHE.exists?(user_key(1)), 'first read should populate the cache')

Labkit::DB.reset_counters
u2 = get_user_profile(1)
assert(u2['id'].to_i == 1, 'second read returns the user')
assert(Labkit::DB.query_count == 0, 'a cache hit must not query Postgres')

update_user_plan(1, 'enterprise')
assert(!Labkit::CACHE.exists?(user_key(1)), 'update must invalidate the cache')
assert(get_user_profile(1)['plan'] == 'enterprise', 'refilled with the new plan')

assert(get_user_profile(999_999).nil?, 'missing user returns nil')

Labkit::CACHE.flush
Labkit::DB.reset_counters
threads = Array.new(50) { Thread.new { get_user_profile_singleflight(1) } }
threads.each(&:join)
assert(Labkit::DB.query_count == 1, 'single-flight: only one DB query for 50 concurrent reads')

puts 'OK — cache-aside, invalidation, and single-flight (1 DB query for 50 concurrent reads)'
