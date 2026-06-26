# Hash Tables

## Problem

The hash table is the data structure that feels like cheating. You hand it a key — a string, a user ID, anything — and it hands you back the value in O(1) time. Not O(log N) like a tree. Not O(N) like a scan. *Constant* time, the same whether the table holds ten items or ten billion. It's the structure behind your language's dictionary, behind Redis, behind the database's join, behind the router's forwarding table. If arrays are the foundation of all data structures, hash tables are the foundation of all *fast lookups*.

And yet "O(1)" hides a small lie of its own — a different lie than the one arrays told. Arrays told you the *address* was free and forgot to mention the *fetch*. Hash tables tell you lookup is constant time and forget to mention three things: that it's *average-case* constant, that the average rests on an assumption (your hash function spreads keys evenly) that an *adversary can deliberately break*, and that the worst case is a quiet collapse all the way down to O(N) — a full linear scan — at which point the structure you trusted to be instant becomes the slowest thing in your request path.

Here's the shape of how that bites in production. In 2011, researchers showed you could take down a web server — PHP, Java, Python, Ruby, ASP.NET, all of them — by sending a single HTTP POST with a few thousand form fields whose names were *chosen so they all hash to the same bucket* (CVE-2011-4885, CVE-2012-1150, the "hashDoS" family). The server dutifully parsed the form into a hash table, every key collided, every insert walked the growing chain, and an O(1)-per-key parse became O(N²) total. A few kilobytes of request burned minutes of CPU. The data structure didn't malfunction. It did exactly what it was designed to do — *under an input distribution its designers never expected an attacker to control.*

So the real problem of this chapter isn't "how do I use a dictionary." You use them every day. The problem is understanding the machine well enough to know *exactly* when the O(1) promise holds, why it holds, what makes it break, and what every serious implementation — Python's `dict`, Go's `map`, Google's Swiss tables, Java's `HashMap` — actually does under the hood to keep the promise honest. By the end, the hash table will stop being magic and become a thing you can reason about, tune, and defend.

## Why It Matters (Latency, Throughput, Cost)

**It's the difference between O(1) and O(N), and that difference is everywhere.** The single most common accidental-quadratic bug in production code is checking membership against a list instead of a set:

```
x in some_list      →  O(N) linear scan, every time
x in some_set        →  O(1) hash lookup
```

Wrap that `x in some_list` in a loop over N items and you've built an O(N²) machine by accident. At N=1,000 it's invisible. At N=1,000,000 it's a 20-minute job that should take a tenth of a second. Half of all "why did this batch job suddenly get slow as data grew" mysteries are exactly this — a hash table that should have been there and wasn't.

**It's how databases join.** When PostgreSQL joins two tables, one of its three strategies is the *hash join*: build a hash table on the smaller table's join key in memory, then stream the larger table past it, probing for matches. This turns an O(N×M) nested-loop join into O(N+M). The entire viability of joining a billion-row fact table against a dimension table rests on hash-table lookups being O(1). When the build side doesn't fit in `work_mem`, the hash table *spills to disk* in partitions — and now you're paying the random-I/O tax, which is why "increase work_mem" is one of the highest-leverage Postgres knobs for analytical queries.

**It's how the internet shards data.** When you have more data than one machine can hold, you split it across N machines by *hashing the key* and assigning it to `hash(key) % N`. Redis Cluster, Cassandra, DynamoDB, every CDN, every sharded database does a version of this. The naïve `% N` has a catastrophic flaw (add one machine and almost every key moves), which is why *consistent hashing* exists — but the root primitive is still: hash the key, the hash decides where it lives. Get the hash distribution wrong and one shard gets 10× the traffic ("hot partition") while the others idle, and your whole cluster's throughput is capped by its unluckiest node.

**It's a memory-vs-speed trade you're always paying.** A hash table buys O(1) lookup with *empty space*. To keep collisions rare you must keep the table maybe 50–70% full — meaning 30–50% of your allocated buckets sit empty, deliberately. That wasted space is the price of speed. A hash set of one million 8-byte integers can easily use 30–50 MB, several times the 8 MB the raw integers would occupy. When you're tuning a memory-bound service, "how full do I dare run my hash tables" is a real dial with a real cost.

