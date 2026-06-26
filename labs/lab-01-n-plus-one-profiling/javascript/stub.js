// Lab 01: N+1 Query Profiling — JavaScript, YOUR TURN (PostgreSQL).
//
// db.queryCount is your proof. The N+1 baseline and setup are given. Implement:
//   Part 1: fetchJoin (1 query), fetchInBatch (2 queries, WHERE user_id = ANY($1))
//   Part 2: PostDataLoader.load / _dispatch — N load() calls -> ONE query
//
//   Run / validate:  node stub.js
//   Reference:        node solution.js
'use strict';

const { db } = require('../../../tooling/node/labkit');
const assert = require('node:assert');

const USER_COUNT = 100;
const POSTS_PER_USER = 5;

async function setupDataset() {
  await db.exec('DROP TABLE IF EXISTS n1_posts');
  await db.exec('DROP TABLE IF EXISTS n1_users');
  await db.exec('CREATE TABLE n1_users (id INT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)');
  await db.exec('CREATE TABLE n1_posts (id INT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, user_id INT NOT NULL REFERENCES n1_users(id))');
  await db.exec('CREATE INDEX idx_n1_posts_user_id ON n1_posts(user_id)');
  await db.exec("INSERT INTO n1_users SELECT g, 'User ' || g, 'user' || g || '@example.com' FROM generate_series(1, $1) g", [USER_COUNT]);
  await db.exec("INSERT INTO n1_posts SELECT u*10+p, 'Post ' || p || ' by User ' || u, 'body', u FROM generate_series(1, $1) u, generate_series(0, $2) p", [USER_COUNT, POSTS_PER_USER - 1]);
  db.resetCounters();
}

// The baseline you are fixing. (given)
async function fetchNPlusOne() {
  const users = await db.query('SELECT id, name FROM n1_users ORDER BY id');
  const out = [];
  for (const u of users) {
    const posts = await db.query('SELECT id, title FROM n1_posts WHERE user_id = $1', [u.id]);
    out.push({ id: u.id, name: u.name, posts });
  }
  return out;
}

// TODO: same shape in EXACTLY ONE query (LEFT JOIN, regroup in JS).
async function fetchJoin() {
  throw new Error('TODO: implement fetchJoin');
}

// TODO: EXACTLY TWO queries; query 2 uses WHERE user_id = ANY($1) with the
// array of user ids; group posts by user_id.
async function fetchInBatch() {
  throw new Error('TODO: implement fetchInBatch');
}

// Part 2 — DataLoader.
class PostDataLoader {
  constructor() { this.queue = []; this.futures = new Map(); this.scheduled = false; }

  // TODO: return a Promise; register its resolver in this.futures[userId] and
  // push userId to this.queue. The first call schedules _dispatch on the next
  // microtask (queueMicrotask), guarded by this.scheduled.
  load(userId) {
    throw new Error('TODO: implement PostDataLoader.load');
  }

  // TODO: dedupe this.queue; run ONE query SELECT ... WHERE user_id = ANY($1);
  // group by user_id; resolve each future with its posts ([] if none).
  async _dispatch() {
    throw new Error('TODO: implement PostDataLoader._dispatch');
  }
}

async function fetchWithDataloader() {
  const users = await db.query('SELECT id, name FROM n1_users ORDER BY id');
  const loader = new PostDataLoader();
  const posts = await Promise.all(users.map((u) => loader.load(u.id)));
  return users.map((u, i) => ({ id: u.id, name: u.name, posts: posts[i] }));
}

const countPosts = (rows) => rows.reduce((n, r) => n + r.posts.length, 0);

async function main() {
  await setupDataset();
  console.log(`Seeded ${USER_COUNT} users x ${POSTS_PER_USER} posts in Postgres`);

  db.resetCounters();
  const n1 = await fetchNPlusOne();
  assert.strictEqual(db.queryCount, 101, 'N+1 should run 101 queries');

  db.resetCounters();
  const join = await fetchJoin();
  assert.strictEqual(db.queryCount, 1, 'JOIN should run 1 query');

  db.resetCounters();
  const batch = await fetchInBatch();
  assert.strictEqual(db.queryCount, 2, 'IN batch should run 2 queries');

  db.resetCounters();
  const dl = await fetchWithDataloader();
  assert.strictEqual(db.queryCount, 2, 'DataLoader should run 2 queries (1 users + 1 batched)');

  for (const r of [n1, join, batch, dl]) {
    assert.strictEqual(r.length, 100, 'all return 100 users');
    assert.strictEqual(countPosts(r), 500, 'all return 500 posts');
  }

  console.log('OK — N+1=101, JOIN=1, IN batch=2, DataLoader=2 queries');
  await db.close();
}

main().catch((e) => { console.error(e.message); process.exit(1); });
