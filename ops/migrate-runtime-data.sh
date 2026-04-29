#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

RUNTIME_WRANGLER_DIR="${HOUSESPREDICT_RUNTIME_DIR}/wrangler-state"
mkdir -p "$RUNTIME_WRANGLER_DIR"

ensure_not_busy() {
  local target="$1"
  if command -v lsof >/dev/null 2>&1; then
    if lsof +D "$target" >/dev/null 2>&1; then
      echo "Refusing to migrate active path: $target" >&2
      echo "Stop running scrape/train/dev processes first." >&2
      exit 1
    fi
  fi
}

migrate_path() {
  local source_path="$1"
  local target_path="$2"
  local source_real
  local target_real

  source_real="$(cd "$(dirname "$source_path")" && pwd -P)/$(basename "$source_path")"
  target_real="$(mkdir -p "$target_path" && cd "$target_path" && pwd -P)"

  if [[ "$source_real" == "$target_real" ]]; then
    echo "Skipping $source_path: source and target are the same path"
    return 0
  fi

  if [[ -L "$source_path" ]]; then
    echo "Skipping $source_path: already a symlink"
    return 0
  fi

  if [[ ! -e "$source_path" ]]; then
    mkdir -p "$target_path"
    ln -s "$target_path" "$source_path"
    echo "Linked empty runtime path: $source_path -> $target_path"
    return 0
  fi

  ensure_not_busy "$source_path"

  local stamp
  stamp="$(date +%Y%m%d-%H%M%S)"
  local backup_path="${source_path}.bak.${stamp}"

  mkdir -p "$target_path"
  rsync -a "$source_path"/ "$target_path"/
  mv "$source_path" "$backup_path"
  ln -s "$target_path" "$source_path"

  echo "Migrated $source_path -> $target_path"
  echo "Backup kept at $backup_path"
}

migrate_path "$ROOT_DIR/data" "$HOUSESPREDICT_DATA_DIR"
migrate_path "$ROOT_DIR/artifacts" "$HOUSESPREDICT_ARTIFACTS_DIR"
migrate_path "$ROOT_DIR/worker-app/.wrangler" "$RUNTIME_WRANGLER_DIR"

echo
echo "Runtime root: $HOUSESPREDICT_RUNTIME_DIR"
echo "Data dir:     $HOUSESPREDICT_DATA_DIR"
echo "Artifacts:    $HOUSESPREDICT_ARTIFACTS_DIR"
echo "Wrangler:     $RUNTIME_WRANGLER_DIR"
