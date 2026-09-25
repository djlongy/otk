#!/usr/bin/env bash
# Stop the otk lab and delete everything it created: containers, volumes, packs, ledger, store, credentials.
set -euo pipefail
cd "$(dirname "$0")"
docker compose --profile tools down -v --remove-orphans
docker run --rm -v "$PWD:/lab" --entrypoint sh otk:dev -c 'exec find /lab/data /lab/.state /lab/quay/low /lab/quay/high -mindepth 1 -delete' 2>/dev/null || true
echo "lab removed"
