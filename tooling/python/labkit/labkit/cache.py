"""
Redis cache / coordination layer for the labs.

Connection comes from REDIS_URL, pre-injected by the lab platform.
Learners just `from labkit import cache`.
"""

import json
import os

import redis


def _client() -> redis.Redis:
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.from_url(url, decode_responses=True)


class Cache:
    """Thin, ready-to-use Redis handle with JSON helpers."""

    def __init__(self):
        self._client = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            self._client = _client()
        return self._client

    def get(self, key: str) -> str | None:
        return self.client.get(key)

    def set(self, key: str, value: str, ttl: int | None = None):
        self.client.set(key, value, ex=ttl)

    def get_json(self, key: str):
        raw = self.client.get(key)
        return json.loads(raw) if raw is not None else None

    def set_json(self, key: str, value, ttl: int | None = None):
        self.client.set(key, json.dumps(value), ex=ttl)

    def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return self.client.delete(*keys)

    def exists(self, key: str) -> bool:
        return bool(self.client.exists(key))

    def incr(self, key: str, amount: int = 1) -> int:
        return self.client.incr(key, amount)

    def flush(self):
        """Clear the cache (test isolation)."""
        self.client.flushdb()

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:
            return False


cache = Cache()
