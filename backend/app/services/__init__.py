"""Service layer split into use-case modules.

All public symbols are re-exported here so that existing imports such as
``from .services import add_audit, dumps, stable_hash`` (worker) and the batch
import in ``main``/``routers`` keep working unchanged.
"""
from __future__ import annotations

from .assets import (
    ASSET_EXTENSIONS,
    MINIMUM_ACTIVE_ASSETS,
    _parse_csv,
    _valid_asset_signature,
    create_asset,
    delete_asset,
    get_asset,
    list_assets,
    patch_asset,
)
from .asset_tagging import request_asset_retag
from .audit import list_audit
from .common import (
    _get_asset,
    _get_job,
    _get_project,
    _get_segment,
    add_audit,
    dumps,
    stable_hash,
)
from .jobs import cancel_job, get_job_detail, retry_job, set_fault
from .projects import (
    SOURCE_EXTENSIONS,
    _existing_idempotent,
    _valid_source_signature,
    create_text_project,
    create_upload_project,
    dashboard,
    delete_project,
    list_projects,
    project_detail,
)
from .previews import (
    build_preview_plan,
    create_preview_job,
    get_project_preview,
    public_preview_plan,
    update_timeline_timing,
)
from .runs import list_runs
from .segments import (
    _asset_rank_payloads,
    _segment_detail,
    patch_segment,
    patch_segment_timing,
    rematch_segment,
    reorder_segments,
)
from .selections import put_selection

__all__ = [
    "ASSET_EXTENSIONS",
    "MINIMUM_ACTIVE_ASSETS",
    "SOURCE_EXTENSIONS",
    "add_audit",
    "cancel_job",
    "create_asset",
    "delete_asset",
    "create_text_project",
    "create_upload_project",
    "create_preview_job",
    "dashboard",
    "delete_project",
    "dumps",
    "get_job_detail",
    "get_asset",
    "get_project_preview",
    "list_assets",
    "list_audit",
    "list_projects",
    "list_runs",
    "patch_asset",
    "patch_segment",
    "patch_segment_timing",
    "project_detail",
    "public_preview_plan",
    "update_timeline_timing",
    "put_selection",
    "rematch_segment",
    "reorder_segments",
    "retry_job",
    "request_asset_retag",
    "set_fault",
    "stable_hash",
    "build_preview_plan",
]
