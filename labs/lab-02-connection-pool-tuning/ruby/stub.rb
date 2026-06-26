# Lab 02: Connection Pool Tuning — Ruby, YOUR TURN.
#
# Implement Pool#acquire and Pool#release so the pool reuses idle connections,
# times out when exhausted (raise PoolTimeout), and never creates more than
# max_size connections.
#
#   Run / validate:  ruby stub.rb     (embedded checks must pass)
#   Reference:        ruby solution.rb
CREATE_S = 0.015
QUERY_S = 0.010

def monotonic = Process.clock_gettime(Process::CLOCK_MONOTONIC)

class Conn
  attr_reader :id
  def initialize(id) = (@id = id)
  def execute = sleep(QUERY_S)
end

class PoolTimeout < StandardError; end

class Pool
  attr_reader :created

  def initialize(max_size, timeout)
    @max_size = max_size
    @timeout = timeout
    @permits = max_size
    @idle = []
    @created = 0
    @mutex = Mutex.new
    @cond = ConditionVariable.new
  end

  # TODO:
  #   Under @mutex: wait (with @cond.wait(@mutex, remaining)) until a permit is
  #   free or the deadline passes (raise PoolTimeout on timeout). Then take a
  #   permit (@permits -= 1) and either pop an idle connection or bump @created
  #   and remember the new id. Finally, for the create path, sleep(CREATE_S)
  #   OUTSIDE the lock and return Conn.new(id).
  def acquire
    raise NotImplementedError, 'Implement Pool#acquire'
  end

  # TODO:
  #   Under @mutex: push conn to @idle, give back a permit (@permits += 1), and
  #   @cond.signal a waiter.
  def release(conn)
    raise NotImplementedError, 'Implement Pool#release'
  end
end

def assert(cond, msg)
  raise "CHECK FAILED: #{msg}" unless cond
end

p1 = Pool.new(2, 5)
c1 = p1.acquire
p1.release(c1)
c2 = p1.acquire
assert(c1.id == c2.id, 'released connection should be reused')
p1.release(c2)

p2 = Pool.new(1, 0.2)
held = p2.acquire
begin
  p2.acquire
  assert(false, 'second acquire should time out')
rescue PoolTimeout
end
p2.release(held)

p3 = Pool.new(10, 30)
ok = 0
ok_mutex = Mutex.new
threads = 100.times.map do
  Thread.new do
    c = p3.acquire
    c.execute
    p3.release(c)
    ok_mutex.synchronize { ok += 1 }
  end
end
threads.each(&:join)
assert(ok == 100, 'all 100 requests should succeed')
assert(p3.created <= 10, 'pool must never create more than max_size connections')

puts "OK — reuse, timeout, and bound (created=#{p3.created} for 100 requests, max=10)"
