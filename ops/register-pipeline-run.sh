#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

REPORT_PATH="${1:-${PIPELINE_RUN_REPORT:-}}"
if [[ -z "$REPORT_PATH" || ! -f "$REPORT_PATH" ]]; then
  echo "Pipeline run report not found, skipping register."
  exit 0
fi

D1_DATABASE="${CLOUDFLARE_D1_DATABASE:-praha-price-predictor}"
DRY_RUN_DIR="${REGISTER_DRY_RUN_DIR:-}"
TMP_DIR="$(mktemp -d)"
SQL_FILE="$TMP_DIR/pipeline-run.sql"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

PIPELINE_RUN_REPORT="$REPORT_PATH" python3 - <<'PY' > "$SQL_FILE"
import json
import os
from pathlib import Path

report = json.loads(Path(os.environ["PIPELINE_RUN_REPORT"]).read_text(encoding="utf-8"))

def sql_quote(value):
    if value is None:
        return "NULL"
    text = str(value).replace("'", "''")
    return f"'{text}'"

summary = json.dumps(report.get("summary", {}), ensure_ascii=False)
error = report.get("error")
error_json = json.dumps(error, ensure_ascii=False) if error is not None else None

print(
    "INSERT OR REPLACE INTO pipeline_run_registry "
    "(run_id, run_type, started_at, finished_at, status, model_version_before, model_version_after, summary_json, error_json) VALUES "
    f"({sql_quote(report.get('runId'))}, "
    f"{sql_quote(report.get('runType'))}, "
    f"{sql_quote(report.get('startedAt'))}, "
    f"{sql_quote(report.get('finishedAt'))}, "
    f"{sql_quote(report.get('status'))}, "
    f"{sql_quote(report.get('modelVersionBefore'))}, "
    f"{sql_quote(report.get('modelVersionAfter'))}, "
    f"{sql_quote(summary)}, "
    f"{sql_quote(error_json)});"
)
PY

if [[ -n "$DRY_RUN_DIR" ]]; then
  mkdir -p "$DRY_RUN_DIR"
  cp "$SQL_FILE" "$DRY_RUN_DIR/pipeline-run.sql"
  exit 0
fi

npx wrangler d1 execute "$D1_DATABASE" --remote --file "$SQL_FILE" >/dev/null
