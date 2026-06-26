// Lab 02: Connection Pool Tuning — TypeScript reference solution.
//
//   Run:  node --experimental-strip-types solution.ts
import { setTimeout as sleep } from 'node:timers/promises';
import assert from 'node:assert';

const CREATE_MS = 15;
const QUERY_MS = 10;

class Conn {
  readonly id: number;
  constructor(id: number) { this.id = id; }
  async execute(): Promise<void> { await sleep(QUERY_MS); }
}

interface Waiter {
  resolve: () => void;
  timer: NodeJS.Timeout;
}

class Pool {
  private maxSize: number;
  private timeoutMs: number;
  private permits: number;
  private idle: Conn[] = [];
  created = 0;
  private waiters: Waiter[] = [];

  constructor(maxSize: number, timeoutMs: number) {
    this.maxSize = maxSize;
    this.timeoutMs = timeoutMs;
    this.permits = maxSize;
  }

  private takePermit(): Promise<void> {
    if (this.permits > 0) { this.permits--; return Promise.resolve(); }
    return new Promise<void>((resolve, reject) => {
      const waiter: Waiter = {
        resolve,
        timer: setTimeout(() => {
          const i = this.waiters.indexOf(waiter);
          if (i >= 0) this.waiters.splice(i, 1);
          reject(new Error('pool exhausted: acquire timed out'));
        }, this.timeoutMs),
      };
      this.waiters.push(waiter);
    });
  }

  async acquire(): Promise<Conn> {
    await this.takePermit();
    const reused = this.idle.pop();
    if (reused) return reused;
    this.created++;
    const id = this.created;
    await sleep(CREATE_MS);
    return new Conn(id);
  }

  release(conn: Conn): void {
    this.idle.push(conn);
    const w = this.waiters.shift();
    if (w) { clearTimeout(w.timer); w.resolve(); }
    else this.permits++;
  }
}

async function main(): Promise<void> {
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

main().catch((e: Error) => { console.error(e.message); process.exit(1); });
