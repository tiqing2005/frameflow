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

is_true() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
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

edge_mode() {
  env_value FRAMEFLOW_EDGE_MODE caddy
}

using_external_nginx() {
  [[ "$(edge_mode)" == "external-nginx" ]]
}

validate_edge_mode() {
  case "$(edge_mode)" in
    caddy|external-nginx) ;;
    *) die "FRAMEFLOW_EDGE_MODE 仅支持 caddy 或 external-nginx" ;;
  esac
}

release_services() {
  printf '%s\n' frameflow
  if ! using_external_nginx; then
    printf '%s\n' caddy
  fi
}

compose() {
  local -a files=(-f "$ROOT_DIR/docker-compose.yml")
  if using_external_nginx; then
    files+=(-f "$ROOT_DIR/deploy/docker-compose.external-nginx.yml")
  fi
  FRAMEFLOW_ENV_FILE="$ENV_FILE" docker compose \
    --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" "${files[@]}" "$@"
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

wait_caddy() {
  local attempts="${1:-60}"
  local i container_id health
  for ((i=1; i<=attempts; i++)); do
    container_id="$(compose ps -q caddy 2>/dev/null || true)"
    if [[ -n "$container_id" ]]; then
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      if [[ "$health" == "healthy" ]]; then
        return 0
      fi
      [[ "$health" != "exited" && "$health" != "dead" ]] || return 1
    fi
    sleep 2
  done
  return 1
}

validate_caddy_config() {
  compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
    >/dev/null
}

validate_auth_state() {
  local expected snippet hash
  expected="$(env_value ENABLE_BASIC_AUTH true)"
  snippet="$ROOT_DIR/deploy/caddy/10-basic-auth.caddy"
  if is_true "$expected"; then
    [[ -f "$snippet" ]] || die "ENABLE_BASIC_AUTH=true，但缺少 10-basic-auth.caddy；请运行 deploy/configure-auth.sh enable"
    hash="$(env_value BASIC_AUTH_HASH)"
    [[ "$hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] \
      || die "ENABLE_BASIC_AUTH=true，但 BASIC_AUTH_HASH 不是有效的 bcrypt 哈希"
  elif [[ -f "$snippet" ]]; then
    die "ENABLE_BASIC_AUTH=false，但鉴权片段仍存在；请运行 deploy/configure-auth.sh disable"
  fi
}

validate_application_auth_state() {
  local enabled local_setup password_hash plaintext_password
  enabled="$(env_value FRAMEFLOW_AUTH_ENABLED true)"
  is_true "$enabled" || die "公网部署必须启用 FRAMEFLOW_AUTH_ENABLED=true"

  local_setup="$(env_value FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED false)"
  is_true "$local_setup" \
    && die "公网部署不得开放浏览器首次认领，请设置 FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED=false"

  plaintext_password="$(env_value FRAMEFLOW_AUTH_PASSWORD)"
  [[ -z "$plaintext_password" ]] \
    || die "deploy/.env 不得保存 FRAMEFLOW_AUTH_PASSWORD 明文，请只配置密码哈希"

  password_hash="$(env_value FRAMEFLOW_AUTH_PASSWORD_HASH)"
  [[ "$password_hash" =~ ^pbkdf2_sha256\$[0-9]{6,7}\$[A-Za-z0-9_-]+\$[A-Za-z0-9_-]+$ ]] \
    || die "FRAMEFLOW_AUTH_PASSWORD_HASH 缺失或格式无效，请重新执行 first-deploy.sh"
}

generate_application_password_hash() {
  local runtime
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import hashlib' >/dev/null 2>&1; then
    runtime=python3
  elif command -v python >/dev/null 2>&1 && python -c 'import hashlib' >/dev/null 2>&1; then
    runtime=python
  else
    die "缺少可用的 Python 3，无法生成应用管理员密码哈希"
  fi
  "$runtime" -c '
import base64
import hashlib
import secrets
import sys

password = sys.stdin.buffer.read()
salt = secrets.token_bytes(16)
iterations = 310_000
digest = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
print(f"pbkdf2_sha256${iterations}${encode(salt)}${encode(digest)}")
'
}

public_smoke_once() {
  local base_url path url timeout headers errors status user password
  base_url="$(env_value PUBLIC_SMOKE_URL "https://$(env_value DOMAIN)")"
  path="$(env_value PUBLIC_SMOKE_PATH /health/ready)"
  timeout="$(env_value PUBLIC_SMOKE_TIMEOUT_SECONDS 20)"
  base_url="${base_url%/}"
  [[ "$path" == /* ]] || path="/$path"
  url="$base_url$path"
  [[ "$base_url" == https://* ]] || {
    printf '公网 smoke 必须使用 HTTPS：%s\n' "$base_url" >&2
    return 1
  }

  headers="$(mktemp)"
  errors="$(mktemp)"
  status="$(curl --silent --show-error --output /dev/null --dump-header "$headers" \
    --write-out '%{http_code}' --connect-timeout 5 --max-time "$timeout" "$url" 2>"$errors")" || {
      cat "$errors" >&2
      rm -f "$headers" "$errors"
      return 1
    }

  if is_true "$(env_value ENABLE_BASIC_AUTH true)"; then
    if [[ "$status" != "401" ]] || ! grep -Eiq '^www-authenticate:[[:space:]]*Basic' "$headers"; then
      printf '公网鉴权 smoke 失败：未认证请求期望 401 + Basic challenge，实际 HTTP %s\n' "$status" >&2
      rm -f "$headers" "$errors"
      return 1
    fi
    password="${FRAMEFLOW_SMOKE_PASSWORD:-}"
    if [[ -n "$password" ]]; then
      user="${FRAMEFLOW_SMOKE_USER:-$(env_value BASIC_AUTH_USER frameflow)}"
      status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --connect-timeout 5 --max-time "$timeout" --user "$user:$password" "$url" 2>"$errors")" || {
          cat "$errors" >&2
          rm -f "$headers" "$errors"
          return 1
        }
      if [[ "$status" != "200" ]]; then
        printf '公网鉴权 smoke 失败：认证后期望 HTTP 200，实际 HTTP %s\n' "$status" >&2
        rm -f "$headers" "$errors"
        return 1
      fi
      printf '公网 smoke 通过：HTTPS、Basic Auth challenge 与认证后 ready 均正常。\n'
    else
      printf '公网 smoke 通过：HTTPS 与 Basic Auth challenge 正常；未提供 FRAMEFLOW_SMOKE_PASSWORD，认证后 ready 由容器内探针兜底。\n'
    fi
  elif [[ "$status" == "200" ]]; then
    printf '公网 smoke 通过：HTTPS ready 返回 200。\n'
  else
    printf '公网 smoke 失败：期望 HTTP 200，实际 HTTP %s\n' "$status" >&2
    rm -f "$headers" "$errors"
    return 1
  fi
  rm -f "$headers" "$errors"
}

run_public_smoke() {
  is_true "$(env_value PUBLIC_SMOKE_ENABLED true)" || {
    printf '已按 PUBLIC_SMOKE_ENABLED=false 跳过公网 HTTPS smoke。\n' >&2
    return 0
  }
  require_command curl
  local attempts interval i
  attempts="$(env_value PUBLIC_SMOKE_ATTEMPTS 12)"
  interval="$(env_value PUBLIC_SMOKE_INTERVAL_SECONDS 5)"
  for ((i=1; i<=attempts; i++)); do
    if public_smoke_once; then
      return 0
    fi
    ((i == attempts)) || sleep "$interval"
  done
  if is_true "$(env_value PUBLIC_SMOKE_REQUIRED true)"; then
    return 1
  fi
  printf '警告：公网 smoke 未通过，但 PUBLIC_SMOKE_REQUIRED=false，继续发布。\n' >&2
  return 0
}

wait_release_ready() {
  local attempts="${1:-90}"
  wait_ready "$attempts" || return 1
  if ! using_external_nginx; then
    wait_caddy "$attempts" || return 1
    validate_caddy_config || return 1
  fi
  run_public_smoke
}

show_release_diagnostics() {
  compose ps >&2 || true
  if using_external_nginx; then
    compose logs --tail=150 frameflow >&2 || true
  else
    compose logs --tail=150 frameflow caddy >&2 || true
  fi
}

sqlite_check_volume() {
  local volume="$1" database_path="${2:-/check/frameflow.db}"
  local image
  image="frameflow:$(env_value FRAMEFLOW_TAG latest)"
  docker image inspect "$image" >/dev/null 2>&1 || die "缺少用于 SQLite 校验的应用镜像：$image"
  docker run --rm --user 10001:10001 --entrypoint python \
    -v "$volume:/check" "$image" -c '
import sqlite3
import sys

database = sys.argv[1]
connection = sqlite3.connect(database, timeout=30)
try:
    integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise SystemExit("SQLite integrity_check 失败：" + "; ".join(integrity[:20]))
    foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
    if foreign_keys:
        details = "; ".join(repr(row) for row in foreign_keys[:20])
        raise SystemExit("SQLite foreign_key_check 失败：" + details)
finally:
    connection.close()
print("SQLite integrity_check / foreign_key_check：通过")
' "$database_path"
}
