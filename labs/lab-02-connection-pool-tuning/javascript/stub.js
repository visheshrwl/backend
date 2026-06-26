// Lab 02: Connection Pool Tuning — JavaScript, YOUR TURN.
//
// Implement Pool._takePermit, acquire, and release so the pool reuses idle
// connections, times out when exhausted, and never creates more than maxSize.
//
//   Run / validate:  node stub.js     (embedded checks must pass)
//   Reference:        node solution.js
'use strict';

const { setTimeout: sleep } = require('node:timers/promises');
const assert = require('node:assert');

const CREATE_MS = 15;
const QUERY_MS = 10;

class Conn {
  constructor(id) { this.id = id; }
  async execute() { await sleep(QUERY_MS); }
}

class Pool {
  constructor(maxSize, timeoutMs) {
    this.maxSize = maxSize;
    this.timeoutMs = timeoutMs;
    this.permits = maxSize;
    this.idle = [];
    this.created = 0;
    this.waiters = [];
  }

  // TODO: resolve immediately if a permit is free (decrement permits);
  // otherwise return a Promise that resolves when release() hands over a permit,
  // and rejects after this.timeoutMs (remove the waiter from this.waiters).
  _takePermit() {
    throw new Error('TODO: implement Pool._takePermit');
  }

  // TODO: await a permit, then pop an idle connection if any, else increment
  // this.created, sleep(CREATE_MS), and return a new Conn(id).
  async acquire() {
    throw new Error('TODO: implement Pool.acquire');
  }

  // TODO: push conn back to idle; if a waiter is queued, clear its timer and
  // resolve it (hand over the permit); otherwise increment this.permits.
  release(conn) {
    throw new Error('TODO: implement Pool.release');
  }
}

async function main() {
  const p = new Pool(2, 5000);
  const c1 = await p.acquire();
  p.release(c1);
  const c2 = await p.acquire();
  assert.strictEqual(c1.id, c2.id, 'released connection should be reused');
  p.release(c2);

  const p2 = new Pool(1, 200);
  const held = await p2.acquire();
  await assert.rejects(() => p2.acquire(), /timed out/, 'second acquire should time out');
  p2.release(held);

  const p3 = new Pool(10, 30000);
  let ok = 0;
  await Promise.all(
    Array.from({ length: 100 }, async () => {
      const c = await p3.acquire();
      await c.execute();
      p3.release(c);
      ok++;
    })
  );
  assert.strictEqual(ok, 100, 'all 100 requests should succeed');
  assert.ok(p3.created <= 10, 'pool must never create more than maxSize connections');

  console.log(`OK — reuse, timeout, and bound (created=${p3.created} for 100 requests, max=10)`);
}

main().catch((e) => { console.error(e.message); process.exit(1); });
