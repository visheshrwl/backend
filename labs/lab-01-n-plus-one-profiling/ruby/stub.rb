# Lab 01: N+1 Query Profiling — Ruby, YOUR TURN (PostgreSQL).
#
# Labkit::DB.query_count is your proof. The baseline and setup are given.
#   Part 1: fetch_join (1 query), fetch_in_batch (2 queries, = ANY($1::int[]))
#   Part 2: PostLoader#load / dispatch — N load() calls -> ONE query
#
#   Run / validate:  ruby stub.rb
#   Reference:        ruby solution.rb
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

# The baseline you are fixing. (given)
def fetch_n_plus_one
  users = Labkit::DB.query('SELECT id, name FROM n1_users ORDER BY id')
  users.map do |u|
    posts = Labkit::DB.query('SELECT id, title FROM n1_posts WHERE user_id = $1', [u['id']])
    { 'id' => u['id'].to_i, 'name' => u['name'], 'posts' => posts }
  end
end

# TODO: same shape in EXACTLY ONE query (LEFT JOIN, regroup in Ruby).
def fetch_join
  raise NotImplementedError, 'Implement fetch_join'
end

# TODO: EXACTLY TWO queries; query 2 uses WHERE user_id = ANY($1::int[]) with the
# array literal "{1,2,...}"; group posts by user_id.
def fetch_in_batch
  raise NotImplementedError, 'Implement fetch_in_batch'
end

# Part 2 — DataLoader.
class PostLoader
  def initialize(expected)
    @expected = expected
    @ids = []
    @result = nil
    @mutex = Mutex.new
    @cond = ConditionVariable.new
  end

  # TODO: under @mutex, append user_id to @ids. When @ids.size == @expected, the
  # batch is full — run dispatch and @cond.broadcast. Otherwise @cond.wait(@mutex)
  # until @result is set. Return @result[user_id] || [].
  def load(user_id)
    raise NotImplementedError, 'Implement PostLoader#load'
  end

  # TODO: run ONE query SELECT ... WHERE user_id = ANY($1::int[]) for all ids,
  # group rows by user_id into a Hash, and return it.
  def dispatch(ids)
    raise NotImplementedError, 'Implement PostLoader#dispatch'
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
