// labkit — zero-setup platform layer for the Node (JavaScript/TypeScript) labs.
//
//   const { db, cache } = require('labkit');   // or the relative path
//
// Connections come from DATABASE_URL / REDIS_URL, injected by the lab platform.
'use strict';

const { Pool } = require('pg');
const { createClient } = require('redis');

class DB {
  constructor() {
    this.pool = new Pool({
      connectionString: process.env.DATABASE_URL || 'postgres://labs:labs@localhost:5432/labs',
    });
    this.queryCount = 0;
  }

  async query(sql, params = []) {
    this.queryCount++;
    const res = await this.pool.query(sql, params);
    return res.rows;
  }

  async queryOne(sql, params = []) {
    const rows = await this.query(sql, params);
    return rows.length ? rows[0] : null;
  }

  async exec(sql, params = []) {
    this.queryCount++;
    const res = await this.pool.query(sql, params);
    return res.rowCount;
  }

  resetCounters() { this.queryCount = 0; }
  async ping() { try { await this.pool.query('SELECT 1'); return true; } catch { return false; } }
  async close() { await this.pool.end(); }
}

class Cache {
  constructor() {
    this.client = createClient({ url: process.env.REDIS_URL || 'redis://localhost:6379/0' });
    this._connected = false;
  }

  async _ensure() {
    if (!this._connected) { await this.client.connect(); this._connected = true; }
  }

  async getJSON(key) {
    await this._ensure();
    const v = await this.client.get(key);
    return v == null ? null : JSON.parse(v);
  }

  async setJSON(key, value, ttlSeconds) {
    await this._ensure();
    const opts = ttlSeconds ? { EX: ttlSeconds } : {};
    await this.client.set(key, JSON.stringify(value), opts);
  }

  async del(...keys) {
    await this._ensure();
    if (keys.length) await this.client.del(keys);
  }

  async exists(key) {
    await this._ensure();
    return (await this.client.exists(key)) > 0;
  }

  async flush() { await this._ensure(); await this.client.flushDb(); }
  async ping() { try { await this._ensure(); return (await this.client.ping()) === 'PONG'; } catch { return false; } }
  async close() { if (this._connected) await this.client.quit(); }
}

module.exports = { db: new DB(), cache: new Cache() };
