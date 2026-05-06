#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

HOUSESPREDICT_DISABLE_INTERPRET_VISUAL=1 \
PYTHONPATH="$ROOT_DIR/pipeline/src${PYTHONPATH:+:$PYTHONPATH}" \
  .venv/bin/python -c 'from praha_predictor.bootstrap import prepare_runtime; prepare_runtime(); from praha_predictor.cli import run_train; raise SystemExit(run_train())'