## Mental Model

Start from a fantasy and then ruin it productively. That's the cleanest way to *feel* what a hash table is.

**The fantasy: direct addressing.** Suppose every key were a small integer — say, user IDs from 0 to 999. Then you don't need anything clever at all. Make an array of 1,000 slots and store user `i`'s data at `arr[i]`. Lookup is a single array index: genuinely O(1), the array-chapter kind of O(1), no tricks. This is *direct addressing*, and it's perfect... right up until your keys are `"alice@example.com"`, or 64-bit user IDs, or arbitrary strings. Now the "array indexed by the key" would need 2⁶⁴ slots, or infinitely many. The fantasy needs a billion-petabyte array. Dead on arrival.

**The hash function is the bridge back to the fantasy.** What if we had a function that *crushes* any key — string, big integer, anything — into a small integer in `[0, M)`, where M is the size of an array we can actually afford?

```
   "alice@example.com"  ──hash──►  3,471,920,... ──% M──►  bucket 5
   "bob@example.com"    ──hash──►  9,127,003,... ──% M──►  bucket 2
   user_id 8675309      ──hash──►  ...           ──% M──►  bucket 5   ← uh oh
```

Now we're back in the array fantasy: compute `bucket = hash(key) % M`, and `arr[bucket]` is where the value lives. One hash, one modulo, one array access — O(1). *That's the whole idea.* A hash table is direct addressing where a hash function manufactures the index.

**The catch that defines everything else: collisions are not a bug, they are inevitable.** Look at the diagram — `"alice"` and `user_id 8675309` both landed in bucket 5. They had to. We're mapping a huge key-space into a tiny M-slot array, so by the **pigeonhole principle**, *different keys must sometimes share a bucket.* You cannot hash your way out of this; you can only manage it. Every design decision in the rest of this chapter — chaining vs. open addressing, load factor, probe sequences, when to resize — is, at its heart, **a different answer to one question: "what do we do when two keys want the same bucket?"**

So hold these three sentences:
1. A hash table is an array you index by `hash(key) % M` instead of by the key itself.
2. Squeezing a big key-space into M buckets *guarantees* collisions (pigeonhole).
3. Everything else is collision management — and the quality of that management is the entire difference between reliable O(1) and the O(N) collapse that took down those web servers.

## Underlying Theory

We'll build it in layers again — each one adds a piece of the real machine and explains failures the previous layer couldn't.

### Layer 1 — What a hash function actually has to do

A hash function for a hash table has exactly one job that matters for *correctness* and one that matters for *speed*. For correctness: it must be **deterministic** — the same key always hashes to the same value, or you'd never find what you stored. For speed: it must **scatter keys uniformly** across `[0, M)`, so that buckets fill evenly and collisions stay rare.

"Uniformly" is the whole game, and the property that delivers it is the **avalanche effect**: flipping a single bit of the input should flip about half the bits of the output, unpredictably. Why does that matter? Because real keys are *not* random — they're `user_1`, `user_2`, `user_3`; they're sequential IDs, similar URLs, timestamps clustered in a range. If your hash function let that structure survive (imagine `hash(s) = s.length`, or `hash(n) = n`), then similar keys would land in similar or identical buckets and your "uniform" assumption is dead before it started. A good hash function takes the clumpy, structured, adversarial mess of real keys and *smears* it into something that looks uniformly random over the buckets.

```
Bad hash (structure survives):        Good hash (avalanche):
  user_1 → bucket 1                     user_1 → bucket 7
  user_2 → bucket 2                     user_2 → bucket 0
  user_3 → bucket 3                     user_3 → bucket 4
  ...clusters, predictable               ...scattered, unpredictable
```

