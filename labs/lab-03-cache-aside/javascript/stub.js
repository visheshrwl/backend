// Lab 03: Cache-Aside with Postgres + Redis — JavaScript, YOUR TURN.
//
//   Run / validate:  node stub.js
//   Reference:        node solution.js
//
// labkit API: db.queryOne(sql, params), db.exec(sql, params), db.queryCount,
//             cache.getJSON(key), cache.setJSON(key, v, ttl), cache.del(key), cache.exists(key)
'use strict';

const { db, cache } = require('../../../tooling/node/labkit');
const assert = require('node:assert');

const TTL = 60;
const key = (id) => `user:${id}`;

// TODO: cache-aside read — cache.getJSON first; on a miss query Postgres
// ("SELECT id, name, email, plan FROM users WHERE id = $1"), populate the cache,
// return null if the user does not exist.
async function getUserProfile(userId) {
  throw new Error('TODO: implement getUserProfile');
}

// TODO: UPDATE users SET plan = $1, updated_at = now() WHERE id = $2, then
// cache.del(key(userId)).
async function updateUserPlan(userId, plan) {
  throw new Error('TODO: implement updateUserPlan');
}

// TODO: single-flight. Keep this a SYNC function returning a promise. If
// inflight has the key, return it. Otherwise build the async work as a promise,
// register it in inflight BEFORE awaiting, clean up with p.finally, return p.
// Inside: cache.getJSON (double role of cache-aside) -> query Postgres once ->
// setJSON -> return.
const inflight = new Map();
function getUserProfileSingleflight(userId) {
  throw new Error('TODO: implement getUserProfileSingleflight');
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
