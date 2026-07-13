#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"

volume="$(env_value DATA_VOLUME_NAME frameflow_data)"
docker volume inspect "$volume" >/dev/null 2>&1 || die "数据卷不存在：$volume"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="frameflow-$stamp.tar.gz"
was_running=false

if compose ps --status running --services | grep -qx frameflow; then
  was_running=true
  compose stop -t 30 frameflow
fi

restart_app() {
  if [[ "$was_running" == true && "${KEEP_STOPPED:-0}" != 1 ]]; then
    compose start frameflow >/dev/null
  fi
}
trap restart_app EXIT

docker run --rm \
  -v "$volume:/data:ro" \
  -v "$BACKUP_DIR:/backup" \
  caddy:2.10-alpine \
  tar -czf "/backup/$archive" -C /data .

[[ -s "$BACKUP_DIR/$archive" ]] || die "备份文件为空"
printf '%s\n' "$BACKUP_DIR/$archive"