This is why you don't roll your own. Production hashes — FNV and MurmurHash for speed, **SipHash** for security, xxHash and Google's CityHash/FarmHash for raw throughput, AES-NI-accelerated hashing where the CPU supports it — are carefully engineered to avalanche fast. We'll see in Layer 7 why "deterministic" and "secure" are in tension, and how randomized seeds resolve it.

### Layer 2 — Just how inevitable are collisions? (the birthday paradox)

Pigeonhole says collisions happen once you have more keys than buckets. But the truth is far more aggressive than that, and it's worth internalizing because it explains why hash tables run *half empty.*

Ask the famous question: in a room of just 23 people, what's the chance two share a birthday? Intuition says low — 23 people, 365 days, surely rare. The answer is **over 50%.** The reason is that collisions are about *pairs*, and 23 people make 253 pairs. Collisions scale with pairs, not with items.

Translate that to hashing: you start getting collisions not when the table is full, but when the number of keys approaches **√M**. With a million buckets, you expect your first collision after inserting only about a *thousand* keys — at 0.1% full. Collisions aren't the exception that happens when you're careless and let the table fill up. They are the *common case*, arriving almost immediately, which is exactly why a hash table cannot be just "a hash function plus an array." It *must* have a collision-resolution strategy baked in from the first insert. The birthday paradox is the mathematical reason the rest of this chapter exists.

### Layer 3 — The two answers: chaining vs. open addressing

When two keys want bucket 5, you have two fundamentally different philosophies.

**Separate chaining: let the bucket hold a list.** Each bucket isn't a single slot but the head of a linked list (or small dynamic array) of all entries that hashed there. Collision? Just append to that bucket's list. Lookup? Hash to the bucket, then walk its short list comparing keys.

```
Chaining:                                  
  bucket[2] → ("bob",7)                     
  bucket[5] → ("alice",1) → (8675309,"x")  ← two keys share bucket 5, chained
  bucket[7] → ("carol",9)                   
```

Simple, robust, degrades gracefully — but notice the ghost of the arrays chapter haunting it: **that linked list is a pointer chase.** Each node lives wherever the allocator put it, so walking a chain is a cache miss per hop. As long as chains are short (good hash, low load), you rarely walk more than one node and it's fine. But the structure has the linked-list disease baked in.

**Open addressing: keep everything in the array itself.** No lists, no pointers, no separate allocations. If bucket 5 is taken, you *probe* — follow a deterministic rule to find the next open slot and put the entry there. To look up a key, you hash to bucket 5, and if that's not your key, you follow the same probe rule, checking slot after slot until you find your key or hit an empty slot (which means "not here").

```
Open addressing (linear probing):
  insert ("alice",1): hash→5, slot 5 empty → place at 5
  insert (8675309):    hash→5, slot 5 taken → probe to 6, empty → place at 6
  
  index:  4      5             6           7
        [ ... ][("alice",1)][(8675309)][ ... ]
                  ▲ collision resolved by walking forward in the SAME array
```

And here's why open addressing has *won* in modern high-performance implementations (Python, Go, Rust, Swiss tables all use it): **the probe sequence walks contiguous memory.** Probing to the next slot is just `index + 1` — the next bytes in the array, almost certainly *in the same cache line you already loaded.* You get the arrays-chapter free lunch: spatial locality, prefetching, no pointer chasing, no per-entry allocation. Chaining's chain is a guaranteed cache miss; open addressing's probe is usually a free cache hit. That single difference — does collision resolution chase pointers or walk an array? — is most of why one approach is fast on 2020s hardware and the other is "fine."

The catch open addressing pays for this: it's far more sensitive to load factor (a nearly-full table makes probe sequences long), and deletion is genuinely tricky — which is Layer 5.

### Layer 4 — Load factor: the dial that governs everything

The **load factor** α = (number of entries) / (number of buckets) is the single most important number in a hash table's life. It is the knob that trades memory for speed, and it directly sets your collision rate.

For chaining, the expected chain length is exactly α, so average lookup is O(1 + α) — fine as long as α stays bounded. For open addressing, the math is more violent: the expected number of probes for an unsuccessful lookup is roughly **1 / (1 − α)**. Read that formula closely, because it's a cliff:

