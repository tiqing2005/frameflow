#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env
require_command tar
require_command sha256sum

archive="${1:-}"
confirmation="${2:-}"
[[ -f "$archive" ]] || die "备份文件不存在：$archive"
[[ "$confirmation" == "--force" ]] || die "恢复会覆盖当前 /data；确认后追加 --force"

checksum_file="$archive.sha256"
[[ -f "$checksum_file" ]] || die "缺少校验文件：$checksum_file；请先确认归档来源并生成 SHA-256"
expected="$(awk 'NR == 1 {print $1}' "$checksum_file")"
[[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || die "校验文件格式错误：$checksum_file"
actual="$(sha256sum "$archive" | awk '{print $1}')"
[[ "${actual,,}" == "${expected,,}" ]] || die "SHA-256 不匹配，拒绝恢复"
tar -tzf "$archive" >/dev/null || die "归档损坏或不是有效的 tar.gz"

if tar -tzf "$archive" | awk '/^\// || /(^|\/)\.\.($|\/)/ {bad=1} END {exit bad ? 0 : 1}'; then
  die "备份包包含不安全路径"
fi

archive_dir="$(cd "$(dirname "$archive")" && pwd)"
archive_name="$(basename "$archive")"
volume="$(env_value DATA_VOLUME_NAME frameflow_data)"
staging_volume="${volume}_restore_$(date -u +%Y%m%dT%H%M%SZ)_$$"
docker volume create "$staging_volume" >/dev/null
cleanup_staging() {
  docker volume rm -f "$staging_volume" >/dev/null 2>&1 || true
}
trap cleanup_staging EXIT

# 先在隔离卷中完整解包和验证；恶意链接或特殊设备不会进入正式数据卷。
docker run --rm \
  -v "$staging_volume:/staging" \
  -v "$archive_dir:/backup:ro" \
  -e ARCHIVE="$archive_name" \
  caddy:2.10-alpine \
  sh -eu -c '
    tar -xzf "/backup/$ARCHIVE" -C /staging
    if find /staging \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit | grep -q .; then
      echo "归档包含链接或特殊文件" >&2
      exit 1
    fi
    test -f /staging/frameflow.db || { echo "归档缺少 frameflow.db" >&2; exit 1; }
  ' || die "隔离解包验证失败，正式数据未被修改"

safety_backup=""
if docker volume inspect "$volume" >/dev/null 2>&1; then
  printf '正在创建恢复前安全备份……\n' >&2
  safety_backup="$(KEEP_STOPPED=1 bash "$SCRIPT_DIR/backup.sh" | tail -n 1)"
  [[ -n "$safety_backup" ]] || die "恢复前安全备份失败"
  printf '恢复前安全备份：%s（请保留到验收完成）\n' "$safety_backup" >&2
else
  docker volume create "$volume" >/dev/null
fi

compose stop -t 30 frameflow || true
docker run --rm -v "$volume:/data" caddy:2.10-alpine \
  sh -c 'rm -rf /data/* /data/.[!.]* /data/..?*'
docker run --rm \
  -v "$volume:/data" \
  -v "$staging_volume:/staging:ro" \
  caddy:2.10-alpine \
  sh -eu -c 'cp -a /staging/. /data/; chown -R 10001:10001 /data' \
  || die "写入正式数据卷失败；请使用恢复前安全备份重新恢复：${safety_backup:-无}"

compose up -d --remove-orphans
if ! wait_ready 90; then
  compose logs --tail=150 frameflow
  die "数据已恢复，但服务未能就绪，请检查日志"
fi
printf '恢复完成：%s\n' "$archive"
[[ -n "$safety_backup" ]] && printf '确认数据无误后再清理恢复前安全备份：%s\n' "$safety_backup"
