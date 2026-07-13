#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_command awk
require_command curl

domain="${1:-}"
email="${2:-}"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ROOT_DIR/deploy/.env.example" "$ENV_FILE"
fi

if [[ -n "$domain" ]]; then
  [[ "$domain" != *://* && "$domain" != *" "* ]] || die "域名只填写主机名，例如 app.example.com"
  upsert_env DOMAIN "$domain"
  upsert_env FRAMEFLOW_CORS_ORIGINS "https://$domain"
fi
if [[ -n "$email" ]]; then
  upsert_env ACME_EMAIL "$email"
fi

domain="$(env_value DOMAIN)"
[[ -n "$domain" && "$domain" != *example.com ]] || die "请执行：bash deploy/first-deploy.sh 你的域名 管理员邮箱"
email="$(env_value ACME_EMAIL)"
[[ "$email" == *@* && "$email" != *example.com ]] || die "请填写真实的 ACME 联系邮箱"

chmod 600 "$ENV_FILE"
if [[ "$(env_value ENABLE_BASIC_AUTH true)" == "true" ]]; then
  FRAMEFLOW_AUTH_NO_RESTART=1 bash "$SCRIPT_DIR/configure-auth.sh" enable
else
  printf '警告：ENABLE_BASIC_AUTH=false，当前部署不会启用整站鉴权。\n' >&2
  FRAMEFLOW_AUTH_NO_RESTART=1 bash "$SCRIPT_DIR/configure-auth.sh" disable
fi
cd "$ROOT_DIR"
compose config --quiet

printf '开始构建 FrameFlow 镜像……\n'
compose build --pull
compose up -d --remove-orphans

if ! wait_ready 90; then
  compose ps
  compose logs --tail=150 frameflow
  die "服务未在 180 秒内就绪"
fi

printf '\n部署完成：\n'
compose ps
printf '站点：https://%s\n' "$domain"
printf '健康检查：https://%s/health/ready\n' "$domain"
printf '提示：DNS A/AAAA 记录必须已指向本机，且防火墙放行 TCP 80/443 与 UDP 443。\n'
