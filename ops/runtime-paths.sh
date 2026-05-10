#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

if [[ -n "${HOUSESPREDICT_RUNTIME_DIR:-}" ]]; then
  runtime_root="$HOUSESPREDICT_RUNTIME_DIR"
elif [[ "${HOUSESPREDICT_USE_REPO_RUNTIME:-}" =~ ^(1|true|TRUE|yes|YES|on|ON)$ ]]; then
  runtime_root="$ROOT_DIR"
elif [[ "$OSTYPE" == darwin* ]]; then
  runtime_root="$HOME/Library/Application Support/HousesPredict-v2"
else
  runtime_root="$ROOT_DIR"
fi

export HOUSESPREDICT_RUNTIME_DIR="$runtime_root"
export HOUSESPREDICT_DATA_DIR="${HOUSESPREDICT_DATA_DIR:-$HOUSESPREDICT_RUNTIME_DIR/data}"
export HOUSESPREDICT_ARTIFACTS_DIR="${HOUSESPREDICT_ARTIFACTS_DIR:-$HOUSESPREDICT_RUNTIME_DIR/artifacts}"

mkdir -p "$HOUSESPREDICT_DATA_DIR" "$HOUSESPREDICT_ARTIFACTS_DIR"

if [[ -f "$ROOT_DIR/worker-app/public/models/active-model.json" && ! -f "$HOUSESPREDICT_ARTIFACTS_DIR/active-model.json" ]]; then
  cp "$ROOT_DIR/worker-app/public/models/active-model.json" "$HOUSESPREDICT_ARTIFACTS_DIR/active-model.json"
fi

if [[ -f "$ROOT_DIR/worker-app/public/manifests/model-registry.json" && ! -f "$HOUSESPREDICT_ARTIFACTS_DIR/model-registry.json" ]]; then
  cp "$ROOT_DIR/worker-app/public/manifests/model-registry.json" "$HOUSESPREDICT_ARTIFACTS_DIR/model-registry.json"
fi

# Avoid bytecode churn inside the synced repo for routine ops runs.
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
