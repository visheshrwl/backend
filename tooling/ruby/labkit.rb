# labkit — zero-setup platform layer for the Ruby labs.
#
#   require_relative '../../../tooling/ruby/labkit'
#   Labkit::DB.query("SELECT 1 AS ok")
#
# Connections come from DATABASE_URL / REDIS_URL, injected by the lab platform.
require 'pg'
require 'redis'
require 'json'

module Labkit
  class DBClient
    attr_reader :query_count

    def initialize
      @conn = PG.connect(ENV['DATABASE_URL'] || 'postgres://labs:labs@localhost:5432/labs')
      @query_count = 0
      @mutex = Mutex.new
    end

    # Returns rows as hashes with string keys (libpq returns text values).
    def query(sql, params = [])
      @mutex.synchronize do
        @query_count += 1
        @conn.exec_params(sql, params).to_a
      end
    end

    def query_one(sql, params = [])
      query(sql, params).first
    end

    def exec(sql, params = [])
      @mutex.synchronize do
        @query_count += 1
        @conn.exec_params(sql, params).cmd_tuples
      end
    end

    def reset_counters
      @query_count = 0
    end

    def ping
      query('SELECT 1')
      true
    rescue StandardError
      false
    end
  end

  class CacheClient
    def initialize
      @redis = Redis.new(url: ENV['REDIS_URL'] || 'redis://localhost:6379/0')
    end

    def get_json(key)
      v = @redis.get(key)
      v && JSON.parse(v)
    end

    def set_json(key, value, ttl = nil)
      @redis.set(key, JSON.generate(value))
      @redis.expire(key, ttl) if ttl
    end

    def delete(*keys)
      @redis.del(*keys) unless keys.empty?
    end

    def exists?(key)
      @redis.exists?(key)
    end

    def flush
      @redis.flushdb
    end

    def ping
      @redis.ping == 'PONG'
    rescue StandardError
      false
    end
  end

  DB = DBClient.new
  CACHE = CacheClient.new
end
