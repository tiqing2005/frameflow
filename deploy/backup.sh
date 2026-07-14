#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env
require_command sha256sum
require_command tar
require_command df
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
chmod 700 "$BACKUP_DIR"

volume="$(env_value DATA_VOLUME_NAME frameflow_data)"
docker volume inspect "$volume" >/dev/null 2>&1 || die "数据卷不存在：$volume"
include_model_cache="$(env_value BACKUP_INCLUDE_MODEL_CACHE false)"
if is_true "$include_model_cache"; then
  include_model_cache=true
else
  include_model_cache=false
fi
reserve_mb="$(env_value BACKUP_MIN_FREE_MB 1024)"
retention_days="$(env_value BACKUP_RETENTION_DAYS 30)"
retention_count="$(env_value BACKUP_RETENTION_COUNT 20)"
[[ "$reserve_mb" =~ ^[0-9]+$ && "$retention_days" =~ ^[0-9]+$ && "$retention_count" =~ ^[0-9]+$ ]] \
  || die "BACKUP_MIN_FREE_MB / BACKUP_RETENTION_DAYS / BACKUP_RETENTION_COUNT 必须是非负整数"

# 预估需要复制的有效数据量。模型和缓存默认不进入归档，可在恢复后重新下载。
included_kb="$(docker run --rm -v "$volume:/data:ro" -e INCLUDE_MODEL_CACHE="$include_model_cache" \
  caddy:2.10-alpine sh -eu -c '
    total=$(du -sk /data | awk "{print \$1}")
    if [ "$INCLUDE_MODEL_CACHE" != true ]; then
      for path in /data/models /data/cache /data/home/frameflow/.cache; do
        if [ -e "$path" ]; then
          size=$(du -sk "$path" | awk "{print \$1}")
          total=$((total - size))
        fi
      done
    fi
    [ "$total" -ge 0 ] || total=0
    printf "%s\n" "$total"
  ')"
volume_free_kb="$(docker run --rm -v "$volume:/data:ro" caddy:2.10-alpine \
  sh -eu -c 'df -Pk /data | awk "END {print \$4}"')"
backup_free_kb="$(df -Pk "$BACKUP_DIR" | awk 'END {print $4}')"
required_kb=$((included_kb + reserve_mb * 1024))
((volume_free_kb >= required_kb)) \
  || die "Docker 数据盘空间不足：预计至少需要 $((required_kb / 1024)) MiB，当前可用 $((volume_free_kb / 1024)) MiB"
((backup_free_kb >= required_kb)) \
  || die "备份目录空间不足：预计至少需要 $((required_kb / 1024)) MiB，当前可用 $((backup_free_kb / 1024)) MiB"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="frameflow-$stamp.tar.gz"
counter=1
while [[ -e "$BACKUP_DIR/$archive" || -e "$BACKUP_DIR/$archive.sha256" ]]; do
  archive="frameflow-$stamp-$counter.tar.gz"
  counter=$((counter + 1))
done
snapshot_volume="${volume}_backup_${stamp}_$$"
docker volume create "$snapshot_volume" >/dev/null
was_running=false
app_stopped=false

if compose ps --status running --services | grep -qx frameflow; then
  was_running=true
  compose stop -t 30 frameflow
  app_stopped=true
fi

cleanup() {
  docker volume rm -f "$snapshot_volume" >/dev/null 2>&1 || true
  if [[ "$app_stopped" == true && "$was_running" == true && "${KEEP_STOPPED:-0}" != 1 ]]; then
    compose start frameflow >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# 停写窗口只负责复制和数据库检查；耗时的 gzip 在服务恢复后执行。
docker run --rm \
  -v "$volume:/data:ro" \
  -v "$snapshot_volume:/snapshot" \
  -e INCLUDE_MODEL_CACHE="$include_model_cache" \
  caddy:2.10-alpine sh -eu -c '
    if [ "$INCLUDE_MODEL_CACHE" = true ]; then
      tar -cf - -C /data .
    else
      tar --exclude="./models" --exclude="./cache" \
        --exclude="./home/frameflow/.cache" -cf - -C /data .
    fi | tar -xf - -C /snapshot
    test -f /snapshot/frameflow.db
  '
if ! sqlite_check_volume "$snapshot_volume"; then
  if [[ "${BACKUP_ALLOW_SQLITE_FAILURE:-0}" == 1 ]]; then
    printf '警告：SQLite 校验失败，仅为恢复前取证保留该安全备份。\n' >&2
  else
    die "SQLite integrity_check / foreign_key_check 未通过，拒绝生成常规备份"
  fi
fi

if [[ "$was_running" == true && "${KEEP_STOPPED:-0}" != 1 ]]; then
  compose start frameflow >/dev/null
  app_stopped=false
fi

docker run --rm \
  -v "$snapshot_volume:/snapshot:ro" \
  -v "$BACKUP_DIR:/backup" \
  -e ARCHIVE="$archive" \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  caddy:2.10-alpine sh -eu -c '
    umask 077
    tar -czf "/backup/$ARCHIVE" -C /snapshot .
    chown "$HOST_UID:$HOST_GID" "/backup/$ARCHIVE"
    chmod 600 "/backup/$ARCHIVE"
  '

[[ -s "$BACKUP_DIR/$archive" ]] || die "备份文件为空"
tar -tzf "$BACKUP_DIR/$archive" >/dev/null || die "备份归档完整性检查失败"
digest="$(sha256sum "$BACKUP_DIR/$archive" | awk '{print $1}')"
printf '%s  %s\n' "$digest" "$archive" > "$BACKUP_DIR/$archive.sha256"
chmod 600 "$BACKUP_DIR/$archive" "$BACKUP_DIR/$archive.sha256"

if ((retention_days > 0)); then
  while IFS= read -r expired; do
    [[ "$expired" == "$BACKUP_DIR/$archive" ]] && continue
    rm -f -- "$expired" "$expired.sha256"
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'frameflow-*.tar.gz' -mtime "+$retention_days" -print)
fi
if ((retention_count > 0)); then
  mapfile -t archives < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'frameflow-*.tar.gz' -print | sort -r)
  for ((i=retention_count; i<${#archives[@]}; i++)); do
    [[ "${archives[$i]}" == "$BACKUP_DIR/$archive" ]] && continue
    rm -f -- "${archives[$i]}" "${archives[$i]}.sha256"
  done
fi

if [[ "$was_running" == true && "${KEEP_STOPPED:-0}" != 1 ]]; then
  wait_ready 60 || die "备份已生成，但 FrameFlow 未能在备份后恢复就绪"
fi
printf 'SHA-256：%s\n' "$digest" >&2
printf '备份范围：数据库与业务媒体%s。\n' "$([[ "$include_model_cache" == true ]] && printf '（包含模型/缓存）' || printf '（不含可再下载模型/缓存）')" >&2
printf '%s\n' "$BACKUP_DIR/$archive"
