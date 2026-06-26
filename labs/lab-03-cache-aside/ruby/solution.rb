# Lab 03: Cache-Aside with Postgres + Redis — Ruby reference solution.
#
#   Run:  ruby solution.rb
require_relative '../../../tooling/ruby/labkit/lib/labkit'

TTL = 60
def user_key(id) = "user:#{id}"

# Part 1 — cache-aside read.
def get_user_profile(user_id)
  key = user_key(user_id)
  cached = Labkit::CACHE.get_json(key)
  return cached if cached

  row = Labkit::DB.query_one('SELECT id, name, email, plan FROM users WHERE id = $1', [user_id])
  return nil unless row

  Labkit::CACHE.set_json(key, row, TTL)
  row
end

def update_user_plan(user_id, plan)
  Labkit::DB.exec('UPDATE users SET plan = $1, updated_at = now() WHERE id = $2', [plan, user_id])
  Labkit::CACHE.delete(user_key(user_id))
end

# Part 2 — single-flight: a per-key lock + double-check so only one thread
# queries Postgres on a cold hot-key miss.
LOCKS = {}
LOCKS_GUARD = Mutex.new

def key_lock(key)
  LOCKS_GUARD.synchronize { LOCKS[key] ||= Mutex.new }
end

def get_user_profile_singleflight(user_id)
  key = user_key(user_id)
  cached = Labkit::CACHE.get_json(key)
  return cached if cached

  key_lock(key).synchronize do
    cached = Labkit::CACHE.get_json(key) # double-check
    return cached if cached

    row = Labkit::DB.query_one('SELECT id, name, email, plan FROM users WHERE id = $1', [user_id])
    return nil unless row

    Labkit::CACHE.set_json(key, row, TTL)
    row
  end
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
