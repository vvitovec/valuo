#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source "$ROOT_DIR/ops/runtime-paths.sh"

: "${PIPELINE_RUN_TYPE:?PIPELINE_RUN_TYPE is required}"
: "${PIPELINE_RUN_ID:?PIPELINE_RUN_ID is required}"
: "${PIPELINE_RUN_STATUS:?PIPELINE_RUN_STATUS is required}"
: "${PIPELINE_RUN_STARTED_AT:?PIPELINE_RUN_STARTED_AT is required}"
: "${PIPELINE_RUN_FINISHED_AT:?PIPELINE_RUN_FINISHED_AT is required}"

python3 - <<'PY'
import json
import os
from pathlib import Path

report = {
    "runId": os.environ["PIPELINE_RUN_ID"],
    "runType": os.environ["PIPELINE_RUN_TYPE"],
    "status": os.environ["PIPELINE_RUN_STATUS"],
    "startedAt": os.environ["PIPELINE_RUN_STARTED_AT"],
    "finishedAt": os.environ["PIPELINE_RUN_FINISHED_AT"],
    "modelVersionBefore": os.environ.get("PIPELINE_MODEL_VERSION_BEFORE") or None,
    "modelVersionAfter": os.environ.get("PIPELINE_MODEL_VERSION_AFTER") or None,
    "summary": json.loads(os.environ.get("PIPELINE_SUMMARY_JSON") or "{}"),
    "error": json.loads(os.environ.get("PIPELINE_ERROR_JSON") or "null"),
}

reports_dir = Path(os.environ["HOUSESPREDICT_DATA_DIR"]) / "reports"
reports_dir.mkdir(parents=True, exist_ok=True)
versioned_path = reports_dir / f"pipeline-{report['runType']}-{report['runId']}.json"
latest_path = reports_dir / f"pipeline-{report['runType']}-latest.json"
payload = json.dumps(report, ensure_ascii=False, indent=2)
versioned_path.write_text(payload, encoding="utf-8")
latest_path.write_text(payload, encoding="utf-8")
print(versioned_path)
PY
