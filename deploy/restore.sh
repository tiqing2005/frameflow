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
[[ "$confirmation" == "--force" ]] || die "恢复会切换当前 /data；确认后追加 --force"

checksum_file="$archive.sha256"
[[ -f "$checksum_file" ]] || die "缺少校验文件：$checksum_file"
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
original_volume="$(env_value DATA_VOLUME_NAME frameflow_data)"
restored_volume="${original_volume}_restored_$(date -u +%Y%m%dT%H%M%SZ)_$$"
docker volume create "$restored_volume" >/dev/null
activated=false
cleanup() {
  if [[ "$activated" != true ]]; then
    docker volume rm -f "$restored_volume" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# 先在新卷解包、拒绝链接/特殊文件，并验证数据库；正式卷此时完全不变。
docker run --rm \
  -v "$restored_volume:/restore" \
  -v "$archive_dir:/backup:ro" \
  -e ARCHIVE="$archive_name" \
  caddy:2.10-alpine sh -eu -c '
    tar -xzf "/backup/$ARCHIVE" -C /restore
    if find /restore \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit | grep -q .; then
      echo "归档包含链接或特殊文件" >&2
      exit 1
    fi
    test -f /restore/frameflow.db || { echo "归档缺少 frameflow.db" >&2; exit 1; }
    chown -R 10001:10001 /restore
  ' || die "隔离解包验证失败，当前数据未被修改"
sqlite_check_volume "$restored_volume"

safety_backup=""
original_exists=false
if docker volume inspect "$original_volume" >/dev/null 2>&1; then
  original_exists=true
  printf '正在创建恢复前安全备份……\n' >&2
  safety_backup="$(KEEP_STOPPED=1 BACKUP_ALLOW_SQLITE_FAILURE=1 bash "$SCRIPT_DIR/backup.sh" | tail -n 1)"
  [[ -n "$safety_backup" ]] || die "恢复前安全备份失败"
  printf '恢复前安全备份：%s\n' "$safety_backup" >&2
fi

upsert_env DATA_VOLUME_NAME "$restored_volume"
if compose up -d --remove-orphans --force-recreate frameflow caddy && wait_release_ready 90; then
  activated=true
  printf '恢复完成：%s\n' "$archive"
  if [[ "$original_exists" == true ]]; then
    printf '旧数据卷仍保留为 %s；确认恢复无误后再手工删除。\n' "$original_volume"
    printf '恢复前安全备份：%s\n' "$safety_backup"
  fi
  exit 0
fi

show_release_diagnostics
if [[ "$original_exists" == true ]]; then
  printf '恢复版本未通过发布验收，正在切回原数据卷 %s……\n' "$original_volume" >&2
  upsert_env DATA_VOLUME_NAME "$original_volume"
  if compose up -d --remove-orphans --force-recreate frameflow caddy && wait_release_ready 90; then
    die "恢复失败，已自动切回原数据卷；安全备份位于 ${safety_backup}"
  fi
  show_release_diagnostics
  die "恢复与自动回滚均未通过验收；原卷仍为 $original_volume，安全备份位于 ${safety_backup}"
fi

# 首次从备份创建环境时没有旧卷可回滚，保留已解包卷和配置供排障。
activated=true
die "恢复数据未通过发布验收；已保留数据卷 $restored_volume，请根据日志修复后重试 deploy/smoke.sh"
