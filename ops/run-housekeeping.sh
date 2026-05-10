#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

if [[ -z "${CLOUDFLARE_API_TOKEN:-}" || -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]]; then
  echo "Housekeeping skipped: set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID to enable remote D1 cleanup."
  exit 0
fi

D1_DATABASE="${CLOUDFLARE_D1_DATABASE:-praha-price-predictor}"
TMP_DIR="$(mktemp -d)"
SQL_FILE="$TMP_DIR/housekeeping.sql"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

python3 - <<'PY' > "$SQL_FILE"
print(
    """
BEGIN IMMEDIATE;
DELETE FROM prediction_audit
WHERE created_at < datetime('now', '-30 days');
DELETE FROM geocode_audit
WHERE created_at < datetime('now', '-30 days');
DELETE FROM pipeline_run_registry
WHERE finished_at < datetime('now', '-90 days');
COMMIT;
""".strip()
)
PY

npx wrangler d1 execute "$D1_DATABASE" --remote --file "$SQL_FILE" >/dev/null
echo "Housekeeping complete"
