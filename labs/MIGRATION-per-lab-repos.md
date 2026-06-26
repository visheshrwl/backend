# Delivery: one repo per lab+language, labkit vendored (no registries)

This monorepo is the **source of truth** for lab content and the `labkit`
platform shims (`tooling/<language>/labkit`). For delivery, nothing is published
anywhere — each shippable unit is **one lab in one language with labkit copied
in**, so a learner's Codespace contains exactly what they selected and nothing
else (no other labs, no other languages, no registry dependency).

The website is already wired for this: the "Start Lab" button opens
`https://codespaces.new/visheshrwl/<lab-slug>-<language>?devcontainer_path=.devcontainer/<config>/devcontainer.json`
for the language tab the learner picked.

---

## Build a bundle

```bash
./tooling/scaffold-lab.sh <lab-slug> <language> [out-dir]
# e.g.
./tooling/scaffold-lab.sh lab-03-cache-aside go dist
```

This produces `dist/lab-03-cache-aside-go/` containing:

```
lab.json  README.md
.devcontainer/<config>/devcontainer.json   .devcontainer/docker-compose.yml  .devcontainer/seed/
<the language's solution + stub + test>
labkit/                 # labkit VENDORED for this language (or labkit.h/.c for C/C++)
requirements.txt | package.json | go.mod(+labkit/) | Cargo.toml(+labkit/) | Gemfile-free
```

The lab references labkit by a **local path** (`./labkit`, `replace … => ./labkit`,
`path = "./labkit"`, `#include "labkit.h"`). Verified: each bundle runs against
real Postgres/Redis from an isolated directory — no monorepo, no registry.

| Language | How labkit is vendored | Lab reference |
|---|---|---|
| Python | `labkit/` package copied beside the code | `from labkit import db` (cwd on path) |
| Node (JS/TS) | `labkit/index.js` + lab `package.json` (pg, redis) | `require('./labkit')` |
| Go | `labkit/` module copied | `replace labkit => ./labkit` |
| Rust | `labkit/` crate copied | `path = "./labkit"` |
| Ruby | `labkit.rb` + `labkit/version.rb` copied | `require_relative './labkit'` |
| C / C++ | `labkit.h` + `labkit.c` copied | `#include "labkit.h"` (compile together) |

---

## Create the repos

For every lab × language, make a repo `<lab-slug>-<language>` and push the bundle:

```bash
for lab in lab-01-n-plus-one-profiling lab-02-connection-pool-tuning lab-03-cache-aside; do
  for lng in python go javascript typescript ruby rust c cpp; do
    [ -d "labs/$lab/$lng" ] || continue
    ./tooling/scaffold-lab.sh "$lab" "$lng" dist
    ( cd "dist/$lab-$lng" && git init -q && git add -A && git commit -qm "lab: $lab ($lng)" \
        && gh repo create "visheshrwl/$lab-$lng" --private --source=. --push )
  done
done
```

The website launch URLs then resolve with no further changes. To regenerate
after editing a lab in the monorepo, re-run the scaffold and force-push.

> Prefer **one repo per lab** (all languages in one repo) instead of per
> language? Drop the inner loop, scaffold each language into a `<lang>/`
> subfolder of one repo, and point the launch at `<lab-slug>` — the container
> still installs only the selected language. Per lab+language (above) is the
> strictest "ship only what was selected."
