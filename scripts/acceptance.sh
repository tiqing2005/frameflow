#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
BASE_URL="${BASE_URL%/}"
API_BASE="$BASE_URL/api/v1"
TIMEOUT_SECONDS="${FRAMEFLOW_ACCEPTANCE_TIMEOUT:-90}"
SKIP_CREATE="${FRAMEFLOW_ACCEPTANCE_READ_ONLY:-0}"
AUTH_USERNAME="${FRAMEFLOW_ACCEPTANCE_USERNAME:-${FRAMEFLOW_AUTH_USERNAME:-admin}}"
AUTH_PASSWORD="${FRAMEFLOW_ACCEPTANCE_PASSWORD:-${FRAMEFLOW_AUTH_PASSWORD:-}}"

if command -v python3 >/dev/null 2>&1 && python3 -c 'import json' >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1 && python -c 'import json' >/dev/null 2>&1; then
  PYTHON=python
else
  echo "ERROR: python3 or python is required for JSON validation." >&2
  exit 2
fi
export PYTHONIOENCODING=utf-8

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
COOKIE_JAR="$TMP_DIR/cookies.txt"
CSRF_TOKEN=""

step() { printf '\n==> %s\n' "$1"; }

request() {
  local method="$1" url="$2" output="$3" expected="$4"
  shift 4
  local status
  local -a auth_args=()
  if [ -n "$CSRF_TOKEN" ] && [ "$method" != "GET" ] && [ "$method" != "HEAD" ]; then
    auth_args+=(--header "X-CSRF-Token: $CSRF_TOKEN")
  fi
  status="$(curl --silent --show-error --location --max-time 30 \
    --cookie "$COOKIE_JAR" --cookie-jar "$COOKIE_JAR" \
    --output "$output" --write-out '%{http_code}' \
    --request "$method" "${auth_args[@]}" "$@" "$url")"
  case ",$expected," in
    *",$status,"*) ;;
    *)
      echo "ERROR: $method $url returned HTTP $status; expected $expected" >&2
      sed -n '1,40p' "$output" >&2 || true
      exit 1
      ;;
  esac
  printf '%s' "$status"
}

json_path() {
  local file="$1"
  shift
  "$PYTHON" - "$file" "$@" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
for raw in sys.argv[2:]:
    value = data
    try:
        for part in raw.split("."):
            value = value[part]
    except (KeyError, TypeError):
        continue
    if value is not None and str(value):
        # Avoid CRLF in command substitutions when the script is run from
        # Git Bash with Windows Python.
        print(value, end="")
        raise SystemExit(0)
raise SystemExit(1)
PY
}

echo "FrameFlow AI contract acceptance"
echo "Target: $BASE_URL"
echo "This script never reads or prints provider API keys."

step "Liveness"
status="$(request GET "$BASE_URL/health/live" "$TMP_DIR/live.json" "200")"
echo "live HTTP $status"

step "Readiness (database and worker)"
status="$(request GET "$BASE_URL/health/ready" "$TMP_DIR/ready.json" "200")"
echo "ready HTTP $status"

step "Application authentication"
request GET "$API_BASE/auth/session" "$TMP_DIR/session.json" "200" >/dev/null
auth_enabled="$(json_path "$TMP_DIR/session.json" auth_enabled || true)"
authenticated="$(json_path "$TMP_DIR/session.json" authenticated || true)"
if [ "$auth_enabled" = "True" ] || [ "$auth_enabled" = "true" ]; then
  if [ "$authenticated" != "True" ] && [ "$authenticated" != "true" ]; then
    if [ -z "$AUTH_PASSWORD" ]; then
      echo "ERROR: authentication is enabled; set FRAMEFLOW_ACCEPTANCE_PASSWORD (and optionally FRAMEFLOW_ACCEPTANCE_USERNAME)." >&2
      exit 2
    fi
    "$PYTHON" - "$AUTH_USERNAME" "$AUTH_PASSWORD" >"$TMP_DIR/login-payload.json" <<'PY'
import json, sys
print(json.dumps({"username": sys.argv[1], "password": sys.argv[2]}))
PY
    request POST "$API_BASE/auth/login" "$TMP_DIR/login.json" "200" \
      --header 'Content-Type: application/json' \
      --data-binary "@$TMP_DIR/login-payload.json" >/dev/null
    CSRF_TOKEN="$(json_path "$TMP_DIR/login.json" csrf_token)" || {
      echo "ERROR: login response has no csrf_token" >&2
      exit 1
    }
    echo "authenticated as $AUTH_USERNAME"
  else
    CSRF_TOKEN="$(json_path "$TMP_DIR/session.json" csrf_token || true)"
    echo "existing authenticated session accepted"
  fi
else
  echo "application authentication disabled"
fi

step "Seed assets"
request GET "$API_BASE/assets" "$TMP_DIR/assets.json" "200" >/dev/null
asset_count="$($PYTHON - "$TMP_DIR/assets.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
items = data if isinstance(data, list) else data.get("items", [])
print(len(items))
PY
)"
if [ "$asset_count" -lt 10 ]; then
  echo "ERROR: expected at least 10 active assets, got $asset_count" >&2
  exit 1