```
  load factor α     avg probes (open addressing, 1/(1−α))
  ─────────────────────────────────────────────────────
  0.50              2          ← comfortable
  0.75              4          ← getting warm
  0.90              10         ← hurting
  0.95              20         ← falling off the cliff
  0.99              100        ← effectively O(N)
```

This is why hash tables *deliberately run half-to-two-thirds empty.* Python's `dict` resizes to stay under 2/3. Go's `map` targets ~6.5 entries per bucket (it uses a hybrid bucket scheme). Swiss tables run up to ~87.5%. The empty space isn't waste — it's the headroom that keeps probe sequences short and the O(1) promise honest. Run the table hotter and you're choosing a slower table to save memory; that's a legitimate choice, but it *is* a choice.

**Resizing: the amortized-doubling trick, again.** When the load factor crosses the threshold, the table *grows* — allocate a new array (typically 2× the buckets), then **rehash every existing entry** into the new array (because `hash(key) % M` changes when M changes). That rehash is O(N). Sound familiar? It's the exact dynamic-array doubling argument from the arrays chapter: occasional O(N) resizes, paid off by the cheap inserts between them, giving **amortized O(1) per insert.** The banker's argument transfers wholesale. One consequence worth knowing: a single unlucky insert — the one that triggers the resize — can take O(N) and cause a latency spike. For latency-sensitive systems this matters, which is why some implementations (and databases) do *incremental* rehashing, moving a few entries per operation to smooth the spike out. Redis does exactly this: it keeps two tables during a resize and migrates buckets gradually so no single command stalls.

### Layer 5 — Probe sequences and the tombstone problem

Inside open addressing, *how* you probe is a real design space, and each choice trades clustering against cache behavior.

**Linear probing** (`try slot+1, slot+2, ...`) has the best cache behavior — you're walking contiguous memory — but suffers **primary clustering**: occupied slots tend to clump into long runs, and any key hashing anywhere into a run has to traverse the whole run. **Quadratic probing** (`slot+1, slot+4, slot+9, ...`) spreads probes out to break up clusters, at the cost of worse locality. **Double hashing** uses a second hash function to determine the step size, scattering probes the most, with the least clustering but the worst cache behavior. On modern hardware, linear probing's cache-friendliness usually wins despite its clustering — which is why it's the default in most fast implementations, often with a clever twist like Robin Hood hashing.

**Robin Hood hashing** is a beautiful idea: when inserting, if you reach a slot occupied by an entry that is *closer to its ideal bucket than you are to yours*, you evict it and take the slot, then go re-insert the evicted one. "Steal from the rich (entries near home), give to the poor (entries that have probed far)." The effect is that probe distances become very *uniform* — no entry is wildly far from home — which tames the variance and keeps the worst-case lookup short. Rust's old `HashMap` and many high-performance tables use it.

**The tombstone problem — why deletion is the hard part.** In open addressing, you cannot simply blank out a deleted slot. Why? Because lookups stop at the first empty slot. Picture: `alice` hashed to 5 but probed to 6 because 5 was taken by `bob`. Now you delete `bob` and blank slot 5. Next time you look up `alice`, you hash to 5, see it's *empty*, and conclude "alice isn't here" — even though she's sitting right there in slot 6. You've broken the table by deleting. The fix is a **tombstone**: mark the slot "deleted-but-not-empty" so lookups *probe past* it but inserts can *reuse* it. Tombstones solve correctness but accumulate — a table that's seen many deletes fills with tombstones, lengthening probe sequences as if it were full, until a rehash sweeps them away. This is why "lots of inserts and deletes" workloads sometimes need periodic rehashing, and why deletion-heavy hash tables can mysteriously slow down over time even at low *live* load factor. It's the tombstones.

### Layer 6 — How the real implementations actually do it

Theory meets the standard library. Knowing these makes you fluent in the trade-offs your runtime already made for you.

