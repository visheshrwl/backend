# Lab 01: N+1 Query Profiling — Ruby reference solution (PostgreSQL).
#
# Runs against real Postgres through labkit; Labkit::DB.query_count proves
# 101 -> 1 -> 2. Creates and seeds its own tables (n1_users, n1_posts).
#
#   Run:  ruby solution.rb
require_relative '../../../tooling/ruby/labkit'

USER_COUNT = 100
POSTS_PER_USER = 5

def setup_dataset
  Labkit::DB.exec('DROP TABLE IF EXISTS n1_posts')
  Labkit::DB.exec('DROP TABLE IF EXISTS n1_users')
  Labkit::DB.exec('CREATE TABLE n1_users (id INT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)')
  Labkit::DB.exec('CREATE TABLE n1_posts (id INT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, user_id INT NOT NULL REFERENCES n1_users(id))')
  Labkit::DB.exec('CREATE INDEX idx_n1_posts_user_id ON n1_posts(user_id)')
  Labkit::DB.exec("INSERT INTO n1_users SELECT g, 'User ' || g, 'user' || g || '@example.com' FROM generate_series(1, $1) g", [USER_COUNT])
  Labkit::DB.exec("INSERT INTO n1_posts SELECT u*10+p, 'Post ' || p || ' by User ' || u, 'body', u FROM generate_series(1, $1) u, generate_series(0, $2) p", [USER_COUNT, POSTS_PER_USER - 1])
  Labkit::DB.reset_counters
end

# The baseline you are fixing — 1 + N queries.
def fetch_n_plus_one
  users = Labkit::DB.query('SELECT id, name FROM n1_users ORDER BY id')
  users.map do |u|
    posts = Labkit::DB.query('SELECT id, title FROM n1_posts WHERE user_id = $1', [u['id']])
    { 'id' => u['id'].to_i, 'name' => u['name'], 'posts' => posts }
  end
end

def fetch_join
  rows = Labkit::DB.query(
    'SELECT u.id AS user_id, u.name AS user_name, p.id AS post_id, p.title AS post_title ' \
    'FROM n1_users u LEFT JOIN n1_posts p ON p.user_id = u.id ORDER BY u.id, p.id'
  )
  by_id = {}
  order = []
  rows.each do |r|
    uid = r['user_id'].to_i
    unless by_id.key?(uid)
      by_id[uid] = { 'id' => uid, 'name' => r['user_name'], 'posts' => [] }
      order << uid
    end
    by_id[uid]['posts'] << { 'id' => r['post_id'].to_i, 'title' => r['post_title'] } unless r['post_id'].nil?
  end
  order.map { |id| by_id[id] }
end

def fetch_in_batch
  users = Labkit::DB.query('SELECT id, name FROM n1_users ORDER BY id')
  ids = users.map { |u| u['id'].to_i }
  literal = "{#{ids.join(',')}}"
  posts = Labkit::DB.query('SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY($1::int[])', [literal])
  by_user = Hash.new { |h, k| h[k] = [] }
  posts.each { |p| by_user[p['user_id'].to_i] << { 'id' => p['id'].to_i, 'title' => p['title'] } }
  users.map { |u| { 'id' => u['id'].to_i, 'name' => u['name'], 'posts' => by_user[u['id'].to_i] } }
end

# Part 2 — DataLoader. N threads each call load(); the batch dispatches a single
# query once all `expected` calls have arrived (deterministic query count).
class PostLoader
  def initialize(expected)
    @expected = expected
    @ids = []
    @result = nil
    @mutex = Mutex.new
    @cond = ConditionVariable.new
  end

  def load(user_id)
    @mutex.synchronize do
      @ids << user_id
      if @ids.size == @expected
        @result = dispatch(@ids)
        @cond.broadcast
      else
        @cond.wait(@mutex) until @result
      end
      @result[user_id] || []
    end
  end

  private

  def dispatch(ids)
    literal = "{#{ids.join(',')}}"
    rows = Labkit::DB.query('SELECT id, title, user_id FROM n1_posts WHERE user_id = ANY($1::int[])', [literal])
    grouped = Hash.new { |h, k| h[k] = [] }
    rows.each { |r| grouped[r['user_id'].to_i] << { 'id' => r['id'].to_i, 'title' => r['title'] } }
    grouped
  end
end

def fetch_with_dataloader
  users = Labkit::DB.query('SELECT id, name FROM n1_users ORDER BY id')
  loader = PostLoader.new(users.size)
  threads = users.map { |u| Thread.new { [u, loader.load(u['id'].to_i)] } }
  threads.map(&:value).map { |u, posts| { 'id' => u['id'].to_i, 'name' => u['name'], 'posts' => posts } }
end

def count_posts(rows) = rows.sum { |r| r['posts'].size }

def assert(cond, msg)
  raise "CHECK FAILED: #{msg}" unless cond
end

setup_dataset
puts "Seeded #{USER_COUNT} users x #{POSTS_PER_USER} posts in Postgres"

Labkit::DB.reset_counters
n1 = fetch_n_plus_one
assert(Labkit::DB.query_count == 101, 'N+1 should run 101 queries')

Labkit::DB.reset_counters
join = fetch_join
assert(Labkit::DB.query_count == 1, 'JOIN should run 1 query')

Labkit::DB.reset_counters
batch = fetch_in_batch
assert(Labkit::DB.query_count == 2, 'IN batch should run 2 queries')

Labkit::DB.reset_counters
dl = fetch_with_dataloader
assert(Labkit::DB.query_count == 2, 'DataLoader should run 2 queries (1 users + 1 batched)')

[n1, join, batch, dl].each do |r|
  assert(r.size == 100, 'all return 100 users')
  assert(count_posts(r) == 500, 'all return 500 posts')
end

puts 'OK — N+1=101, JOIN=1, IN batch=2, DataLoader=2 queries'
