#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env
require_command sha256sum
require_command tar
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
chmod 700 "$BACKUP_DIR"

volume="$(env_value DATA_VOLUME_NAME frameflow_data)"
docker volume inspect "$volume" >/dev/null 2>&1 || die "数据卷不存在：$volume"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="frameflow-$stamp.tar.gz"
counter=1
while [[ -e "$BACKUP_DIR/$archive" || -e "$BACKUP_DIR/$archive.sha256" ]]; do
  archive="frameflow-$stamp-$counter.tar.gz"
  counter=$((counter + 1))
done
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
  -e ARCHIVE="$archive" \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  caddy:2.10-alpine \
  sh -eu -c '
    umask 077
    tar -czf "/backup/$ARCHIVE" -C /data .
    chown "$HOST_UID:$HOST_GID" "/backup/$ARCHIVE"
    chmod 600 "/backup/$ARCHIVE"
  '

[[ -s "$BACKUP_DIR/$archive" ]] || die "备份文件为空"
tar -tzf "$BACKUP_DIR/$archive" >/dev/null || die "备份归档完整性检查失败"
digest="$(sha256sum "$BACKUP_DIR/$archive" | awk '{print $1}')"
printf '%s  %s\n' "$digest" "$archive" > "$BACKUP_DIR/$archive.sha256"
chmod 600 "$BACKUP_DIR/$archive" "$BACKUP_DIR/$archive.sha256"
printf 'SHA-256：%s\n' "$digest" >&2
printf '%s\n' "$BACKUP_DIR/$archive"
