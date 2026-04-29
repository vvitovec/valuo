#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

R2_BUCKET="${CLOUDFLARE_R2_BUCKET:-praha-price-models}"
D1_DATABASE="${CLOUDFLARE_D1_DATABASE:-praha-price-predictor}"
DRY_RUN_DIR="${PUBLISH_DRY_RUN_DIR:-}"
PIPELINE_RUN_ID="${PIPELINE_RUN_ID:-publish-$(date -u +%Y%m%dT%H%M%SZ)}"
PUBLISH_STARTED_AT="${PIPELINE_PUBLISH_STARTED_AT:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
TMP_DIR="$(mktemp -d)"
MANIFEST_FILE="$TMP_DIR/r2-upload-manifest.tsv"
SQL_FILE="$TMP_DIR/d1-seed.sql"
FALLBACK_SQL_FILE="$TMP_DIR/d1-seed-no-transaction.sql"
PUBLISH_REPORT_PATH=""

write_publish_report() {
  local publish_status="$1"
  local error_json="$2"
  local finished_at="$3"
  local active_model_version
  active_model_version="$(
    python3 - <<'PY'
import json
import os
from pathlib import Path
path = Path(os.environ["HOUSESPREDICT_ARTIFACTS_DIR"]) / "model-registry.json"
if not path.exists():
    print("")
else:
    print(json.loads(path.read_text(encoding="utf-8")).get("activeModelVersion") or "")
PY
  )"
  PIPELINE_RUN_TYPE="publish"
  PIPELINE_RUN_STATUS="$publish_status"
  PIPELINE_RUN_STARTED_AT="$PUBLISH_STARTED_AT"
  PIPELINE_RUN_FINISHED_AT="$finished_at"
  PIPELINE_SUMMARY_JSON="$(
    python3 - <<'PY'
import json
import os
from pathlib import Path

artifacts_dir = Path(os.environ["HOUSESPREDICT_ARTIFACTS_DIR"])
reports_dir = Path(os.environ["HOUSESPREDICT_DATA_DIR"]) / "reports"
registry = json.loads((artifacts_dir / "model-registry.json").read_text(encoding="utf-8"))
rows_path = reports_dir / "market-opportunities-latest.json"
rows = json.loads(rows_path.read_text(encoding="utf-8")) if rows_path.exists() else []
print(
    json.dumps(
        {
            "activeModelVersion": registry.get("activeModelVersion"),
            "marketOpportunitiesRows": len(rows),
            "uploadedObjectCount": sum(1 for _ in reports_dir.glob("pipeline-*.json")) + sum(1 for _ in artifacts_dir.glob("*.json")),
        },
        ensure_ascii=False,
    )
)
PY
  )"
  PIPELINE_MODEL_VERSION_BEFORE="$active_model_version"
  PIPELINE_MODEL_VERSION_AFTER="$active_model_version"
  PIPELINE_ERROR_JSON="$error_json"
  export PIPELINE_RUN_ID PIPELINE_RUN_TYPE PIPELINE_RUN_STATUS PIPELINE_RUN_STARTED_AT PIPELINE_RUN_FINISHED_AT PIPELINE_SUMMARY_JSON PIPELINE_MODEL_VERSION_BEFORE PIPELINE_MODEL_VERSION_AFTER PIPELINE_ERROR_JSON
  PUBLISH_REPORT_PATH="$(./ops/write-pipeline-run-report.sh)"
  if [[ -z "$DRY_RUN_DIR" ]]; then
    ./ops/register-pipeline-run.sh "$PUBLISH_REPORT_PATH" || true
  fi
}

cleanup() {
  rm -rf "$TMP_DIR"
}
on_exit() {
  local exit_code=$?
  if [[ $exit_code -ne 0 && -z "$PUBLISH_REPORT_PATH" ]]; then
    local failed_command="${BASH_COMMAND:-unknown}"
    local error_json
    error_json="$(FAILED_COMMAND="$failed_command" python3 - <<'PY'
import json
import os
print(json.dumps({"message": "Cloudflare publish failed", "command": os.environ.get("FAILED_COMMAND", "unknown")}, ensure_ascii=False))
PY
)"
    write_publish_report "failure" "$error_json" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
  cleanup
  exit "$exit_code"
}
trap on_exit EXIT

