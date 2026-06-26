"""
Postgres persistence layer for the labs.

Connection details come from the environment the lab platform pre-injects
(DATABASE_URL or the standard PG* vars). Learners never write a connection
string — they just `from labkit import db`.
"""

import os

import psycopg
from psycopg.rows import dict_row


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    return (
        f"host={os.environ.get('PGHOST', 'localhost')} "
        f"port={os.environ.get('PGPORT', '5432')} "
        f"dbname={os.environ.get('PGDATABASE', 'labs')} "
        f"user={os.environ.get('PGUSER', 'labs')} "
        f"password={os.environ.get('PGPASSWORD', 'labs')}"
    )


class DB:
    """Thin, ready-to-use Postgres handle.

    query_count is exposed so labs can *prove* a cache is working
    (a cache hit should not increment it).
    """

    def __init__(self):
        self._conn = None
        self.query_count = 0

    @property
    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row)
        return self._conn

    def query(self, sql: str, params=()) -> list[dict]:
        """Run a SELECT and return all rows as dicts."""
        self.query_count += 1
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def queryone(self, sql: str, params=()) -> dict | None:
        """Run a SELECT and return the first row (or None)."""
        self.query_count += 1
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def execute(self, sql: str, params=()) -> int:
        """Run an INSERT/UPDATE/DELETE and return affected row count."""
        self.query_count += 1
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def ping(self) -> bool:
        try:
            self.queryone("SELECT 1 AS ok")
            return True
        except Exception:
            return False

    def reset_counters(self):
        self.query_count = 0


db = DB()
