#!/usr/bin/env bash
# Build a self-contained lab bundle: ONE lab, ONE language, labkit VENDORED in
# (no registries, no monorepo paths). The generated devcontainer is tuned for a
# FAST Codespace start:
#   - a prebuilt language image (toolchain already baked — no feature compile)
#   - Postgres/Redis only when the lab actually needs them (none for stdlib labs)
#   - dependency installs in onCreateCommand so a repo prebuild caches them
#
#   ./tooling/scaffold-lab.sh <lab-slug> <language> [out-dir]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAB="${1:?lab slug required}"
LANG_ID="${2:?language required}"
OUT_BASE="${3:-$ROOT/dist}"
SRC="$ROOT/labs/$LAB"
[ -d "$SRC/$LANG_ID" ] || { echo "no $LANG_ID/ in $LAB"; exit 1; }

OUT="$OUT_BASE/$LAB-$LANG_ID"
rm -rf "$OUT"; mkdir -p "$OUT/.devcontainer"

NEEDS_INFRA=$(python3 -c "import json; print('1' if json.load(open('$SRC/lab.json')).get('infra') else '0')")

# Prebuilt image with the toolchain baked in (fast — no devcontainer features).
case "$LANG_ID" in
  python)               IMAGE="mcr.microsoft.com/devcontainers/python:3.11";        ONCREATE="pip install -r requirements.txt" ;;
  go)                   IMAGE="mcr.microsoft.com/devcontainers/go:1.22";            ONCREATE="go build ./... || true" ;;
  javascript|typescript)IMAGE="mcr.microsoft.com/devcontainers/javascript-node:22"; ONCREATE="npm install" ;;
  ruby)                 IMAGE="mcr.microsoft.com/devcontainers/ruby:3.2";           ONCREATE="sudo apt-get update && sudo apt-get install -y libpq-dev && gem install --no-document pg redis" ;;
  rust)                 IMAGE="mcr.microsoft.com/devcontainers/rust:1";             ONCREATE="cargo build" ;;
  c|cpp)                IMAGE="mcr.microsoft.com/devcontainers/cpp:ubuntu";         ONCREATE="sudo apt-get update && sudo apt-get install -y libpq-dev" ;;
esac
# Stdlib labs (infra=0) use no labkit and no deps — keep their bundle minimal so
# the Codespace just opens the language image and runs.
[ "$NEEDS_INFRA" = "1" ] || ONCREATE="true"

# Lab content.
cp "$SRC/README.md" "$SRC/lab.json" "$OUT/" 2>/dev/null || true
cp -r "$SRC/$LANG_ID/." "$OUT/"
rm -rf "$OUT/__pycache__" "$OUT/target" "$OUT/node_modules"

# Vendor labkit only for labs that use it (infra labs).
if [ "$NEEDS_INFRA" = "1" ]; then
  case "$LANG_ID" in
    python)               cp -r "$ROOT/tooling/python/labkit/labkit" "$OUT/labkit"; printf 'psycopg[binary]>=3.1\nredis>=5.0\n' > "$OUT/requirements.txt" ;;
    javascript|typescript)mkdir -p "$OUT/labkit"; cp "$ROOT/tooling/node/labkit/index.js" "$OUT/labkit/index.js"; printf '{\n  "name": "%s-%s",\n  "private": true,\n  "dependencies": { "pg": "^8.11.0", "redis": "^4.6.0" }\n}\n' "$LAB" "$LANG_ID" > "$OUT/package.json"; sed -i "s#['\"]\.\./\.\./\.\./tooling/node/labkit['\"]#'./labkit'#g" "$OUT"/*.js "$OUT"/*.ts 2>/dev/null || true ;;
    go)                   cp -r "$ROOT/tooling/go/labkit" "$OUT/labkit"; sed -i 's#replace labkit => .*#replace labkit => ./labkit#' "$OUT/go.mod" ;;
    rust)                 cp -r "$ROOT/tooling/rust/labkit" "$OUT/labkit"; sed -i 's#path = "\.\./\.\./\.\./tooling/rust/labkit"#path = "./labkit"#' "$OUT/Cargo.toml" ;;
    ruby)                 cp "$ROOT/tooling/ruby/labkit/lib/labkit.rb" "$OUT/labkit.rb"; mkdir -p "$OUT/labkit"; cp "$ROOT/tooling/ruby/labkit/lib/labkit/version.rb" "$OUT/labkit/version.rb"; sed -i "s#require_relative '\.\./\.\./\.\./tooling/ruby/labkit/lib/labkit'#require_relative './labkit'#g" "$OUT"/*.rb ;;
    c|cpp)                cp "$ROOT/tooling/c/labkit.h" "$ROOT/tooling/c/labkit.c" "$OUT/" ;;
  esac
fi

# Devcontainer: image-based (fast) for stdlib labs; compose with services only
# when the lab needs Postgres/Redis.
if [ "$NEEDS_INFRA" = "1" ]; then
  cat > "$OUT/.devcontainer/devcontainer.json" <<JSON
{
  "name": "$LAB ($LANG_ID)",
  "dockerComposeFile": "docker-compose.yml",
  "service": "workspace",
  "workspaceFolder": "/workspaces/\${localWorkspaceFolderBasename}",
  "onCreateCommand": "$ONCREATE",
  "remoteUser": "vscode"
}
JSON
  cat > "$OUT/.devcontainer/docker-compose.yml" <<YAML
services:
  workspace:
    image: $IMAGE
    command: sleep infinity
    volumes:
      - ../..:/workspaces:cached
    environment:
      DATABASE_URL: postgresql://labs:labs@postgres:5432/labs
      REDIS_URL: redis://redis:6379/0
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
  postgres:
    image: postgres:16-alpine
    environment: { POSTGRES_USER: labs, POSTGRES_PASSWORD: labs, POSTGRES_DB: labs }
    volumes: [ "./seed:/docker-entrypoint-initdb.d:ro" ]
    healthcheck: { test: ["CMD-SHELL", "pg_isready -U labs -d labs"], interval: 2s, timeout: 3s, retries: 30 }
  redis:
    image: redis:7-alpine
    command: ["redis-server", "--save", "", "--appendonly", "no"]
    healthcheck: { test: ["CMD", "redis-cli", "ping"], interval: 2s, timeout: 3s, retries: 30 }
YAML
  [ -d "$ROOT/.devcontainer/seed" ] && cp -r "$ROOT/.devcontainer/seed" "$OUT/.devcontainer/seed"
else
  cat > "$OUT/.devcontainer/devcontainer.json" <<JSON
{
  "name": "$LAB ($LANG_ID)",
  "image": "$IMAGE",
  "onCreateCommand": "$ONCREATE",
  "remoteUser": "vscode"
}
JSON
fi

# Enable a repo prebuild (the biggest start-time win) on push to the default branch.
mkdir -p "$OUT/.github"
cat > "$OUT/.github/codespaces-prebuild.md" <<'MD'
Turn on a prebuild so Codespaces start in seconds:
Repo Settings → Codespaces → Set up prebuild → branch: default, region: your users'.
The prebuild bakes the image + onCreateCommand (deps) so creation skips them.
MD

echo "Bundle ready: $OUT  (infra=$NEEDS_INFRA, image=$IMAGE)"