- **Python `dict`** (since 3.6) is a marvel of compactness. It splits into two arrays: a dense, insertion-ordered array of `(hash, key, value)` entries, and a sparse array of *indices* into that dense array. Open addressing happens in the sparse index array; the actual entries stay packed. This is why modern Python dicts **preserve insertion order** (a side effect that became a language guarantee in 3.7) *and* use ~30% less memory than the old design. Load factor capped at 2/3.
- **Go `map`** uses open addressing with a twist: buckets hold **8 key/value pairs each**, plus an array of the top 8 bits of each key's hash. A lookup first scans those 8 one-byte hash fragments — 8 cheap comparisons in a cache line — and only does a full key comparison on a fragment match. Overflow buckets chain when a bucket's 8 slots fill. It also *randomizes iteration order on purpose* so you can't accidentally depend on it.
- **Swiss tables** (Google's `absl::flat_hash_map`, and the basis of Rust's current `hashbrown`/`HashMap`) take Go's idea and weaponize SIMD. They keep a separate array of one-byte "control" tags (7 bits of hash + 1 occupancy bit) and use a single **SIMD instruction to compare 16 control bytes at once**, finding candidate matches in a 16-slot group in essentially one operation. Combined with open addressing and high load factors (~87.5%), this is roughly the state of the art for in-memory hash tables — pure arrays-chapter thinking (contiguity + SIMD + cache lines) applied to hashing.
- **Java `HashMap`** uses chaining — but with a safety net against the hashDoS attack of Layer 7: when a single bucket's chain grows past 8 entries *and* the table is large enough, it **"treeifies"** that bucket, converting the linked list into a red-black tree. That caps a degenerate bucket at O(log N) instead of O(N), turning a potential O(N²) collision attack into a merely-O(N log N) annoyance. A data-structure inside a data-structure, purely for adversarial robustness.

Notice the pattern: every fast modern table is *open addressing + small contiguous groups + (often) SIMD over a compact tag array.* They're all reaching for the same arrays-chapter free lunch.

### Layer 7 — Hash flooding: when the input is an adversary

Back to the attack from the opening, now that we have the machinery to understand it precisely. The O(1) promise rests on *uniform* distribution of keys across buckets. But for a *deterministic, public* hash function, an attacker can run it offline, find thousands of distinct strings that all hash to the same bucket, and send them as your input — HTTP headers, JSON keys, form fields, query parameters, anything you'll funnel into a hash table. Every key collides, every insert/lookup walks the full chain (or a giant probe run), and your O(1) table degrades to O(N) per operation, O(N²) to build. CPU pegs at 100%, the server stops responding, and the attacker spent a few kilobytes to do it. That's hashDoS (CVE-2011-4885 and the whole 2011–2012 family).

The defenses, in layers:

1. **Randomized seed (the primary fix).** Make the hash function depend on a secret random seed chosen at process startup. Now `hash` is still deterministic *within* a process (so lookups work) but *unpredictable across* processes — the attacker can't precompute colliding keys because they don't know your seed. This is what `PYTHONHASHSEED` controls; it's on by default in Python 3, Node.js, Ruby, and others.
2. **A cryptographically strong hash: SipHash.** A random seed only helps if the attacker can't *reverse* the function to find collisions even without knowing the seed up front. SipHash (Aumasson & Bernstein, 2012) is a keyed pseudorandom function designed to be fast enough for hash tables yet strong enough that you can't find collisions without the key. Python, Rust, and others adopted it as the default string hash *specifically* in response to hashDoS. It's a little slower than MurmurHash — that's the price of not being DoS-able.
3. **Cap the damage structurally.** Java's treeify (Layer 6) is the belt-and-suspenders move: even if collisions happen, no single bucket can go worse than O(log N).

The lesson generalizes far beyond hashing: **any data structure whose performance depends on input distribution is a denial-of-service surface the moment an attacker controls the input.** (The same is true of naïve quicksort — see the sorting chapter.) "Average case O(1)" is a statement about a *random* adversary. A real adversary is not random.

### Layer 8 — Hashing at scale: partitioning and consistent hashing (the principal view)

