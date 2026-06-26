# Migration: one repo per lab + labkit from package registries

This monorepo is the **source of truth** for lab content. For delivery, each lab
ships as its **own GitHub repo** so a learner's Codespace contains only the lab
they clicked — nothing else. The shared `labkit` platform shim is consumed from
a **package registry** per language (vendored for C/C++, which have none).

The website is already wired for this: the "Start Lab" button opens
`https://codespaces.new/visheshrwl/<lab-slug>?devcontainer_path=.devcontainer/<config>/devcontainer.json`,
where `<config>` is the selected language's container (python, go, node, ruby,
rust, c-cpp). It launches only that lab repo, only that language's container.

---

## 1. Per-lab repo layout

Each lab repo (`visheshrwl/lab-03-cache-aside`, etc.) contains:

```
lab.json  README.md
.devcontainer/{python,go,node,ruby,rust,c-cpp}/devcontainer.json   # copied from this monorepo
.devcontainer/docker-compose.yml  .devcontainer/seed/              # copied (infra labs)
python/      {solution.py, stub.py, test_lab.py, requirements.txt}
go/          {go.mod, stub.go, solution/solution.go}
javascript/  {solution.js, stub.js, package.json}
typescript/  {solution.ts, stub.ts, package.json}
ruby/        {solution.rb, stub.rb, Gemfile}
rust/        {Cargo.toml, solution.rs, stub.rs}
c/           {solution.c, stub.c, labkit.h, labkit.c}     # labkit VENDORED
cpp/         {solution.cpp, stub.cpp, labkit.h, labkit.c} # labkit VENDORED
```

Copy `.devcontainer/` + `docker-compose.yml` (+ `seed/` for infra labs) from this
monorepo into every lab repo unchanged.

---

## 2. Publish the labkit packages (once)

Pick names you own on each registry (examples below use `vr-labkit`). The
**import name stays `labkit`** wherever the registry allows it.

| Ecosystem | Source in monorepo | Publish | Lab dependency |
|---|---|---|---|
| Python / PyPI | `tooling/python/labkit` | `python -m build && twine upload dist/*` | `requirements.txt`: `vr-labkit` (import stays `labkit`) |
| Node / npm | `tooling/node/labkit` | set name `@visheshrwl/labkit`, `npm publish --access public` | `package.json` dep `@visheshrwl/labkit` |
| Rust / crates.io | `tooling/rust/labkit` | `cargo publish` | `Cargo.toml`: `labkit = "0.1"` |
| Ruby / RubyGems | `tooling/ruby/labkit.rb` | wrap in a gem (`vr-labkit.gemspec`), `gem build && gem push` | `Gemfile`: `gem "vr-labkit"` (provides `require "labkit"`) |
| Go modules | `tooling/go/labkit` | push to its own repo `github.com/visheshrwl/labkit-go`, `git tag v0.1.0` | `go.mod`: `require github.com/visheshrwl/labkit-go v0.1.0` |
| C / C++ | `tooling/c/labkit.{h,c}` | **no registry** — vendor the two files into each lab's `c/` and `cpp/` | `#include "labkit.h"` (compile with the vendored `labkit.c`) |

---

## 3. Per-language code change (relative path → package)

The lab logic is unchanged; only how it reaches `labkit` changes.

- **Python** — *no change*. Already `from labkit import db, cache`. (Verified: the
  lab runs standalone with only the installed package, no `tooling/`.)
- **Node / TS** — `require('../../../tooling/node/labkit')` →
  `require('@visheshrwl/labkit')`; TS `createRequire(...)('@visheshrwl/labkit')`.
  Add a `package.json` per lab with that dependency.
- **Go** — remove the `replace labkit => ../../../tooling/go/labkit` line; set
  `require github.com/visheshrwl/labkit-go v0.1.0`; change `import "labkit"` →
  `import "github.com/visheshrwl/labkit-go"` (kept as package `labkit`).
- **Rust** — `labkit = { path = "../../../tooling/rust/labkit" }` →
  `labkit = "0.1"`. No source change (`use labkit::…`).
- **Ruby** — `require_relative '../../../tooling/ruby/labkit'` → `require 'labkit'`;
  add a `Gemfile`.
- **C / C++** — copy `labkit.h` + `labkit.c` into the lab's `c/`/`cpp/`; the
  compile command drops `-I../../../tooling/c` and compiles the local `labkit.c`.

---

## 4. Per-language devcontainer postCreate (install labkit from the package)

In each lab repo, change the per-language `.devcontainer/<config>/devcontainer.json`
`postCreateCommand` to install from the registry instead of the monorepo path:

- `python`: `pip install -r python/requirements.txt`
- `node`:   `cd javascript && npm install && cd ../typescript && npm install`
- `ruby`:   `sudo apt-get update && sudo apt-get install -y libpq-dev && cd ruby && bundle install`
- `rust`:   `cd rust && cargo fetch`
- `go`:     *(none — `go run` fetches the module)*
- `c-cpp`:  `sudo apt-get update && sudo apt-get install -y build-essential libpq-dev` *(labkit is vendored)*

---

## 5. Create the repos

For each lab: create `visheshrwl/<lab-slug>`, assemble the layout in §1 (a
scaffold script can copy from this monorepo, vendor C/C++ labkit, and apply the
§3 substitutions), commit, push. The website launch URLs then resolve with no
further site changes.

Update `lab.json` with an explicit `"repo": "<lab-slug>"` if the repo name ever
differs from the slug (the site falls back to the slug otherwise).