python3 - <<'PY' > "$MANIFEST_FILE"
import os
from pathlib import Path

upload_pairs: list[tuple[Path, str]] = []
artifact_dir = Path(os.environ["HOUSESPREDICT_ARTIFACTS_DIR"])
report_dir = Path(os.environ["HOUSESPREDICT_DATA_DIR"]) / "reports"

for path in sorted(artifact_dir.glob("*.json")):
    upload_pairs.append((path, path.name))

for pattern in (
    "quality-report*.json",
    "source-health-*-latest.json",
    "source-probe-*-latest.json",
    "market-opportunities-latest.json",
    "pipeline-*.json",
):
    for path in sorted(report_dir.glob(pattern)):
        upload_pairs.append((path, f"reports/{path.name}"))

for local_path, object_key in upload_pairs:
    print(f"{local_path}\t{object_key}")
PY

if [[ -z "$DRY_RUN_DIR" ]]; then
  while IFS=$'\t' read -r local_path object_key; do
    [[ -z "$local_path" ]] && continue
    echo "Uploading $object_key"
    npx wrangler r2 object put "${R2_BUCKET}/${object_key}" --file "$local_path" --remote >/dev/null
  done < "$MANIFEST_FILE"
fi

python3 - <<'PY' > "$SQL_FILE"
import json
import os
from pathlib import Path

model_registry_path = Path(os.environ["HOUSESPREDICT_ARTIFACTS_DIR"]) / "model-registry.json"
report_dir = Path(os.environ["HOUSESPREDICT_DATA_DIR"]) / "reports"


def sql_quote(value: object) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("'", "''")
    return f"'{text}'"


lines: list[str] = ["BEGIN IMMEDIATE;"]

if model_registry_path.exists():
    registry = json.loads(model_registry_path.read_text(encoding="utf-8"))
    for entry in registry.get("entries", []):
        metrics_json = json.dumps(entry.get("validationSummary", {}), ensure_ascii=False)
        lines.append(
            "INSERT OR REPLACE INTO model_registry "
            "(model_version, created_at, model_kind, metrics_json, promotion_reason, curated_row_count) VALUES "
            f"({sql_quote(entry.get('version'))}, "
            f"{sql_quote(entry.get('trainedAt'))}, "
            f"{sql_quote(entry.get('modelKind'))}, "
            f"{sql_quote(metrics_json)}, "
            f"{sql_quote(entry.get('promotionReason'))}, "
            f"{entry.get('curatedRowCount', 0)});"
        )

for report_path in sorted(report_dir.glob("source-health-*-latest.json")):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_json = json.dumps(report, ensure_ascii=False)
    lines.append(
        "INSERT OR REPLACE INTO source_run_registry "
        "(run_id, source, created_at, report_json) VALUES "
        f"({sql_quote(report.get('run_id'))}, "
        f"{sql_quote(report.get('source'))}, "
        f"{sql_quote(report.get('generated_at'))}, "
        f"{sql_quote(report_json)});"
    )

for report_path in sorted(report_dir.glob("pipeline-*-latest.json")):
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary_json = json.dumps(report.get("summary", {}), ensure_ascii=False)
    error = report.get("error")
    error_json = json.dumps(error, ensure_ascii=False) if error is not None else None
    lines.append(
        "INSERT OR REPLACE INTO pipeline_run_registry "
        "(run_id, run_type, started_at, finished_at, status, model_version_before, model_version_after, summary_json, error_json) VALUES "
        f"({sql_quote(report.get('runId'))}, "
        f"{sql_quote(report.get('runType'))}, "
        f"{sql_quote(report.get('startedAt'))}, "
        f"{sql_quote(report.get('finishedAt'))}, "
        f"{sql_quote(report.get('status'))}, "
        f"{sql_quote(report.get('modelVersionBefore'))}, "
        f"{sql_quote(report.get('modelVersionAfter'))}, "
        f"{sql_quote(summary_json)}, "
        f"{sql_quote(error_json)});"
    )

