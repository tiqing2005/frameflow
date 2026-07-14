#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

require_runtime
require_env
validate_edge_mode
validate_auth_state
validate_application_auth_state
compose config --quiet

if ! wait_release_ready "${FRAMEFLOW_SMOKE_READY_ATTEMPTS:-30}"; then
  show_release_diagnostics
  die "发布 smoke 失败"
fi

compose ps
printf '发布 smoke 通过。\n'