fi
echo "active assets: $asset_count"

step "Read-only collection endpoints"
request GET "$API_BASE/projects" "$TMP_DIR/projects.json" "200" >/dev/null
request GET "$API_BASE/runs" "$TMP_DIR/runs.json" "200" >/dev/null
request GET "$API_BASE/audit" "$TMP_DIR/audit.json" "200" >/dev/null
echo "projects/runs/audit HTTP: 200/200/200"

if [ "$SKIP_CREATE" = "1" ]; then
  echo
  echo "PASS: read-only checks completed."
  exit 0
fi

step "Create a unique text project"
suffix="$(date -u +%Y%m%d-%H%M%S)-$RANDOM"
idem="acceptance-$suffix"
"$PYTHON" - "$suffix" >"$TMP_DIR/payload.json" <<'PY'
import json, sys
suffix = sys.argv[1]
print(json.dumps({
    "title": f"验收脚本-{suffix}",
    "text": "清晨，我骑着自行车穿过城市，准时来到办公室。团队用数据看板梳理项目进度，把复杂任务拆成清晰步骤。午后，我们讨论怎样用绿色科技让工作更高效，也让生活更健康。"
}, ensure_ascii=False))
PY
request POST "$API_BASE/projects/text" "$TMP_DIR/create.json" "200,202" \
  --header 'Content-Type: application/json; charset=utf-8' \
  --header "Idempotency-Key: $idem" \
  --data-binary "@$TMP_DIR/payload.json" >/dev/null

project_id="$(json_path "$TMP_DIR/create.json" project.id project_id)" || {
  echo "ERROR: create response has no project.id/project_id" >&2; exit 1;
}
job_id="$(json_path "$TMP_DIR/create.json" job.id job_id)" || {
  echo "ERROR: create response has no job.id/job_id" >&2; exit 1;
}
echo "created project=$project_id job=$job_id"

step "Replay the same idempotent request"
request POST "$API_BASE/projects/text" "$TMP_DIR/replay.json" "200,202" \
  --header 'Content-Type: application/json; charset=utf-8' \
  --header "Idempotency-Key: $idem" \
  --data-binary "@$TMP_DIR/payload.json" >/dev/null
replay_project="$(json_path "$TMP_DIR/replay.json" project.id project_id)"
replay_job="$(json_path "$TMP_DIR/replay.json" job.id job_id)"
if [ "$replay_project" != "$project_id" ] || [ "$replay_job" != "$job_id" ]; then
  echo "ERROR: idempotency replay returned different resources" >&2
  exit 1
fi
echo "idempotency replay returned the original resources"

step "Wait for the durable job terminal state"
start_epoch="$(date +%s)"
last_status=""
while :; do
  request GET "$API_BASE/jobs/$job_id" "$TMP_DIR/job.json" "200" >/dev/null
  job_status="$(json_path "$TMP_DIR/job.json" job.status status || true)"
  job_stage="$(json_path "$TMP_DIR/job.json" job.stage stage || true)"
  job_progress="$(json_path "$TMP_DIR/job.json" job.progress progress || true)"
  if [ "$job_status" != "$last_status" ]; then
    echo "job status=$job_status stage=$job_stage progress=$job_progress"
    last_status="$job_status"
  fi
  if [ "$job_status" = "succeeded" ]; then break; fi
  if [ "$job_status" = "failed" ] || [ "$job_status" = "canceled" ]; then
    code="$(json_path "$TMP_DIR/job.json" job.error_code error_code || true)"
    message="$(json_path "$TMP_DIR/job.json" job.error_message error_message || true)"
    echo "ERROR: job reached $job_status; code=$code; message=$message" >&2
    exit 1
  fi
  now="$(date +%s)"
  if [ $((now - start_epoch)) -ge "$TIMEOUT_SECONDS" ]; then
    echo "ERROR: timed out waiting for job $job_id (last status=$job_status)" >&2
    exit 1
  fi
  sleep 1
done

step "Verify persisted project result"
request GET "$API_BASE/projects/$project_id" "$TMP_DIR/detail.json" "200" >/dev/null
"$PYTHON" - "$TMP_DIR/detail.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
segments = data.get("segments", [])
if not segments:
    raise SystemExit("Ready project has no persisted segments")
for segment in segments:
    recs = segment.get("recommendations", [])
    if len(recs) < 3:
        raise SystemExit(f"Segment {segment.get('id')} has fewer than three recommendations")
    asset_ids = [item.get("asset_id") or (item.get("asset") or {}).get("id") for item in recs]
    if len(set(asset_ids)) < 3:
        raise SystemExit(f"Segment {segment.get('id')} recommendations are not unique")
    if any(not item.get("explanation") for item in recs):
        raise SystemExit(f"Segment {segment.get('id')} has a recommendation without explanation")
print(f"persisted segments: {len(segments)}; every segment has >=3 unique explainable candidates")
PY

echo
echo "PASS: FrameFlow AI contract smoke completed."
echo "Created demo project remains available for refresh/manual UI checks: $project_id"