market_scores_path = report_dir / "market-opportunities-latest.json"
if market_scores_path.exists():
    rows = json.loads(market_scores_path.read_text(encoding="utf-8"))
    lines.append("DELETE FROM market_listing_score;")
    for row in rows:
        lines.append(
            "INSERT OR REPLACE INTO market_listing_score "
            "(source, source_listing_id, discovered_at, observed_at, listing_url, address_text, district_prague, "
            "location_cluster, property_type, asking_price_czk, predicted_price_czk, typical_range_low_czk, "
            "typical_range_high_czk, deviation_czk, deviation_pct, market_position, opportunity_score, "
            "listing_quality_score, quality_flags, comparables_count, confidence_score, is_filtered_default, "
            "filter_reasons, warning_flags, updated_at) VALUES "
            f"({sql_quote(row.get('source'))}, "
            f"{sql_quote(row.get('source_listing_id'))}, "
            f"{sql_quote(row.get('discovered_at'))}, "
            f"{sql_quote(row.get('observed_at'))}, "
            f"{sql_quote(row.get('listing_url'))}, "
            f"{sql_quote(row.get('address_text'))}, "
            f"{sql_quote(row.get('district_prague'))}, "
            f"{sql_quote(row.get('location_cluster'))}, "
            f"{sql_quote(row.get('property_type'))}, "
            f"{row.get('asking_price_czk', 0)}, "
            f"{row.get('predicted_price_czk', 0)}, "
            f"{row.get('typical_range_low_czk', 0)}, "
            f"{row.get('typical_range_high_czk', 0)}, "
            f"{row.get('deviation_czk', 0)}, "
            f"{row.get('deviation_pct', 0)}, "
            f"{sql_quote(row.get('market_position'))}, "
            f"{row.get('opportunity_score', 0)}, "
            f"{row.get('listing_quality_score', 1)}, "
            f"{sql_quote(json.dumps(row.get('quality_flags', []), ensure_ascii=False))}, "
            f"{row.get('comparables_count', 0)}, "
            f"{row.get('confidence_score', 1)}, "
            f"{1 if row.get('is_filtered_default') else 0}, "
            f"{sql_quote(json.dumps(row.get('filter_reasons', []), ensure_ascii=False))}, "
            f"{sql_quote(json.dumps(row.get('warning_flags', []), ensure_ascii=False))}, "
            f"{sql_quote(row.get('updated_at'))});"
        )

lines.append("COMMIT;")
print("\n".join(lines))
PY

if [[ -n "$DRY_RUN_DIR" ]]; then
  mkdir -p "$DRY_RUN_DIR"
  cp "$MANIFEST_FILE" "$DRY_RUN_DIR/r2-upload-manifest.tsv"
  cp "$SQL_FILE" "$DRY_RUN_DIR/d1-seed.sql"
  echo "Cloudflare publish dry run prepared"
  exit 0
fi

echo "Seeding D1 registries"
set +e
D1_EXECUTE_OUTPUT="$(npx wrangler d1 execute "$D1_DATABASE" --remote --file "$SQL_FILE" 2>&1)"
D1_EXECUTE_STATUS=$?
set -e
if [[ $D1_EXECUTE_STATUS -ne 0 ]]; then
  if grep -q "state.storage.transaction" <<<"$D1_EXECUTE_OUTPUT"; then
    python3 - <<'PY' "$SQL_FILE" "$FALLBACK_SQL_FILE"
from pathlib import Path
import sys

source_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
lines = source_path.read_text(encoding="utf-8").splitlines()
filtered = [line for line in lines if line not in {"BEGIN IMMEDIATE;", "COMMIT;"}]
target_path.write_text("\n".join(filtered) + "\n", encoding="utf-8")
PY
    echo "D1 CLI rejected explicit transaction statements, retrying non-transactional seed"
    npx wrangler d1 execute "$D1_DATABASE" --remote --file "$FALLBACK_SQL_FILE" >/dev/null
  else
    printf '%s\n' "$D1_EXECUTE_OUTPUT" >&2
    exit "$D1_EXECUTE_STATUS"
  fi
fi

write_publish_report "success" "null" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Cloudflare publish complete"
