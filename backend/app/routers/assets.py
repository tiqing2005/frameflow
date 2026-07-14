from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Request, UploadFile, Response, status

from ..errors import request_id
from ..schemas import AssetPatch
from ..serializers import asset_dict
from ..services import (
    create_asset,
    delete_asset,
    get_asset,
    list_assets,
    patch_asset,
    request_asset_retag,
)
from ._deps import SessionDep, SettingsDep

router = APIRouter(prefix="/api/v1", tags=["assets"])


@router.get("/assets")
def get_assets(
    session: SessionDep,
    q: str | None = Query(None, max_length=100),
    kind: str | None = Query(None, pattern="^(image|video)$"),
    tag: str | None = Query(None, max_length=60),
    include_inactive: bool = False,
):
    return list_assets(session, q, kind, tag, include_inactive)


@router.get("/assets/{asset_id}")
def get_asset_detail(asset_id: str, session: SessionDep):
    return asset_dict(get_asset(session, asset_id))


@router.post("/assets", status_code=status.HTTP_201_CREATED)
def post_asset(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
    name: Annotated[str, Form(min_length=1, max_length=160)],
    tags: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
):
    asset = create_asset(
        session,
        settings,
        file.filename or "asset",
        file.content_type,
        file.file,
        name,
        tags,
        keywords,
        request_id(request),
    )
    return asset_dict(asset)


@router.patch("/assets/{asset_id}")
def update_asset(asset_id: str, payload: AssetPatch, request: Request, session: SessionDep):
    return asset_dict(patch_asset(session, asset_id, payload, request_id(request)))


@router.post("/assets/{asset_id}/retag", status_code=status.HTTP_202_ACCEPTED)
def retag_asset(asset_id: str, request: Request, session: SessionDep):
    return asset_dict(request_asset_retag(session, asset_id, request_id(request)))


@router.delete("/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_asset(
    asset_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
) -> Response:
    delete_asset(session, settings, asset_id, request_id(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
