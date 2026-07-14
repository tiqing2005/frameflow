#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env

snippet="$ROOT_DIR/deploy/caddy/10-basic-auth.caddy"
template="$ROOT_DIR/deploy/caddy/basic-auth.caddy.example"
action="${1:-status}"

restart_caddy_if_running() {
  [[ "${FRAMEFLOW_AUTH_NO_RESTART:-0}" == 1 ]] && return 0
  if compose ps --status running --services 2>/dev/null | grep -qx caddy; then
    compose up -d --no-deps --force-recreate caddy
  fi
}

case "$action" in
  status)
    if [[ -f "$snippet" ]]; then
      printf 'Basic Auth：已启用（用户：%s）\n' "$(env_value BASIC_AUTH_USER frameflow)"
    else
      printf 'Basic Auth：未启用\n'
    fi
    ;;
  enable)
    user="$(env_value BASIC_AUTH_USER frameflow)"
    [[ "$user" =~ ^[A-Za-z0-9._@-]+$ ]] || die "BASIC_AUTH_USER 只能包含字母、数字、点、下划线、@ 或连字符"
    hash="$(env_value BASIC_AUTH_HASH)"
    if [[ -z "$hash" ]]; then
      password="${FRAMEFLOW_AUTH_PASSWORD:-}"
      if [[ -z "$password" ]]; then
        [[ -t 0 ]] || die "非交互环境请先写入 BASIC_AUTH_HASH，或临时传入 FRAMEFLOW_AUTH_PASSWORD"
        read -r -s -p "设置 Demo 访问密码：" password
        printf '\n'
        read -r -s -p "再次输入密码：" confirmation
        printf '\n'
        [[ "$password" == "$confirmation" ]] || die "两次密码不一致"
      fi
      [[ ${#password} -ge 12 ]] || die "Demo 密码至少需要 12 个字符"
      hash="$(printf '%s\n' "$password" | docker run --rm -i caddy:2.10-alpine \
        sh -eu -c 'IFS= read -r password; caddy hash-password --plaintext "$password"')"
      unset password confirmation
      [[ -n "$hash" ]] || die "密码哈希生成失败"
      upsert_env BASIC_AUTH_HASH "'$hash'"
    fi
    [[ "$hash" =~ ^\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}$ ]] \
      || die "BASIC_AUTH_HASH 不是有效的 bcrypt 哈希；请清空后重新运行 enable"
    cp "$template" "$snippet"
    chmod 600 "$snippet"
    upsert_env ENABLE_BASIC_AUTH true
    compose config --quiet
    restart_caddy_if_running
    printf 'Basic Auth 已启用（用户：%s）。\n' "$user"
    ;;
  disable)
    rm -f "$snippet"
    upsert_env ENABLE_BASIC_AUTH false
    compose config --quiet
    restart_caddy_if_running
    printf '警告：Basic Auth 已关闭，仅应在可信内网或外层已有强鉴权时使用。\n' >&2
    ;;
  *)
    die "用法：bash deploy/configure-auth.sh {enable|disable|status}"
    ;;
esac
