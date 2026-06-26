# Lab 02: Connection Pool Tuning — Ruby reference solution.
#
#   Run:  ruby solution.rb
CREATE_S = 0.015
QUERY_S = 0.010

def monotonic = Process.clock_gettime(Process::CLOCK_MONOTONIC)

class Conn
  attr_reader :id
  def initialize(id) = (@id = id)
  def execute = sleep(QUERY_S)
end

class PoolTimeout < StandardError; end

# Thread-safe, bounded pool. A counting "permit" (guarded by a mutex +
# condition variable) caps concurrent holders at max_size, so the pool never
# creates more than max_size connections.
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

  def acquire
    reuse = nil
    new_id = nil
    @mutex.synchronize do
      deadline = monotonic + @timeout
      while @permits <= 0
        remaining = deadline - monotonic
        raise PoolTimeout, 'pool exhausted: acquire timed out' if remaining <= 0
        @cond.wait(@mutex, remaining)
      end
      @permits -= 1
      if @idle.empty?
        @created += 1
        new_id = @created
      else
        reuse = @idle.pop
      end
    end
    return reuse if reuse

    sleep(CREATE_S) # creation cost, outside the lock
    Conn.new(new_id)
  end

  def release(conn)
    @mutex.synchronize do
      @idle.push(conn)
      @permits += 1
      @cond.signal
    end
  end
end

def assert(cond, msg)
  raise "CHECK FAILED: #{msg}" unless cond
end

# 1. reuse
p1 = Pool.new(2, 5)
c1 = p1.acquire
p1.release(c1)
c2 = p1.acquire
assert(c1.id == c2.id, 'released connection should be reused')
p1.release(c2)

# 2. timeout when exhausted
p2 = Pool.new(1, 0.2)
held = p2.acquire
begin
  p2.acquire
  assert(false, 'second acquire should time out')
rescue PoolTimeout
end
p2.release(held)

# 3. never exceed max_size under concurrent load
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
