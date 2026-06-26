// Lab 02: Connection Pool Tuning — JavaScript reference solution.
//
// Node is single-threaded; concurrency is the event loop. The pool is an async
// semaphore: acquire() awaits a permit (with a timeout) before reusing an idle
// connection or creating a new one — never more than maxSize.
//
//   Run:  node solution.js
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

  _takePermit() {
    if (this.permits > 0) { this.permits--; return Promise.resolve(); }
    return new Promise((resolve, reject) => {
      const waiter = { resolve, timer: null };
      waiter.timer = setTimeout(() => {
        const i = this.waiters.indexOf(waiter);
        if (i >= 0) this.waiters.splice(i, 1);
        reject(new Error('pool exhausted: acquire timed out'));
      }, this.timeoutMs);
      this.waiters.push(waiter);
    });
  }

  async acquire() {
    await this._takePermit();
    if (this.idle.length) return this.idle.pop();
    this.created++;
    const id = this.created;
    await sleep(CREATE_MS); // creation cost
    return new Conn(id);
  }

  release(conn) {
    this.idle.push(conn);
    const w = this.waiters.shift();
    if (w) { clearTimeout(w.timer); w.resolve(); } // hand the permit to a waiter
    else this.permits++;
  }
}

async function main() {
  // 1. reuse
  const p = new Pool(2, 5000);
  const c1 = await p.acquire();
  p.release(c1);
  const c2 = await p.acquire();
  assert.strictEqual(c1.id, c2.id, 'released connection should be reused');
  p.release(c2);

  // 2. timeout when exhausted
  const p2 = new Pool(1, 200);
  const held = await p2.acquire();
  await assert.rejects(() => p2.acquire(), /timed out/, 'second acquire should time out');
  p2.release(held);

  // 3. never exceed maxSize under concurrent load
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
