#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALID_HASH='pbkdf2_sha256$310000$MDEyMzQ1Njc4OWFiY2RlZg$MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY'

run_case() {
  local name="$1" expected="$2" content="$3" env_file status=0
  env_file="$(mktemp)"
  printf '%s\n' "$content" > "$env_file"
  (
    export FRAMEFLOW_ENV_FILE="$env_file"
    # shellcheck source=../deploy/common.sh
    source "$ROOT_DIR/deploy/common.sh"
    validate_application_auth_state
  ) >/dev/null 2>&1 || status=$?
  rm -f "$env_file"
  if [[ "$expected" == pass && "$status" -ne 0 ]] \
    || [[ "$expected" == fail && "$status" -eq 0 ]]; then
    printf 'FAIL: %s (status=%s)\n' "$name" "$status" >&2
    return 1
  fi
  printf 'PASS: %s\n' "$name"
}

run_case "valid public application authentication" pass "FRAMEFLOW_AUTH_ENABLED=true
FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false
FRAMEFLOW_AUTH_PASSWORD_HASH=$VALID_HASH
FRAMEFLOW_AUTH_PASSWORD="
run_case "authentication cannot be disabled" fail "FRAMEFLOW_AUTH_ENABLED=false
FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false
FRAMEFLOW_AUTH_PASSWORD_HASH=$VALID_HASH"
run_case "public first-run setup cannot remain open" fail "FRAMEFLOW_AUTH_ENABLED=true
FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=true
FRAMEFLOW_AUTH_PASSWORD_HASH=$VALID_HASH"
run_case "plaintext password is rejected" fail "FRAMEFLOW_AUTH_ENABLED=true
FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false
FRAMEFLOW_AUTH_PASSWORD_HASH=$VALID_HASH
FRAMEFLOW_AUTH_PASSWORD=secret"
run_case "missing password hash is rejected" fail "FRAMEFLOW_AUTH_ENABLED=true
FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false
FRAMEFLOW_AUTH_PASSWORD_HASH="

compose_env="$(mktemp)"
generated_hash="$(printf '%s' 'FrameFlow-Test-2026!' | (
  export FRAMEFLOW_ENV_FILE="$compose_env"
  # shellcheck source=../deploy/common.sh
  source "$ROOT_DIR/deploy/common.sh"
  generate_application_password_hash
))"
generated_hash="${generated_hash%$'\r'}"
[[ "$generated_hash" =~ ^pbkdf2_sha256\$310000\$[A-Za-z0-9_-]+\$[A-Za-z0-9_-]+$ ]]
cat > "$compose_env" <<EOF
DOMAIN=frameflow.example.test
ACME_EMAIL=ops@example.test
FRAMEFLOW_EDGE_MODE=external-nginx
FRAMEFLOW_HOST_PORT=8080
FRAMEFLOW_AUTH_ENABLED=true
FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false
FRAMEFLOW_AUTH_PASSWORD_HASH='$generated_hash'
FRAMEFLOW_AUTH_PASSWORD=
EOF
(
  export FRAMEFLOW_ENV_FILE="$compose_env"
  # shellcheck source=../deploy/common.sh
  source "$ROOT_DIR/deploy/common.sh"
  validate_edge_mode
  mapfile -t services_to_start < <(release_services)
  [[ "${services_to_start[*]}" == "frameflow" ]]
  services="$(compose config --services)"
  [[ "$services" == "frameflow" ]]
  compose config | grep -q 'host_ip: 127.0.0.1'
  compose config | grep -q 'published: "8080"'
  readback_hash="$(compose config --format json | python -c '
import json
import sys
print(json.load(sys.stdin)["services"]["frameflow"]["environment"]["FRAMEFLOW_AUTH_PASSWORD_HASH"])
')"
  # `docker compose config` serializes a literal dollar as `$$`; the container receives `$`.
  readback_hash="${readback_hash//\$\$/\$}"
  [[ "$readback_hash" == "$generated_hash" ]]
) || {
  rm -f "$compose_env"
  printf 'FAIL: external Nginx Compose topology\n' >&2
  exit 1
}
rm -f "$compose_env"
printf 'PASS: external Nginx Compose topology\n'
