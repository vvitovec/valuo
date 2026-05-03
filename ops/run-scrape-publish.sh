#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

PIPELINE_RUN_ID="${PIPELINE_RUN_ID:-run-$(date -u +%Y%m%dT%H%M%SZ)}"
PIPELINE_RUN_TYPE="scrape"
PIPELINE_RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PIPELINE_RUN_STATUS="failure"
PIPELINE_SUMMARY_JSON="{}"
PIPELINE_ERROR_JSON='{"message":"scrape pipeline did not finish"}'
PIPELINE_MODEL_VERSION_BEFORE="$(python3 - <<'PY'
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
PIPELINE_MODEL_VERSION_AFTER="$PIPELINE_MODEL_VERSION_BEFORE"
REPORT_PATH=""

finalize_report() {
  export PIPELINE_RUN_ID PIPELINE_RUN_TYPE PIPELINE_RUN_STARTED_AT PIPELINE_RUN_STATUS PIPELINE_SUMMARY_JSON PIPELINE_ERROR_JSON PIPELINE_MODEL_VERSION_BEFORE PIPELINE_MODEL_VERSION_AFTER
  PIPELINE_RUN_FINISHED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  export PIPELINE_RUN_FINISHED_AT
  REPORT_PATH="$(./ops/write-pipeline-run-report.sh)"
  if [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]]; then
    ./ops/register-pipeline-run.sh "$REPORT_PATH" || true
  fi
}

on_exit() {
  local exit_code=$?
  if [[ -z "$REPORT_PATH" ]]; then
    finalize_report
  fi
  exit "$exit_code"
}
trap on_exit EXIT

./ops/run-scrape.sh
PYTHONPATH="$ROOT_DIR/pipeline/src${PYTHONPATH:+:$PYTHONPATH}" .venv/bin/python -m praha_predictor.cli refresh-outputs

PIPELINE_SUMMARY_JSON="$(
  python3 - <<'PY'
import json
import os
from pathlib import Path

reports_dir = Path(os.environ["HOUSESPREDICT_DATA_DIR"]) / "reports"
health_reports = []
for path in sorted(reports_dir.glob("source-health-*-latest.json")):
    report = json.loads(path.read_text(encoding="utf-8"))
    health_reports.append(
        {
            "source": report.get("source"),
            "status": report.get("status", "success"),
            "normalizedCount": report.get("normalized_count", 0),
            "rejectedCount": report.get("rejected_count", 0),
            "degradedReasons": report.get("degraded_reasons", []),
        }
    )

quality = json.loads((reports_dir / "quality-report-latest.json").read_text(encoding="utf-8"))
market_rows_path = reports_dir / "market-opportunities-latest.json"
market_rows = (
    json.loads(market_rows_path.read_text(encoding="utf-8"))
    if market_rows_path.exists()
    else []
)
summary = {
    "sources": health_reports,
    "curatedRows": quality.get("curated_records", 0),
    "newCuratedRowsSincePreviousRun": quality.get("new_curated_rows_since_previous_run", 0),
    "qualityStatus": quality.get("status", "success"),
    "degradedSources": quality.get("degraded_sources", []),
    "marketOpportunitiesRows": len(market_rows),
}
print(json.dumps(summary, ensure_ascii=False))
PY
)"
export PIPELINE_SUMMARY_JSON
PIPELINE_RUN_STATUS="$(
  python3 - <<'PY'
import json
import os

summary = json.loads(os.environ["PIPELINE_SUMMARY_JSON"])
statuses = {source.get("status", "success") for source in summary.get("sources", [])}
if summary.get("qualityStatus") == "degraded" or "degraded" in statuses:
    print("degraded")
else:
    print("success")
PY
)"
PIPELINE_ERROR_JSON="null"
finalize_report

PIPELINE_PUBLISH_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" PIPELINE_RUN_ID="$PIPELINE_RUN_ID" ./ops/publish-cloudflare.sh
