#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

./ops/run-scrape.sh
./ops/run-train.sh
./ops/publish-cloudflare.sh
