#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${FRAMEFLOW_ENV_FILE:-$ROOT_DIR/deploy/.env}"
BACKUP_DIR="${BACKUP_DIR:-$ROOT_DIR/backups}"

die() {
  printf '错误：%s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "缺少命令：$1"
}

require_runtime() {
  require_command docker
  docker compose version >/dev/null 2>&1 || die "需要 Docker Compose v2（docker compose）"
  docker info >/dev/null 2>&1 || die "当前用户无法连接 Docker daemon"
}

require_env() {
  [[ -f "$ENV_FILE" ]] || die "缺少 $ENV_FILE，请先复制 deploy/.env.example"
}

env_value() {
  local key="$1" fallback="${2:-}"
  local value
  value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE")"
  value="${value%$'\r'}"
  if [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value:-$fallback}"
}

upsert_env() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  awk -v key="$key" -v value="$value" '
    BEGIN { found=0 }
    $0 ~ "^" key "=" { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$ENV_FILE" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

compose() {
  FRAMEFLOW_ENV_FILE="$ENV_FILE" docker compose \
    --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" "$@"
}

wait_ready() {
  local attempts="${1:-60}"
  local i
  for ((i=1; i<=attempts; i++)); do
    if compose exec -T frameflow python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}
