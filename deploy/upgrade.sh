#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env
cd "$ROOT_DIR"

if [[ "${SKIP_GIT_PULL:-0}" != 1 ]]; then
  require_command git
  git pull --ff-only
fi

compose config --quiet
tag="$(env_value FRAMEFLOW_TAG latest)"
image="frameflow:$tag"
rollback="frameflow:rollback-$(date -u +%Y%m%dT%H%M%SZ)"
old_image_id="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || true)"
if [[ -n "$old_image_id" ]]; then
  docker tag "$old_image_id" "$rollback"
fi

printf '构建新镜像……\n'
compose build --pull
printf '创建一致性备份……\n'
if ! KEEP_STOPPED=1 bash "$SCRIPT_DIR/backup.sh"; then
  compose start frameflow >/dev/null 2>&1 || true
  die "升级前备份失败，旧版本已尝试重新启动"
fi

if compose up -d --remove-orphans && wait_ready 90; then
  compose ps
  printf '升级完成。回滚镜像保留为 %s\n' "$rollback"
  exit 0
fi

compose logs --tail=150 frameflow || true
if [[ -n "$old_image_id" ]]; then
  printf '新版本未就绪，正在回滚应用镜像……\n' >&2
  docker tag "$old_image_id" "$image"
  compose up -d --no-build --force-recreate frameflow caddy
  wait_ready 90 || die "自动回滚后仍未就绪，请人工处理"
  die "新版本健康检查失败，已自动回滚到旧镜像"
fi
die "新版本健康检查失败，且没有可用的旧镜像"
