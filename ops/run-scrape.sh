#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

PYTHONPATH="$ROOT_DIR/pipeline/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m praha_predictor.cli scrape --max-listings-per-source "${MAX_LISTINGS_PER_SOURCE:-250}"