Zoom out from one machine to a fleet. The same primitive — `hash(key)` decides where data lives — is how distributed systems shard. But the naïve version has a fatal flaw.

**Why `hash(key) % N` breaks.** Shard a key to machine `hash(key) % N`. It works beautifully — until you add or remove a machine. Go from N=4 to N=5 and the modulus changes for *almost every key*: nearly the entire dataset has to move to a new shard at once. For a cache that means a near-total miss storm; for a database it means a massive, dangerous rebalance. The structure is too brittle to scale elastically.

**Consistent hashing** fixes this with an idea worth carrying in your head forever: hash *both the keys and the machines* onto the same circular space (a "ring", say `[0, 2³²)`). A key belongs to the first machine you meet walking clockwise from the key's position. Now when you add a machine, it inserts at one point on the ring and steals keys *only from its immediate clockwise neighbor* — roughly 1/N of the keys move, not all of them. Remove a machine and its keys flow to the next one clockwise; everyone else is untouched.

```
        machineB
           ●
      key1●   ●key2          a key maps to the next machine clockwise.
    ●               ●        add machineD → only the arc between C and D's
 machineA          machineC  insertion point reassigns. ~1/N keys move, not all.
      ●           ●
       key3 ● ● machineD
```

Real systems add **virtual nodes** (each physical machine occupies many points on the ring) so load spreads evenly and removing a node redistributes its share across *many* others rather than dumping it all on one neighbor. This is the backbone of Amazon DynamoDB, Cassandra, Riak, Redis Cluster's slot mapping (a discretized cousin), and most CDN request routing. The whole edifice is still "hash the key to decide where it lives" — just arranged so the mapping is *stable* under membership changes.

And the in-memory hashing ideas come full circle here too: a **hot partition** (one shard getting disproportionate traffic) is just the distributed version of a long chain — the failure mode of a non-uniform hash, scaled up to a datacenter. Whether the cost is "a long probe sequence" or "one server melting while the others idle," the root cause is identical: keys that didn't spread evenly.

## A Ladder From L1 to Principal

Same structure, climbing altitude:

- **L1 / new grad:** A hash table is average O(1) get/put/delete; worst case O(N). You always use a set/dict for membership instead of scanning a list, and you know collisions exist.
- **L3–L4 / solid engineer:** You understand load factor, why tables resize (amortized doubling), and chaining vs. open addressing. You can explain why `x in set` beats `x in list` and you spot accidental-O(N²) membership checks in review.
- **Senior:** You reason about probe sequences, cache behavior (why open addressing is fast), tombstones, and you know your language's actual implementation — Python's ordered compact dict, Go's bucketed map. You've debugged a hash-distribution or resize-latency issue.
- **Staff:** You connect hashing to systems — hash joins and `work_mem` spills, hash flooding as a security surface, why a hot partition forms, when to pick a hash index vs. a B-tree.
- **Principal:** You design the hashing strategy for systems that must scale and stay up under adversarial load — consistent hashing with virtual nodes, hash-function choice (speed vs. SipHash-grade safety), incremental rehashing for tail latency, partition schemes that won't produce hot shards. You treat "performance depends on input distribution" as a security property, not just a performance one.

It's the same handful of ideas — hash to an index, collisions are inevitable, manage them, keep the load factor sane — reaching from a one-line `dict` lookup all the way to the architecture of a globally distributed database.

## Complexity Analysis

| Operation | Average | Worst | Why — and the part the table doesn't tell you |
|-----------|---------|-------|-----------------------------------------------|
| `get(key)` | O(1) | O(N) | Average assumes uniform spread; worst = all keys in one bucket (bad hash, or an adversary). |
| `put(key)` | O(1) amortized | O(N) | Amortized over resizes (doubling, like dynamic arrays); the resize itself is O(N) and can spike latency. |
| `delete(key)` | O(1) | O(N) | Open addressing needs **tombstones**; they accumulate and silently lengthen probes until a rehash. |
| Iteration | O(N) | O(N + empty slots) | You walk *buckets*, including empty ones — a sparsely-filled large table iterates slower than its entry count suggests. |
| Resize / rehash | O(N) | O(N) | Triggered at the load-factor threshold; rehashes every entry. Incremental rehashing spreads this out. |

