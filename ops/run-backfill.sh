#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

PYTHONPATH="$ROOT_DIR/pipeline/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m praha_predictor.cli backfill \
  --target-curated-rows "${TARGET_CURATED_ROWS:-1000}" \
  --max-rounds "${BACKFILL_MAX_ROUNDS:-10}" \
  --max-listings-per-source "${MAX_LISTINGS_PER_SOURCE:-250}"
PYTHONPATH="$ROOT_DIR/pipeline/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m praha_predictor.cli status
"$ROOT_DIR/ops/publish-cloudflare.sh"
