// Lab 03: Cache-Aside with Postgres + Redis — JavaScript reference solution.
//
// labkit hands you ready db (Postgres) and cache (Redis) handles — no setup.
//   Run:  node solution.js
'use strict';

const { db, cache } = require('../../../tooling/node/labkit');
const assert = require('node:assert');

const TTL = 60;
const key = (id) => `user:${id}`;

// Part 1 — cache-aside read.
async function getUserProfile(userId) {
  const cached = await cache.getJSON(key(userId));
  if (cached) return cached;
  const row = await db.queryOne('SELECT id, name, email, plan FROM users WHERE id = $1', [userId]);
  if (!row) return null;
  await cache.setJSON(key(userId), row, TTL);
  return row;
}

async function updateUserPlan(userId, plan) {
  await db.exec('UPDATE users SET plan = $1, updated_at = now() WHERE id = $2', [plan, userId]);
  await cache.del(key(userId));
}

// Part 2 — single-flight. In Node's single-threaded model the idiomatic guard is
// an in-flight-promise map (not a mutex): register the promise synchronously
// before any await, so concurrent callers share one DB round trip.
const inflight = new Map();
function getUserProfileSingleflight(userId) {
  const k = key(userId);
  if (inflight.has(k)) return inflight.get(k);
  const p = (async () => {
    const cached = await cache.getJSON(k);
    if (cached) return cached;
    const row = await db.queryOne('SELECT id, name, email, plan FROM users WHERE id = $1', [userId]);
    if (!row) return null;
    await cache.setJSON(k, row, TTL);
    return row;
  })();
  inflight.set(k, p);
  p.finally(() => inflight.delete(k));
  return p;
}

async function main() {
  await db.exec("UPDATE users SET plan = 'pro' WHERE id = 1");
  await cache.flush();
  db.resetCounters();

  const u = await getUserProfile(1);
  assert.ok(u && u.id === 1, 'user 1 should be fetched');
  assert.ok(db.queryCount >= 1, 'first read should hit Postgres');
  assert.ok(await cache.exists(key(1)), 'first read should populate the cache');

  db.resetCounters();
  const u2 = await getUserProfile(1);
  assert.strictEqual(u2.id, 1, 'second read returns the user');
  assert.strictEqual(db.queryCount, 0, 'a cache hit must not query Postgres');

  await updateUserPlan(1, 'enterprise');
  assert.ok(!(await cache.exists(key(1))), 'update must invalidate the cache');
  assert.strictEqual((await getUserProfile(1)).plan, 'enterprise', 'refilled with new plan');

  assert.strictEqual(await getUserProfile(999999), null, 'missing user returns null');

  await cache.flush();
  db.resetCounters();
  await Promise.all(Array.from({ length: 50 }, () => getUserProfileSingleflight(1)));
  assert.strictEqual(db.queryCount, 1, 'single-flight: one DB query for 50 concurrent reads');

  console.log('OK — cache-aside, invalidation, and single-flight (1 DB query for 50 concurrent reads)');
  await db.close();
  await cache.close();
}

main().catch((e) => { console.error(e.message); process.exit(1); });
