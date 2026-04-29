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

# Avoid bytecode churn inside the synced repo for routine ops runs.
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