The honest summary: a hash table is O(1) **when the hash is good and the load factor is bounded** — two conditions the table cannot guarantee on its own. The worst-case column is not theoretical trivia; it's the column an attacker aims for.

## War Stories (the shape of the bug in the wild)

- **The kilobyte that pegged the CPU.** A few thousand carefully-named form fields, all colliding into one bucket, turned form parsing into O(N²) and DoS'd a fleet of web servers (hashDoS, 2011). Fix: randomized seeds + SipHash. The data structure worked perfectly; the *assumption* was the vulnerability.
- **The accidental quadratic.** A nightly job did `if record.id in processed_list` inside a loop over millions of records. Fine in testing (small data), 40 minutes in production. One-character fix: make `processed_list` a `set`. Runtime: 8 seconds.
- **The deletes that slowed a table down.** A long-lived open-addressing cache with heavy churn got steadily slower despite a stable number of live entries. Cause: tombstone accumulation lengthening probe sequences. Fix: periodic rehash to sweep tombstones.
- **The hot shard.** A sharded system keyed on `customer_id % N` melted one node while the rest idled, because a handful of whale customers generated most of the traffic and happened to share a shard. The hash was uniform over *keys* but the *traffic* per key wasn't — a reminder that "uniform key distribution" and "uniform load" are different promises.

## Key Takeaways

1. **A hash table is direct addressing made affordable** — an array you index by `hash(key) % M`. That one move buys O(1) lookup over arbitrary keys.
2. **Collisions are inevitable and arrive early** (pigeonhole + birthday paradox: first collisions near √M keys, not at "full"). Every design choice in a hash table is an answer to "what do we do on a collision?"
3. **Load factor is the master dial.** Open addressing's probe count is ~1/(1−α) — a cliff after ~0.9 — which is *why* good tables run 50–87% full and resize (amortized-doubling, straight from the arrays chapter) to stay there.
4. **Open addressing beats chaining on modern hardware for the arrays-chapter reason:** probing walks contiguous, cache-resident, prefetchable memory, while a chain is a guaranteed pointer-chase. Swiss tables push this to SIMD comparisons over a compact tag array.
5. **Deletion in open addressing needs tombstones**, and tombstones accumulate — a delete-heavy table can slow down even at low live load until it's rehashed.
6. **"Average-case O(1)" assumes a non-adversarial input.** A deterministic public hash is a DoS surface (hashDoS); defend with randomized seeds, SipHash-grade functions, and structural caps like Java's treeify. Performance-depends-on-input is a *security* property.
7. **The same primitive scales to the datacenter:** sharding hashes keys to machines, naïve `% N` is too brittle, and **consistent hashing** (with virtual nodes) makes the mapping stable under membership changes. A hot partition is just a long collision chain at fleet scale.

## Related Modules

- `01-arrays-and-memory-layout.md` — the contiguity/cache-line/prefetch arguments that make open addressing fast and the amortized-doubling argument behind resizing both originate here
- `03-trees-and-indexing.md` — the other great lookup structure; hash index (O(1) point lookup, no ordering) vs. B-tree (O(log N), but ordered → range scans). Knowing *why you'd pick each* is the payoff of reading both
- `05-sorting-and-searching.md` — hash joins vs. sort-merge joins; and quicksort's adversarial-input DoS is the exact same "performance depends on input distribution" vulnerability as hashDoS
- `../01-mathematics-for-systems/02-probability-for-engineers.md` — the birthday paradox, balls-into-bins, and the probability of the worst-case bucket length made rigorous
- `../06-databases/02-indexing.md` — hash indexes, hash joins, `work_mem` and on-disk hash spills, and partition/sharding strategy in real database engines
- `../08-systems-design/` — consistent hashing, virtual nodes, and hot-partition avoidance as first-class distributed-systems design concerns
