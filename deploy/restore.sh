#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env
require_command tar

archive="${1:-}"
confirmation="${2:-}"
[[ -f "$archive" ]] || die "备份文件不存在：$archive"
[[ "$confirmation" == "--force" ]] || die "恢复会覆盖当前 /data；确认后追加 --force"

if tar -tzf "$archive" | awk '/^\// || /(^|\/)\.\.($|\/)/ {bad=1} END {exit bad ? 0 : 1}'; then
  die "备份包包含不安全路径"
fi

archive_dir="$(cd "$(dirname "$archive")" && pwd)"
archive_name="$(basename "$archive")"
volume="$(env_value DATA_VOLUME_NAME frameflow_data)"
docker volume inspect "$volume" >/dev/null 2>&1 || docker volume create "$volume" >/dev/null

compose stop -t 30 frameflow || true
docker run --rm -v "$volume:/data" caddy:2.10-alpine \
  sh -c 'rm -rf /data/* /data/.[!.]* /data/..?*'
docker run --rm \
  -v "$volume:/data" \
  -v "$archive_dir:/backup:ro" \
  caddy:2.10-alpine \
  tar -xzf "/backup/$archive_name" -C /data

compose up -d --remove-orphans
if ! wait_ready 90; then
  compose logs --tail=150 frameflow
  die "数据已恢复，但服务未能就绪，请检查日志"
fi
printf '恢复完成：%s\n' "$archive"
