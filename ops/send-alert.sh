#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${ALERT_WEBHOOK_URL:-}" ]]; then
  exit 0
fi

RUN_TYPE="${1:-pipeline}"
STATUS="${2:-failed}"
MESSAGE="${3:-Valuo pipeline run failed}"

RUN_TYPE="$RUN_TYPE" STATUS="$STATUS" MESSAGE="$MESSAGE" python3 - <<'PY' | curl -fsS -X POST "$ALERT_WEBHOOK_URL" -H "content-type: application/json" --data-binary @- >/dev/null
import json
import os

payload = {
    "text": f"[Valuo] {os.environ.get('RUN_TYPE', 'pipeline')} {os.environ.get('STATUS', 'failed')}: {os.environ.get('MESSAGE', 'Pipeline run failed')}",
}
print(json.dumps(payload, ensure_ascii=False))
PY
