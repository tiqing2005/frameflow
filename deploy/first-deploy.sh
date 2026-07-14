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
  upsert_env PUBLIC_SMOKE_URL "https://$domain"
  if [[ "$(env_value FRAMEFLOW_ASR_PROVIDER)" == "dashscope" ]]; then
    upsert_env FRAMEFLOW_ASR_PUBLIC_BASE_URL "https://$domain"
  fi
fi
if [[ -n "$email" ]]; then
  upsert_env ACME_EMAIL "$email"
fi

if [[ "$(env_value FRAMEFLOW_ASR_PROVIDER)" == "dashscope" ]] \
  && [[ -z "$(env_value FRAMEFLOW_ASR_URL_SIGNING_SECRET)" ]]; then
  require_command openssl
  upsert_env FRAMEFLOW_ASR_URL_SIGNING_SECRET "$(openssl rand -hex 32)"
fi

domain="$(env_value DOMAIN)"
[[ -n "$domain" && "$domain" != *example.com ]] || die "请执行：bash deploy/first-deploy.sh 你的域名 管理员邮箱"
email="$(env_value ACME_EMAIL)"
validate_edge_mode
if ! using_external_nginx; then
  [[ "$email" == *@* && "$email" != *example.com ]] || die "请填写真实的 ACME 联系邮箱"
fi

chmod 600 "$ENV_FILE"

if [[ -z "$(env_value FRAMEFLOW_AUTH_PASSWORD_HASH)" ]]; then
  [[ -z "$(env_value FRAMEFLOW_AUTH_PASSWORD)" ]] \
    || die "deploy/.env 不得保存 FRAMEFLOW_AUTH_PASSWORD 明文，请清空后重新部署"
  [[ -t 0 ]] || die "非交互部署请先配置 FRAMEFLOW_AUTH_PASSWORD_HASH"
  read -r -s -p "设置应用管理员密码：" app_password
  printf '\n'
  read -r -s -p "再次输入应用管理员密码：" app_confirmation
  printf '\n'
  [[ "$app_password" == "$app_confirmation" ]] || die "两次应用管理员密码不一致"
  [[ ${#app_password} -ge 12 ]] || die "应用管理员密码至少需要 12 个字符"
  [[ "$app_password" =~ [[:alpha:]] && "$app_password" =~ [[:digit:][:punct:]] ]] \
    || die "应用管理员密码至少组合使用字母、数字或符号中的两类"
  app_password_hash="$(printf '%s' "$app_password" | generate_application_password_hash)"
  # Compose 会展开未转义的 `$salt`/`$digest`；单引号既保留完整哈希，也不会进入容器值。
  upsert_env FRAMEFLOW_AUTH_PASSWORD_HASH "'$app_password_hash'"
  upsert_env FRAMEFLOW_AUTH_PASSWORD ""
  unset app_password app_confirmation app_password_hash
fi
upsert_env FRAMEFLOW_AUTH_ENABLED true
upsert_env FRAMEFLOW_AUTH_LOCAL_SETUP_ENABLED false
validate_application_auth_state

if using_external_nginx; then
  FRAMEFLOW_AUTH_NO_RESTART=1 bash "$SCRIPT_DIR/configure-auth.sh" disable
elif is_true "$(env_value ENABLE_BASIC_AUTH true)"; then
  auth_password=""
  if [[ -z "$(env_value BASIC_AUTH_HASH)" ]]; then
    [[ -t 0 ]] || die "非交互部署请先配置 BASIC_AUTH_HASH"
    read -r -s -p "设置 Demo 访问密码：" auth_password
    printf '\n'
    read -r -s -p "再次输入密码：" auth_confirmation
    printf '\n'
    [[ "$auth_password" == "$auth_confirmation" ]] || die "两次密码不一致"
    [[ ${#auth_password} -ge 12 ]] || die "Demo 密码至少需要 12 个字符"
  fi
  FRAMEFLOW_AUTH_PASSWORD="$auth_password" FRAMEFLOW_AUTH_NO_RESTART=1 \
    bash "$SCRIPT_DIR/configure-auth.sh" enable
  if [[ -n "$auth_password" && -z "${FRAMEFLOW_SMOKE_PASSWORD:-}" ]]; then
    export FRAMEFLOW_SMOKE_PASSWORD="$auth_password"
  fi
  unset auth_password auth_confirmation
else
  printf '警告：ENABLE_BASIC_AUTH=false，当前部署不会启用整站鉴权。\n' >&2
  FRAMEFLOW_AUTH_NO_RESTART=1 bash "$SCRIPT_DIR/configure-auth.sh" disable
fi
validate_auth_state
validate_application_auth_state
cd "$ROOT_DIR"
compose config --quiet

printf '开始构建 FrameFlow 镜像……\n'
compose build --pull
compose up -d --remove-orphans

if ! wait_release_ready 90; then
  show_release_diagnostics
  die "发布验收失败：请检查应用就绪、边缘代理、DNS、HTTPS 与鉴权配置"
fi
unset FRAMEFLOW_SMOKE_PASSWORD

printf '\n部署完成：\n'
compose ps
printf '站点：https://%s\n' "$domain"
printf '健康检查：https://%s/health/ready\n' "$domain"
if using_external_nginx; then
  printf '入口模式：external-nginx（应用仅监听 127.0.0.1:%s）。\n' "$(env_value FRAMEFLOW_HOST_PORT 8080)"
  printf '提示：请由宿主机 Nginx 提供 HTTPS，并反向代理到上述回环地址。\n'
else
  printf '提示：DNS A/AAAA 记录必须已指向本机，且防火墙放行 TCP 80/443 与 UDP 443。\n'
fi
